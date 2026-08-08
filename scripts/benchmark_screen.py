"""Measure the reduced-QP screen against the exact LP (the Phase-3 gate).

Three sections, mirroring the pre-registered acceptance thresholds in
docs/guides/performance-testing.md:

1. **Shell series** — thin-shell greedy layouts at increasing radius
   (the same hollow-shell family as the pinned 348/505/712/902-brick
   rows, re-baselined same-session per the section-2 protocol): cold
   ``analyze`` wall vs screen wall (build/solve split from telemetry),
   verdict agreement, per-brick score Spearman, weakest-brick match.
2. **Candidate ranking** — K brick-removal perturbations per shell:
   pairwise-order agreement between screen q and cold q on pairs
   separated by more than the screen margin, plus the
   confident-false-reject rate at the actual certify gate — scored
   separately under each production consumer's acceptance rule
   (Luo maximin, Luo rbe, ALNS repair), since a rejection is only false
   against a rule that would have accepted the candidate. The reported
   gate number is the worst consumer.
3. **Corpus spread** — every synthetic corpus shape, greedy at seed 0:
   verdict agreement across non-shell topologies.

Writes ``eval/profiles/<UTC>-screen-bench.json`` and prints the
threshold verdicts.

Usage::

    uv run python scripts/benchmark_screen.py [--radii 8 10 12 14]
        [--candidates 30] [--seed 0] [--skip-corpus] [--skip-maximin]
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self

import numpy as np
from scipy import stats

from legolization import telemetry
from legolization.catalog import default_catalog
from legolization.corpus.generators import GENERATORS, thin_shell
from legolization.eval_artifacts import atomic_json, source_identity
from legolization.graph import ConnectionGraph
from legolization.grid import VoxelGrid
from legolization.pipeline import PipelineConfig
from legolization.placement.registry import make_strategy
from legolization.placement.snot import apply_snot
from legolization.stability import (
    SolverConfig,
    analyze,
    build_model_from_config,
    solve_maximin,
)
from legolization.stability.links import localize_instability
from legolization.stability.screen import ReducedScreen, screen_layout

if TYPE_CHECKING:
    from legolization.layout import Layout
    from legolization.stability.screen import ScreenReport
    from legolization.stability.solver import StabilityResult

_REPO = Path(__file__).resolve().parent.parent

_THRESHOLDS = {
    "speed_ratio_largest_max": 0.10,
    "speed_ratio_kill": 1.0 / 3.0,
    "setup_share_max": 0.30,
    "ranking_agreement_min": 0.95,
    "ranking_agreement_kill": 0.85,
    "confident_false_reject_max": 0.02,
    "verdict_agreement_min": 0.98,
    "nonconverged_share_max": 0.01,
    "nonconverged_share_kill": 0.05,
}


@dataclass(slots=True)
class _Tally:
    """Running screen-status and verdict-agreement counters.

    Verdict agreement is gated on *structural* rows (whole shell,
    corpus, and clad layouts) and only recorded informationally for the
    damaged removal candidates: on those, the exact LP's own objective
    (``sum(t) + ALPHA * sum(dmax)``) can prefer leaving a
    sub-tolerance equilibrium residual on feather-light SNOT parts over
    paying drag for it, flagging bricks the screen (which drives
    residuals to ~1e-7) reports balanced. That flip direction only ever
    costs a wasted cold certify — candidate quality is gated by the
    production-semantics ranking and false-reject metrics instead.
    """

    solves: int = 0
    nonconverged: int = 0
    verdict_pairs: int = 0
    verdict_agree: int = 0
    candidate_pairs: int = 0
    candidate_agree: int = 0

    def status(self, report: ScreenReport) -> None:
        self.solves += 1
        if report.status == "nonconverged":
            self.nonconverged += 1

    def verdict(
        self,
        report: ScreenReport,
        cold: StabilityResult,
        *,
        structural: bool = True,
    ) -> None:
        agree = report.status == "ok" and report.stable == cold.stable
        if structural:
            self.verdict_pairs += 1
            self.verdict_agree += int(agree)
        else:
            self.candidate_pairs += 1
            self.candidate_agree += int(agree)


def _place_shell(radius: int, seed: int) -> Layout:
    grid = VoxelGrid.from_array(thin_shell(radius), plates_per_voxel=3)
    config = PipelineConfig(seed=seed)
    strategy = make_strategy("greedy", catalog=default_catalog(), config=config)
    return strategy.place(grid, rng=np.random.default_rng(seed))


def _screen_with_split(
    layout: Layout, config: SolverConfig
) -> tuple[ScreenReport, float, float]:
    """Screen a layout; return (report, total_seconds, build_seconds)."""
    start = time.perf_counter()
    with telemetry.record() as session:
        report = screen_layout(layout, config)
    total = time.perf_counter() - start
    build = sum(
        session.spans[name].seconds
        for name in ("stability.reduced.build",)
        if name in session.spans
    )
    return report, total, build


@dataclass(slots=True)
class _ShellRow:
    """One shell-series measurement."""

    radius: int
    bricks: int
    cold_seconds: float
    screen_seconds: float
    screen_build_seconds: float
    status: str
    cold_stable: bool
    screen_stable: bool
    cold_q: float
    screen_q: float
    score_spearman: float
    weakest_match: bool | None

    @property
    def speed_ratio(self) -> float:
        return (
            self.screen_seconds / self.cold_seconds
            if self.cold_seconds
            else float("nan")
        )

    def payload(self) -> dict[str, object]:
        return {
            "radius": self.radius,
            "bricks": self.bricks,
            "cold_seconds": self.cold_seconds,
            "screen_seconds": self.screen_seconds,
            "screen_build_seconds": self.screen_build_seconds,
            "speed_ratio": self.speed_ratio,
            "status": self.status,
            "cold_stable": self.cold_stable,
            "screen_stable": self.screen_stable,
            "cold_q": self.cold_q,
            "screen_q": self.screen_q,
            "score_spearman": self.score_spearman,
            "weakest_match": self.weakest_match,
        }


def _shell_row(
    radius: int, seed: int, config: SolverConfig, tally: _Tally
) -> _ShellRow:
    layout = _place_shell(radius, seed)
    cold_start = time.perf_counter()
    cold = analyze(layout, config)
    cold_seconds = time.perf_counter() - cold_start
    report, screen_seconds, build_seconds = _screen_with_split(layout, config)
    tally.status(report)
    tally.verdict(report, cold)
    spearman = float("nan")
    weakest_match = None
    if report.status == "ok" and report.scores is not None and len(layout) > 2:
        ids = sorted(layout.bricks)
        cold_scores = [cold.scores[b].score for b in ids]
        screen_scores = [report.scores[b] for b in ids]
        rho = stats.spearmanr(cold_scores, screen_scores).statistic
        spearman = float(rho) if np.isfinite(rho) else float("nan")
        cold_weakest = max(cold.scores.values(), key=lambda s: s.score).brick_id
        screen_scores_map = report.scores
        screen_weakest = max(screen_scores_map, key=screen_scores_map.__getitem__)
        weakest_match = cold_weakest == screen_weakest
    return _ShellRow(
        radius=radius,
        bricks=len(layout),
        cold_seconds=cold_seconds,
        screen_seconds=screen_seconds,
        screen_build_seconds=build_seconds,
        status=report.status,
        cold_stable=cold.stable,
        screen_stable=report.stable,
        cold_q=cold.max_score,
        screen_q=report.q,
        score_spearman=spearman,
        weakest_match=weakest_match,
    )


def _lateral_dependents(layout: Layout) -> dict[int, set[int]]:
    """Cladding mounted on each carrier via lateral (SNOT) knobs."""
    dependents: dict[int, set[int]] = {}
    for knob in ConnectionGraph.from_layout(layout).knob_contacts:
        if knob.normal != (0, 0, 1):
            dependents.setdefault(knob.below_id, set()).add(knob.above_id)
    return dependents


def _candidates(layout: Layout, count: int, rng: np.random.Generator) -> list[Layout]:
    """Brick-removal perturbations, closing over stranded cladding.

    Removing a SNOT carrier without its mounted tiles floats the tiles
    — a candidate no production loop would generate — so each removal
    set is closed over the carriers' lateral dependents.
    """
    ids = sorted(layout.bricks)
    dependents = _lateral_dependents(layout)
    out: list[Layout] = []
    for _ in range(count):
        removal = {
            int(b) for b in rng.choice(ids, size=int(rng.integers(1, 4)), replace=False)
        }
        for carrier in list(removal):
            removal |= dependents.get(carrier, set())
        out.append(layout.subset(frozenset(ids) - removal))
    return out


_ACCEPTANCE_RULES = ("luo_maximin", "luo_rbe", "repair_alns")


@dataclass(slots=True)
class _Acceptance:
    """One layout's cold measurements under every consumer's metric.

    A false reject is only false relative to what the consumer would
    have done with the candidate, and the three production consumers
    disagree: ``LuoStrategy._better`` compares maximin capacity by
    default (``acceptance="maximin"``, ``placement/luo.py``) or the
    ``(unstable count, min capacity)`` tuple under ``"rbe"``, while ALNS
    repair accepts on a strictly lower localizer ``q``
    (``placement/repair.py``). Each is measured separately.
    """

    result: StabilityResult
    link_q: float
    capacity: float = float("nan")
    """Maximin capacity, or NaN when ``--skip-maximin`` drops that rule."""

    @classmethod
    def measure(cls, layout: Layout, config: SolverConfig, *, maximin: bool) -> Self:
        """Cold-measure a layout under every consumer's metric."""
        capacity = float("nan")
        if maximin:
            solved = solve_maximin(build_model_from_config(layout, config))
            capacity = solved.capacity if solved.feasible else float("-inf")
        return cls(
            result=analyze(layout, config),
            link_q=localize_instability(layout, config=config).q,
            capacity=capacity,
        )

    def accepted_over(self, base: _Acceptance) -> dict[str, bool]:
        """Which consumers would accept this candidate over ``base``.

        The ALNS rule carries repair's own loop guard: production only
        compares localizer q while ``base.link_q > _Q_TOLERANCE``
        (``repair_stability`` never iterates on a stable baseline), so
        a noise-level ordering of two ~zero q values is not an
        acceptance.
        """
        from legolization.placement.repair import _Q_TOLERANCE  # noqa: PLC0415

        rules = {
            "luo_rbe": (len(self.result.unstable_ids), -self.result.min_capacity)
            < (len(base.result.unstable_ids), -base.result.min_capacity),
            "repair_alns": base.link_q > _Q_TOLERANCE and self.link_q < base.link_q,
        }
        if not np.isnan(self.capacity):
            rules["luo_maximin"] = self.capacity > base.capacity
        return rules


