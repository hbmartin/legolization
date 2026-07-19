"""Run every placement strategy on one grid and select the best model.

Selection is lexicographic, following the reference papers (Luo et al.,
StableLego/BrickGPT, Kollsker): hard gates first, a scalar last. A weighted
sum over every concern at once is explicitly reported as unreliable in the
literature, so candidates are first gated on :attr:`PipelineResult.buildable`
(stable, one connected component, nothing floating) and only the survivors
are ranked — ascending by the weighted objective ``evaluate(...).total``,
tie-broken by higher maximin friction capacity (Luo's ``C_M``), then fewer
bricks, then strategy name so the result never depends on scheduling order.
When no candidate passes the gate the least-bad layout is chosen instead
(fewest components, then lowest worst-brick stress, then objective).

Every candidate runs the full pipeline with the *same* seed and weights;
only ``strategy`` differs (``progress`` is stripped so configs pickle under
the spawn start method). Timeouts are cooperative: ``timeout_s`` is folded
into ``time_budget_s``, which ``fast``/``smga``/``beauty`` honour, ``bond``
ignores (it is single-pass fast), and ``greedy``/``luo`` have no budget
hook — the parallel path additionally stops waiting once the deadline
passes and records stragglers as errors, but it cannot hard-kill a running
worker process.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, replace
from multiprocessing import get_context
from typing import TYPE_CHECKING

from legolization.pipeline import PipelineConfig, PipelineResult, run
from legolization.placement.base import ObjectiveWeights, evaluate
from legolization.placement.registry import strategy_names
from legolization.stability import SolverConfig, build_model, solve_maximin

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from concurrent.futures import Future

    from legolization.grid import VoxelGrid

_TIMEOUT_SLACK_S = 5.0


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    """Flat, JSON-safe scores for one finished candidate."""

    buildable: bool
    stable: bool
    component_count: int
    floating_count: int
    objective_total: float
    maximin_feasible: bool
    maximin_capacity: float
    max_score: float
    min_capacity: float
    brick_count: int
    mass_g: float
    step_count: int
    cost: float
    aesthetics: float
    colour_error: float
    perpendicularity: float
    symmetry: float


@dataclass(frozen=True, slots=True)
class Candidate:
    """One (strategy, seed) outcome: a scored result or a captured error."""

    strategy: str
    seconds: float
    seed: int = 0
    result: PipelineResult | None = None
    metrics: CandidateMetrics | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the strategy finished and was scored."""
        return self.error is None and self.metrics is not None


