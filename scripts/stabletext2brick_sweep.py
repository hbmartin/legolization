"""Sweep StableText2Brick and compare verdicts and scores with the release.

The sibling of ``scripts/stablelego_sweep.py``, against a much larger and
differently-labelled release. What it adds over that sweep:

1. **A positive-only regression at scale.** Every one of the 47,389 rows passed
   Liu et al.'s analysis during dataset construction, so any ``unstable`` verdict
   from us is a divergence worth naming. This bounds false negatives; StableLego's
   mixed valid/invalid release bounds the other direction.
2. **Numeric score agreement, not just binary.** The release ships a per-voxel
   stability *margin*; we produce a per-brick *stress*. They point opposite ways
   (see ``legolization.datasets.stabletext2brick``), so the sweep reports the
   residual ``|(1 - their worst margin) - our max_score|`` and its distribution
   rather than asserting the two scales are the same quantity.
3. **A BrickSim-comparable number.** ``references/bricksim-*/paper.md`` line 194
   states BrickSim's evaluation set is "150 assemblies randomly sampled from
   StableText2Brick", scoring 150/150 solvable (line 200). ``--sample 150``
   produces a directly comparable figure.
4. **Screen agreement at scale.** ``--screen bricksim`` runs the same sweep
   through the reduced-QP candidate screen, which has no large-sample agreement
   measurement today.

Usage::

    uv run python scripts/stabletext2brick_sweep.py [--sample 150] [--seed 0]
        [--screen bricksim] [--dataset DIR] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from legolization.datasets import stabletext2brick as s2b
from legolization.stability import SolverConfig, analyze

if TYPE_CHECKING:
    from collections.abc import Sequence

_REPO = Path(__file__).parent.parent
_DEFAULT_DATASET = _REPO / "datasets" / "stabletext2brick"
_DEFAULT_OUT = _REPO / "eval" / "datasets" / "stabletext2brick"


@dataclass(frozen=True, slots=True)
class StructureRow:
    """One structure's verdict from both sides."""

    structure_id: str
    category_id: str
    bricks: int
    ours_stable: bool
    ours_max_score: float
    theirs_stands: bool
    theirs_margin: float

    @property
    def agree(self) -> bool:
        """Whether the two verdicts match."""
        return self.ours_stable == self.theirs_stands

    @property
    def residual(self) -> float:
        """``|(1 - their margin) - our max_score|``.

        Not a claim that the two scales are the same quantity - it is the
        measurement of whether they behave as if they were.
        """
        return abs((1.0 - self.theirs_margin) - self.ours_max_score)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET)
    parser.add_argument(
        "--sample",
        type=int,
        default=500,
        help="structures to evaluate (0 = every row); 150 matches BrickSim",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--screen", choices=["off", "bricksim"], default="off")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    return parser.parse_args(argv)


def evaluate(structure: s2b.Structure, *, config: SolverConfig) -> StructureRow | str:
    """Analyse one structure, returning a skip reason instead of raising."""
    try:
        layout = s2b.layout_from_bricks(structure.bricks)
    except (KeyError, ValueError) as error:
        return f"{structure.structure_id}: layout failed: {error}"
    try:
        result = analyze(layout, config=config)
    except (ArithmeticError, RuntimeError, ValueError) as error:
        return f"{structure.structure_id}: analysis failed: {error}"
    stands, margin = s2b.release_verdict(structure)
    return StructureRow(
        structure_id=structure.structure_id,
        category_id=structure.category_id,
        bricks=len(structure.bricks),
        ours_stable=bool(result.stable),
        ours_max_score=float(result.max_score),
        theirs_stands=stands,
        theirs_margin=margin,
    )


def summarize(rows: Sequence[StructureRow]) -> dict[str, float | int | None]:
    """Aggregate agreement counts and the score-residual distribution.

    An undefined correlation (one row, or a constant series) is ``None``, not
    ``float("nan")``: ``json.dumps`` writes NaN as a bare ``NaN`` token, which
    is not valid JSON and strict consumers reject the whole report.
    """
    if not rows:
        return {}
    residuals = np.array([row.residual for row in rows])
    ours = np.array([row.ours_max_score for row in rows])
    theirs = np.array([1.0 - row.theirs_margin for row in rows])
    correlation = (
        float(np.corrcoef(ours, theirs)[0, 1])
        if ours.size > 1 and ours.std() > 0 and theirs.std() > 0
        else None
    )
    return {
        "structures": len(rows),
        "agree": sum(1 for row in rows if row.agree),
        "disagree": sum(1 for row in rows if not row.agree),
        "ours_unstable": sum(1 for row in rows if not row.ours_stable),
        "theirs_unstable": sum(1 for row in rows if not row.theirs_stands),
        "residual_mean": float(residuals.mean()),
        "residual_median": float(np.median(residuals)),
        "residual_p95": float(np.percentile(residuals, 95)),
        "residual_max": float(residuals.max()),
        "stress_correlation": correlation,
    }


def to_markdown(
    rows: Sequence[StructureRow],
    skipped: Sequence[str],
    summary: dict[str, float | int | None],
    *,
    screen: str,
) -> str:
    """Render the human-readable report."""
    lines = [
        "# StableText2Brick verdict sweep",
        "",
        (
            f"structures={summary.get('structures', 0)} "
            f"agree={summary.get('agree', 0)} "
            f"disagree={summary.get('disagree', 0)} "
            f"skipped={len(skipped)} "
            f"screen={screen}"
        ),
        "",
        "## Conventions (read before comparing numbers)",
        "",
        "The release's `stability_scores` is a **margin**: higher is better, 1.0",
        "is effortless, unoccupied voxels are padded with 1.0, and its own rule is",
        "`stable iff every brick scores > 0`. Our `max_score` is a **stress**:",
        "higher is worse, `>= 1.0` is at or past capacity. The two point in",
        "**opposite** directions. `residual` below is",
        "`|(1 - their worst occupied margin) - our max_score|` - a measurement of",
        "how closely the two behave like the same quantity, not a claim that they",
        "are.",
        "",
        "**This set is positive-only.** Every row was filtered to stable during",
        "dataset construction, so a disagreement here can only be us calling a",
        "stable structure unstable. False positives are not testable from this",
        "data; use the StableLego sweep for that direction.",
        "",
    ]
    if summary:
        correlation = summary["stress_correlation"]
        lines += [
            "## Score agreement",
            "",
            f"- residual mean   {summary['residual_mean']:.6f}",
            f"- residual median {summary['residual_median']:.6f}",
            f"- residual p95    {summary['residual_p95']:.6f}",
            f"- residual max    {summary['residual_max']:.6f}",
            (
                "- correlation of (1 - margin) with max_score: "
                + (f"{correlation:.6f}" if isinstance(correlation, float) else "n/a")
            ),
            "",
        ]
    if disagreements := [row for row in rows if not row.agree]:
        lines += [
            "## Disagreements",
            "",
            "| structure | category | bricks | ours | ours max | theirs | margin |",
            "| --- | --- | ---: | --- | ---: | --- | ---: |",
        ]
        lines += [
            f"| {row.structure_id} | {row.category_id} | {row.bricks} | "
            f"{'stable' if row.ours_stable else 'collapses'} | "
            f"{row.ours_max_score:.4f} | "
            f"{'stands' if row.theirs_stands else 'collapses'} | "
            f"{row.theirs_margin:.4f} |"
            for row in disagreements
        ]
        lines.append("")
    if worst := sorted(rows, key=lambda row: -row.residual)[:10]:
        lines += [
            "## Largest score residuals",
            "",
            "| structure | bricks | ours max | 1 - margin | residual |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        lines += [
            f"| {row.structure_id} | {row.bricks} | {row.ours_max_score:.4f} | "
            f"{1.0 - row.theirs_margin:.4f} | {row.residual:.4f} |"
            for row in worst
        ]
        lines.append("")
    if skipped:
        lines += ["## Skipped", ""]
        lines += [f"- {reason}" for reason in skipped]
        lines.append("")
    return "\n".join(lines)


def run_sweep(
    loaded: Sequence[s2b.Structure | str],
    *,
    config: SolverConfig,
) -> tuple[list[StructureRow], list[str]]:
    """Analyse every loaded structure, partitioning results from skip reasons."""
    rows: list[StructureRow] = []
    skipped: list[str] = []
    for position, item in enumerate(loaded, start=1):
        match item:
            case str() as reason:
                skipped.append(reason)
            case s2b.Structure() as structure:
                match evaluate(structure, config=config):
                    case str() as reason:
                        skipped.append(reason)
                    case StructureRow() as row:
                        rows.append(row)
        if sys.stderr.isatty():
            print(f"\r{position}/{len(loaded)}", end="", file=sys.stderr)
    if sys.stderr.isatty():
        print(file=sys.stderr)
    return rows, skipped


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sweep and write the report pair."""
    args = parse_args(argv)
    if not args.dataset.is_dir():
        print(f"{args.dataset} is not a directory", file=sys.stderr)
        return 1
    if not (paths := s2b.shard_paths(args.dataset)):
        print(f"no parquet shards under {args.dataset}", file=sys.stderr)
        return 1

    counts = s2b.shard_row_counts(paths)
    indices = s2b.sample_indices(counts, sample=args.sample, seed=args.seed)
    config = SolverConfig(screen=args.screen)

    rows, skipped = run_sweep(
        s2b.load_selected(paths=paths, counts=counts, indices=indices),
        config=config,
    )
    if not rows:
        print("every sampled structure was skipped", file=sys.stderr)
        return 1

    summary = summarize(rows)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(
            {
                "generated": stamp,
                "dataset": str(args.dataset),
                "seed": args.seed,
                "sample": args.sample,
                "screen": args.screen,
                "summary": summary,
                "skipped": skipped,
                "rows": [
                    asdict(row) | {"agree": row.agree, "residual": row.residual}
                    for row in rows
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown = to_markdown(rows, skipped, summary, screen=args.screen)
    (out / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