@dataclass(slots=True)
class _DomainStats:
    """Ranking/false-reject counters for one candidate domain."""

    pairs: int = 0
    agree: int = 0
    gated: int = 0
    false_rejects: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(_ACCEPTANCE_RULES, 0)
    )


@dataclass(slots=True)
class _RankingStats:
    """Candidate-harness accumulators and shared run context.

    Vertical and lateral (SNOT-clad) candidates are scored separately:
    rank-rejection is production-scoped to vertical layouts, and on
    clad candidates the certifier's feather-light tie-zone verdicts
    make cold q an unrankable ordering ground truth — the gate numbers
    come from the vertical domain, the snot domain reports the scoped
    (count-clause-only) gate.
    """

    config: SolverConfig
    seed: int
    count: int
    tally: _Tally
    maximin: bool = True
    domains: dict[str, _DomainStats] = field(
        default_factory=lambda: {
            "vertical": _DomainStats(),
            "snot": _DomainStats(),
        }
    )
    rows: list[dict[str, object]] = field(default_factory=list)

    @property
    def pairs(self) -> int:
        return sum(d.pairs for d in self.domains.values())


def _rank_shell(
    layout: Layout, stats_acc: _RankingStats, domain: str = "vertical"
) -> None:
    config = stats_acc.config
    tally = stats_acc.tally
    stats = stats_acc.domains[domain]
    rng = np.random.default_rng(stats_acc.seed + 1)
    base_acceptance = _Acceptance.measure(layout, config, maximin=stats_acc.maximin)
    screen = ReducedScreen.create(layout, config)
    if screen is None:
        return
    tally.status(screen.baseline)
    pairs_q: list[tuple[float, float]] = []
    for candidate in _candidates(layout, stats_acc.count, rng):
        acceptance = _Acceptance.measure(candidate, config, maximin=stats_acc.maximin)
        cold = acceptance.result
        report, _, _ = _screen_with_split(candidate, config)
        tally.status(report)
        tally.verdict(report, cold, structural=False)
        if report.status != "ok":
            continue
        pairs_q.append((cold.max_score, report.q))
        # The PRODUCTION gate, not a re-implementation.
        if screen.should_reject(report, config.screen_margin):
            stats.gated += 1
            for rule, accepted in acceptance.accepted_over(base_acceptance).items():
                stats.false_rejects[rule] += int(accepted)
    for i in range(len(pairs_q)):
        for j in range(i + 1, len(pairs_q)):
            gap = abs(pairs_q[i][0] - pairs_q[j][0])
            if gap <= config.screen_margin:
                continue
            stats.pairs += 1
            cold_order = pairs_q[i][0] < pairs_q[j][0]
            screen_order = pairs_q[i][1] < pairs_q[j][1]
            if cold_order == screen_order:
                stats.agree += 1
    stats_acc.rows.append(
        {
            "domain": domain,
            "bricks": len(layout),
            "candidates": len(pairs_q),
        }
    )


