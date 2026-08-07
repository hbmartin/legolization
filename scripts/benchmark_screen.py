"""Measure the reduced-QP screen against the exact LP (the Phase-3 gate).

Three sections, mirroring the pre-registered acceptance thresholds in
docs/performance-testing.md:

1. **Shell series** — thin-shell greedy layouts at increasing radius
   (the same hollow-shell family as the pinned 348/505/712/902-brick
   rows, re-baselined same-session per the section-2 protocol): cold
   ``analyze`` wall vs screen wall (build/solve split from telemetry),
   verdict agreement, per-brick score Spearman, weakest-brick match.
2. **Candidate ranking** — K brick-removal perturbations per shell:
   pairwise-order agreement between screen q and cold q on pairs
   separated by more than the screen margin, plus the
   confident-false-reject rate at the actual certify gate.
3. **Corpus spread** — every synthetic corpus shape, greedy at seed 0:
   verdict agreement across non-shell topologies.

Writes ``eval/profiles/<UTC>-screen-bench.json`` and prints the
threshold verdicts.

Usage::

    uv run python scripts/benchmark_screen.py [--radii 8 10 12 14]
        [--candidates 30] [--seed 0] [--skip-corpus]
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from scipy import stats

from legolization import telemetry
from legolization.catalog import default_catalog
from legolization.corpus.generators import GENERATORS, thin_shell
from legolization.eval_artifacts import atomic_json, source_identity
from legolization.grid import VoxelGrid
from legolization.pipeline import PipelineConfig
from legolization.placement.registry import make_strategy
from legolization.stability import SolverConfig, analyze
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
    """Running screen-status and verdict-agreement counters."""

    solves: int = 0
    nonconverged: int = 0
    verdict_pairs: int = 0
    verdict_agree: int = 0

    def status(self, report: ScreenReport) -> None:
        self.solves += 1
        if report.status == "nonconverged":
            self.nonconverged += 1

    def verdict(self, report: ScreenReport, cold: StabilityResult) -> None:
        self.verdict_pairs += 1
        if report.status == "ok" and report.stable == cold.stable:
            self.verdict_agree += 1


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


def _candidates(layout: Layout, count: int, rng: np.random.Generator) -> list[Layout]:
    ids = sorted(layout.bricks)
    out: list[Layout] = []
    for _ in range(count):
        removal = rng.choice(ids, size=int(rng.integers(1, 4)), replace=False)
        keep = frozenset(ids) - {int(b) for b in removal}
        out.append(layout.subset(keep))
    return out


@dataclass(slots=True)
class _RankingStats:
    """Candidate-harness accumulators and shared run context."""

    config: SolverConfig
    seed: int
    count: int
    tally: _Tally
    pairs: int = 0
    agree: int = 0
    gated: int = 0
    false_rejects: int = 0
    rows: list[dict[str, object]] = field(default_factory=list)


def _rank_shell(layout: Layout, stats_acc: _RankingStats) -> None:
    config = stats_acc.config
    tally = stats_acc.tally
    rng = np.random.default_rng(stats_acc.seed + 1)
    base_cold = analyze(layout, config)
    screen = ReducedScreen.create(layout, config)
    if screen is None:
        return
    tally.status(screen.baseline)
    pairs_q: list[tuple[float, float]] = []
    for candidate in _candidates(layout, stats_acc.count, rng):
        cold = analyze(candidate, config)
        report, _, _ = _screen_with_split(candidate, config)
        tally.status(report)
        tally.verdict(report, cold)
        if report.status != "ok":
            continue
        pairs_q.append((cold.max_score, report.q))
        # The PRODUCTION gate, not a re-implementation.
        gate_reject = screen.should_reject(report, config.screen_margin)
        cold_improves = cold.max_score < base_cold.max_score
        if gate_reject:
            stats_acc.gated += 1
            if cold_improves and cold.stable:
                stats_acc.false_rejects += 1
    for i in range(len(pairs_q)):
        for j in range(i + 1, len(pairs_q)):
            gap = abs(pairs_q[i][0] - pairs_q[j][0])
            if gap <= config.screen_margin:
                continue
            stats_acc.pairs += 1
            cold_order = pairs_q[i][0] < pairs_q[j][0]
            screen_order = pairs_q[i][1] < pairs_q[j][1]
            if cold_order == screen_order:
                stats_acc.agree += 1
    stats_acc.rows.append(
        {
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
    ranking_agreement = ranking.agree / ranking.pairs if ranking.pairs else None
    false_reject_rate = ranking.false_rejects / ranking.gated if ranking.gated else 0.0
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
        "ranking_pass": (
            ranking_agreement is not None
            and ranking_agreement >= _THRESHOLDS["ranking_agreement_min"]
        ),
        "ranking_kill": (
            ranking_agreement is not None
            and ranking_agreement < _THRESHOLDS["ranking_agreement_kill"]
        ),
        "confident_false_reject_rate": false_reject_rate,
        "false_reject_pass": (
            false_reject_rate <= _THRESHOLDS["confident_false_reject_max"]
        ),
        "verdict_agreement": verdict_agreement,
        "verdict_pass": (
            verdict_agreement is not None
            and verdict_agreement >= _THRESHOLDS["verdict_agreement_min"]
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
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    config = SolverConfig()
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
        config=config, seed=args.seed, count=args.candidates, tally=tally
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

    verdicts = _verdicts(shells, ranking, tally)
    payload = {
        "schema": 1,
        "generated": datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ"),
        "source": source_identity(_REPO).to_dict(),
        "seed": args.seed,
        "solver_config": {
            "screen_margin": config.screen_margin,
            "screen_eps": config.screen_eps,
            "screen_max_iter": config.screen_max_iter,
        },
        "thresholds": _THRESHOLDS,
        "shells": [row.payload() for row in shells],
        "ranking": {
            "pairs": ranking.pairs,
            "agree": ranking.agree,
            "gated": ranking.gated,
            "false_rejects": ranking.false_rejects,
            "per_shell": ranking.rows,
        },
        "corpus": corpus,
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