@dataclass(frozen=True, slots=True)
class SelectionReport:
    """The winning candidate, why it won, and the full field."""

    winner: Candidate | None
    reason: str
    candidates: tuple[Candidate, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-safe summary (no layouts or numpy payloads)."""
        return {
            "winner": self.winner.strategy if self.winner is not None else None,
            "winner_seed": self.winner.seed if self.winner is not None else None,
            "reason": self.reason,
            "buildable": (
                self.winner is not None
                and self.winner.metrics is not None
                and self.winner.metrics.buildable
            ),
            "candidates": [
                {
                    "strategy": candidate.strategy,
                    "seed": candidate.seed,
                    "seconds": round(candidate.seconds, 3),
                    "error": candidate.error,
                    "metrics": (
                        asdict(candidate.metrics)
                        if candidate.metrics is not None
                        else None
                    ),
                }
                for candidate in self.candidates
            ],
        }


def candidate_metrics(
    result: PipelineResult,
    *,
    weights: ObjectiveWeights,
    solver_config: SolverConfig,
) -> CandidateMetrics:
    """Score one pipeline result into flat comparable numbers."""
    if result.grid is None:
        msg = "candidate metrics need a placed result (imported models have no grid)"
        raise ValueError(msg)
    report = evaluate(
        result.layout,
        result.grid,
        weights=weights,
        solver_config=solver_config,
    )
    maximin = solve_maximin(build_model(result.layout))
    return CandidateMetrics(
        buildable=result.buildable,
        stable=result.stability.stable,
        component_count=result.component_count,
        floating_count=result.floating_count,
        objective_total=report.total,
        maximin_feasible=maximin.feasible,
        maximin_capacity=maximin.capacity,
        max_score=result.stability.max_score,
        min_capacity=result.stability.min_capacity,
        brick_count=result.brick_count,
        mass_g=result.mass_g,
        step_count=result.step_count,
        cost=report.cost,
        aesthetics=report.aesthetics,
        colour_error=report.colour_error,
        perpendicularity=report.perpendicularity,
        symmetry=report.symmetry,
    )


def select_best(candidates: Sequence[Candidate]) -> SelectionReport:
    """Pick the winner lexicographically; independent of input order."""
    ordered = tuple(
        sorted(candidates, key=lambda candidate: (candidate.strategy, candidate.seed))
    )
    scored = [
        (candidate, candidate.metrics)
        for candidate in ordered
        if candidate.error is None and candidate.metrics is not None
    ]
    if not scored:
        return SelectionReport(
            winner=None,
            reason="every strategy failed",
            candidates=ordered,
        )
    if buildable := [pair for pair in scored if pair[1].buildable]:
        winner, metrics = min(
            buildable,
            key=lambda pair: (
                pair[1].objective_total,
                -pair[1].maximin_capacity,
                pair[1].brick_count,
                pair[0].strategy,
                pair[0].seed,
            ),
        )
        reason = (
            f"buildable, best objective {metrics.objective_total:.4f} "
            f"(capacity {metrics.maximin_capacity:.3f} N, "
            f"{metrics.brick_count} bricks) "
            f"among {len(buildable)} buildable candidate(s)"
        )
    else:
        winner, metrics = min(
            scored,
            key=lambda pair: (
                pair[1].component_count,
                pair[1].max_score,
                pair[1].objective_total,
                pair[0].strategy,
                pair[0].seed,
            ),
        )
        reason = (
            f"no candidate is buildable; least-bad has "
            f"{metrics.component_count} component(s), "
            f"worst stress {metrics.max_score:.3f}, "
            f"objective {metrics.objective_total:.4f}"
        )
    return SelectionReport(winner=winner, reason=reason, candidates=ordered)


def run_all(  # noqa: PLR0913 - sweep knobs are all keyword-only
    grid: VoxelGrid,
    config: PipelineConfig,
    *,
    jobs: int = 0,
    names: Sequence[str] | None = None,
    seeds: Sequence[int] | None = None,
    timeout_s: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[Candidate]:
    """Run each (strategy, seed) job; a single failure never kills the sweep.

    ``seeds`` adds restart candidates: every strategy runs once per seed
    (deduplicated, order preserved); None or empty means one run at
    ``config.seed``. ``jobs=0`` picks one worker per job capped at the
    CPU count; ``jobs=1`` runs sequentially in-process (no pool, no
    pickling). ``timeout_s`` stays one sweep-wide soft deadline — more
    seeds squeeze the same budget. Returns candidates sorted by
    (strategy, seed) regardless of completion order.
    """
    chosen = tuple(names) if names is not None else tuple(strategy_names())
    chosen_seeds = tuple(dict.fromkeys(seeds)) if seeds else (config.seed,)
    configs = {
        (name, seed): _candidate_config(
            config, strategy=name, seed=seed, timeout_s=timeout_s
        )
        for name in chosen
        for seed in chosen_seeds
    }
    workers = jobs if jobs > 0 else min(len(configs), os.cpu_count() or 1)
    if workers == 1:
        candidates = []
        for candidate_config in configs.values():
            candidate = _run_candidate(grid, candidate_config)
            _report(progress, candidate)
            candidates.append(candidate)
    else:
        candidates = _run_parallel(
            grid,
            configs,
            workers=workers,
            timeout_s=timeout_s,
            progress=progress,
        )
    return sorted(candidates, key=lambda c: (c.strategy, c.seed))


def _candidate_config(
    config: PipelineConfig,
    *,
    strategy: str,
    seed: int,
    timeout_s: float | None,
) -> PipelineConfig:
    """Clone the config for one job; strip the unpicklable progress."""
    budgets = [
        budget for budget in (config.time_budget_s, timeout_s) if budget is not None
    ]
    return replace(
        config,
        strategy=strategy,
        seed=seed,
        progress=None,
        time_budget_s=min(budgets) if budgets else None,
    )


def _run_candidate(grid: VoxelGrid, config: PipelineConfig) -> Candidate:
    """Run one strategy end to end; exceptions become data, never propagate.

    Module-level so the spawn start method can pickle it. Metrics are
    computed here, in the worker, so their LP solves parallelize too.
    """
    start = time.perf_counter()
    try:
        result = run(grid=grid, config=config)
        metrics = candidate_metrics(
            result,
            weights=config.weights,
            solver_config=config.solver,
        )
    except Exception as error:  # noqa: BLE001 - isolate strategy failures
        return Candidate(
            strategy=config.strategy,
            seconds=time.perf_counter() - start,
            seed=config.seed,
            error=f"{type(error).__name__}: {error}",
        )
    return Candidate(
        strategy=config.strategy,
        seconds=time.perf_counter() - start,
        seed=config.seed,
        result=result,
        metrics=metrics,
    )


def _run_parallel(
    grid: VoxelGrid,
    configs: dict[tuple[str, int], PipelineConfig],
    *,
    workers: int,
    timeout_s: float | None,
    progress: Callable[[str], None] | None,
) -> list[Candidate]:
    """Fan out over a spawn pool with a soft, sweep-wide wait deadline.

    Pending futures become timeout candidates once the deadline passes.
    Already-running workers cannot be terminated by ``ProcessPoolExecutor``
    and may continue after this function returns.
    """
    start = time.perf_counter()
    candidates: list[Candidate] = []
    executor = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=get_context("spawn"),
    )
    try:
        pending: dict[Future[Candidate], tuple[str, int]] = {
            executor.submit(_run_candidate, grid, candidate_config): key
            for key, candidate_config in configs.items()
        }
        deadline = None if timeout_s is None else timeout_s + _TIMEOUT_SLACK_S
        try:
            for future in as_completed(pending, timeout=deadline):
                name, seed = pending.pop(future)
                candidate = _collect(future, strategy=name, seed=seed)
                _report(progress, candidate)
                candidates.append(candidate)
        except TimeoutError:
            pass
        elapsed = time.perf_counter() - start
        timeout_text = "" if timeout_s is None else f" after {timeout_s:.0f}s"
        for name, seed in pending.values():
            candidate = Candidate(
                strategy=name,
                seconds=elapsed,
                seed=seed,
                error=f"timed out{timeout_text}",
            )
            _report(progress, candidate)
            candidates.append(candidate)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return candidates


def _collect(future: Future[Candidate], *, strategy: str, seed: int) -> Candidate:
    """Unwrap a finished future; retrieval failures become error candidates."""
    try:
        return future.result(timeout=0)
    except BrokenProcessPool:
        return Candidate(
            strategy=strategy,
            seconds=0.0,
            seed=seed,
            error="worker process pool broke (native crash?)",
        )
    except Exception as error:  # noqa: BLE001 - isolate worker transport failures
        return Candidate(
            strategy=strategy,
            seconds=0.0,
            seed=seed,
            error=(f"failed to retrieve result: {type(error).__name__}: {error}"),
        )


def _report(progress: Callable[[str], None] | None, candidate: Candidate) -> None:
    if progress is None:
        return
    verdict = "ok" if candidate.ok else f"error: {candidate.error}"
    tag = f"[seed {candidate.seed}]" if candidate.seed else ""
    progress(f"{candidate.strategy}{tag}: {verdict} ({candidate.seconds:.1f}s)")


__all__ = [
    "Candidate",
    "CandidateMetrics",
    "SelectionReport",
    "candidate_metrics",
    "run_all",
    "select_best",
]