def _corpus_rows(
    seed: int, config: SolverConfig, tally: _Tally
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    catalog = default_catalog()
    for name, generator in sorted(GENERATORS.items()):
        grid = VoxelGrid.from_array(generator(), plates_per_voxel=3)
        strategy = make_strategy(
            "greedy", catalog=catalog, config=PipelineConfig(seed=seed)
        )
        layout = strategy.place(grid, rng=np.random.default_rng(seed))
        if not len(layout):
            continue
        cold = analyze(layout, config)
        report, _, _ = _screen_with_split(layout, config)
        tally.status(report)
        tally.verdict(report, cold)
        rows.append(
            {
                "model": name,
                "bricks": len(layout),
                "status": report.status,
                "cold_stable": cold.stable,
                "screen_stable": report.stable,
                "cold_q": cold.max_score,
                "screen_q": report.q,
            }
        )
    return rows


_SNOT_MODELS = ("letter_h", "mushroom")
"""Clad models for the SNOT section; the FIRST also runs the candidate
harness, so keep it small — every candidate pays the consumer
acceptance measurements (links QP + optional maximin)."""


def _snot_rows(
    seed: int, config: SolverConfig, tally: _Tally
) -> list[dict[str, object]]:
    """Screen-vs-cold rows on SNOT-clad greedy layouts."""
    rows: list[dict[str, object]] = []
    catalog = default_catalog()
    for name in _SNOT_MODELS:
        grid = VoxelGrid.from_array(GENERATORS[name](), plates_per_voxel=3)
        strategy = make_strategy(
            "greedy", catalog=catalog, config=PipelineConfig(seed=seed)
        )
        layout = strategy.place(grid, rng=np.random.default_rng(seed))
        before = len(layout)
        apply_snot(layout, grid)
        cold = analyze(layout, config)
        report, _, _ = _screen_with_split(layout, config)
        tally.status(report)
        tally.verdict(report, cold)
        rows.append(
            {
                "model": name,
                "bricks": len(layout),
                "snot_parts": len(layout) - before,
                "status": report.status,
                "cold_stable": cold.stable,
                "screen_stable": report.stable,
                "cold_q": cold.max_score,
                "screen_q": report.q,
            }
        )
    return rows


def _verdicts(
    shells: list[_ShellRow],
    ranking: _RankingStats,
    tally: _Tally,
) -> dict[str, object]:
    largest = max(shells, key=lambda r: r.bricks) if shells else None
    speed_ratio = largest.speed_ratio if largest else float("nan")
    setup_share = (
        largest.screen_build_seconds / largest.screen_seconds
        if largest and largest.screen_seconds
        else float("nan")
    )
    vertical = ranking.domains["vertical"]
    snot = ranking.domains["snot"]
    ranking_agreement = vertical.agree / vertical.pairs if vertical.pairs else None

    def consumer_rates(stats: _DomainStats) -> dict[str, float]:
        return {
            rule: (count / stats.gated if stats.gated else 0.0)
            for rule, count in stats.false_rejects.items()
            if ranking.maximin or rule != "luo_maximin"
        }

    by_consumer = consumer_rates(vertical)
    snot_by_consumer = consumer_rates(snot)
    # The gate is the worst consumer over BOTH domains (the snot domain
    # runs the production-scoped count-clause-only gate), not an
    # average: a rejection only one rule would have taken is still a
    # lost candidate.
    false_reject_rate = max(
        [*by_consumer.values(), *snot_by_consumer.values()], default=0.0
    )
    verdict_agreement = (
        tally.verdict_agree / tally.verdict_pairs if tally.verdict_pairs else None
    )
    nonconverged_share = tally.nonconverged / tally.solves if tally.solves else 0.0
    return {
        "speed_ratio_largest": speed_ratio,
        "speed_ratio_pass": speed_ratio <= _THRESHOLDS["speed_ratio_largest_max"],
        "speed_ratio_kill": speed_ratio > _THRESHOLDS["speed_ratio_kill"],
        "setup_share": setup_share,
        "setup_share_pass": setup_share <= _THRESHOLDS["setup_share_max"],
        "ranking_agreement": ranking_agreement,
        "snot_ranking_agreement": (snot.agree / snot.pairs if snot.pairs else None),
        "ranking_pass": (
            ranking_agreement is not None
            and ranking_agreement >= _THRESHOLDS["ranking_agreement_min"]
        ),
        "ranking_kill": (
            ranking_agreement is not None
            and ranking_agreement < _THRESHOLDS["ranking_agreement_kill"]
        ),
        "confident_false_reject_rate": false_reject_rate,
        "confident_false_reject_by_consumer": by_consumer,
        "snot_false_reject_by_consumer": snot_by_consumer,
        "snot_gated": snot.gated,
        "false_reject_pass": (
            false_reject_rate <= _THRESHOLDS["confident_false_reject_max"]
        ),
        "verdict_agreement": verdict_agreement,
        "verdict_pass": (
            verdict_agreement is not None
            and verdict_agreement >= _THRESHOLDS["verdict_agreement_min"]
        ),
        "candidate_verdict_agreement": (
            tally.candidate_agree / tally.candidate_pairs
            if tally.candidate_pairs
            else None
        ),
        "nonconverged_share": nonconverged_share,
        "nonconverged_pass": (
            nonconverged_share <= _THRESHOLDS["nonconverged_share_max"]
        ),
        "nonconverged_kill": (
            nonconverged_share > _THRESHOLDS["nonconverged_share_kill"]
        ),
    }


def main() -> int:
    """Run the gate measurements and write the artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radii", type=int, nargs="+", default=[8, 10, 12, 14])
    parser.add_argument("--candidates", type=int, default=30)
    parser.add_argument(
        "--candidate-radii",
        type=int,
        nargs="+",
        default=None,
        help="radii to run the candidate harness on (default: two smallest)",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--skip-corpus", action="store_true")
    parser.add_argument("--skip-snot", action="store_true")
    parser.add_argument(
        "--fields",
        choices=["restricted", "bricksim"],
        default="restricted",
        help=(
            "screen basis to measure; 'bricksim' is the paper's "
            "friction-pyramid research basis (its q is a utilization, "
            "not a max_score — compare artifacts, not thresholds)"
        ),
    )
    parser.add_argument(
        "--skip-maximin",
        action="store_true",
        help=(
            "drop the Luo-maximin acceptance rule from the false-reject "
            "measurement (it costs one extra maximin LP per candidate; "
            "maximin is Luo's default acceptance, so the gate number is "
            "incomplete without it)"
        ),
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    config = SolverConfig(screen_fields=args.fields)
    tally = _Tally()
    shells: list[_ShellRow] = []
    for radius in args.radii:
        row = _shell_row(radius, args.seed, config, tally)
        shells.append(row)
        print(
            f"shell r={radius}: {row.bricks} bricks, "
            f"cold {row.cold_seconds:.2f}s, screen {row.screen_seconds:.3f}s "
            f"(build {row.screen_build_seconds:.3f}s), "
            f"ratio {row.speed_ratio:.3f}, q {row.screen_q:.3f}"
            f"/{row.cold_q:.3f}, spearman {row.score_spearman:.3f}"
        )

    ranking = _RankingStats(
        config=config,
        seed=args.seed,
        count=args.candidates,
        tally=tally,
        maximin=not args.skip_maximin,
    )
    candidate_radii = args.candidate_radii or sorted(args.radii)[:2]
    for radius in candidate_radii:
        layout = _place_shell(radius, args.seed)
        _rank_shell(layout, ranking)
        print(f"candidates r={radius}: done ({ranking.pairs} scored pairs so far)")

    corpus = [] if args.skip_corpus else _corpus_rows(args.seed, config, tally)
    for row in corpus:
        marker = "" if row["cold_stable"] == row["screen_stable"] else "  <-- DIFF"
        print(
            f"corpus {row['model']}: cold {row['cold_stable']} "
            f"q={row['cold_q']:.3f} | screen {row['screen_stable']} "
            f"q={row['screen_q']:.3f}{marker}"
        )

    snot = [] if args.skip_snot else _snot_rows(args.seed, config, tally)
    for row in snot:
        marker = "" if row["cold_stable"] == row["screen_stable"] else "  <-- DIFF"
        print(
            f"snot {row['model']}: {row['bricks']} bricks "
            f"(+{row['snot_parts']} clad), cold {row['cold_stable']} "
            f"q={row['cold_q']:.3f} | screen {row['screen_stable']} "
            f"q={row['screen_q']:.3f}{marker}"
        )
    if snot:
        # Ranking coverage over lateral geometry: carrier-aware
        # candidates on the first clad layout.
        grid = VoxelGrid.from_array(GENERATORS[_SNOT_MODELS[0]](), plates_per_voxel=3)
        strategy = make_strategy(
            "greedy", catalog=default_catalog(), config=PipelineConfig(seed=args.seed)
        )
        clad = strategy.place(grid, rng=np.random.default_rng(args.seed))
        apply_snot(clad, grid)
        _rank_shell(clad, ranking, domain="snot")
        print(f"candidates snot: done ({ranking.pairs} scored pairs total)")

    verdicts = _verdicts(shells, ranking, tally)
    payload = {
        "schema": 1,
        "generated": datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ"),
        "source": source_identity(_REPO).to_dict(),
        "seed": args.seed,
        "solver_config": {
            "screen_fields": config.screen_fields,
            "screen_margin": config.screen_margin,
            "screen_eps": config.screen_eps,
            "screen_max_iter": config.screen_max_iter,
        },
        "thresholds": _THRESHOLDS,
        "shells": [row.payload() for row in shells],
        "ranking": {
            "domains": {
                name: {
                    "pairs": stats.pairs,
                    "agree": stats.agree,
                    "gated": stats.gated,
                    "false_rejects_by_consumer": stats.false_rejects,
                }
                for name, stats in ranking.domains.items()
            },
            "acceptance_rules": [
                rule
                for rule in _ACCEPTANCE_RULES
                if ranking.maximin or rule != "luo_maximin"
            ],
            "per_shell": ranking.rows,
        },
        "corpus": corpus,
        "snot": snot,
        "verdicts": verdicts,
    }
    out = args.out or (
        _REPO / "eval" / "profiles" / f"{payload['generated']}-screen-bench.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(out, payload)
    print(f"\nartifact: {out}")
    print("verdicts:")
    for key, value in verdicts.items():
        print(f"  {key}: {value}")
    hard_fail = bool(
        verdicts["speed_ratio_kill"]
        or verdicts["ranking_kill"]
        or verdicts["nonconverged_kill"]
    )
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
