"""Sweep StableLego dataset objects and compare verdicts with the release.

The StableLego dataset (Liu et al., RA-L 2024; Google Drive link in the
release README) ships one directory per object holding ``task_graph.json``
and a per-brick ``stability_score.npy``. This harness loads a deterministic
sample, runs our RBE ``analyze`` on each layout, derives the release's
verdict from its score file, and writes an agreement report — the scaled
extension of the nine vendored fixture pins in
``tests/test_stablelego_cross.py``.

Release verdict convention (documented assumption, counted separately when
a file defies it): an object stands iff every per-brick score is finite and
strictly below 1.0. Score files whose length does not match the layout's
brick count are recorded as skipped, never guessed at.

Usage::

    uv run python scripts/stablelego_sweep.py --dataset DIR [--sample 500]
        [--seed 0] [--release-parity] [--library PATH] [--out eval/stablelego]

``--release-parity`` analyzes with ``rotate_contact_pattern=False``
(StableLego release behaviour) instead of the shipped default physics.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from legolization.stability import SolverConfig, analyze
from legolization.stablelego import (
    Library,
    layout_from_task_graph,
    load_library,
    load_task_graph,
    stablelego_catalog,
)

_REPO = Path(__file__).parent.parent

_DEFAULT_LIBRARY = _REPO / "tests" / "data" / "stablelego" / "lego_library.json"


@dataclass(frozen=True, slots=True)
class ObjectRow:
    """One object's verdict comparison."""

    name: str
    bricks: int
    ours_stable: bool
    ours_max_score: float
    theirs_stable: bool
    theirs_max_score: float

    @property
    def agree(self) -> bool:
        """Whether both analyses reach the same stand/collapse verdict."""
        return self.ours_stable == self.theirs_stable


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the sweep CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--sample", type=int, default=500, metavar="N")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--release-parity", action="store_true")
    parser.add_argument("--library", type=Path, default=_DEFAULT_LIBRARY)
    parser.add_argument("--out", type=Path, default=_REPO / "eval" / "stablelego")
    return parser.parse_args(argv)


def discover_objects(dataset: Path) -> list[Path]:
    """Every object directory carrying the release's two files, sorted."""
    return sorted(
        path
        for path in dataset.iterdir()
        if (path / "task_graph.json").exists()
        and (path / "stability_score.npy").exists()
    )


def sample_objects(objects: list[Path], sample: int, seed: int) -> list[Path]:
    """Pick a deterministic subset; ``sample=0`` keeps everything."""
    if sample <= 0 or sample >= len(objects):
        return objects
    rng = np.random.default_rng(seed)
    chosen = rng.choice(len(objects), size=sample, replace=False)
    return [objects[i] for i in sorted(chosen)]


def release_verdict(scores: np.ndarray) -> tuple[bool, float]:
    """(stands, max score) under the documented release convention."""
    finite = np.isfinite(scores)
    max_score = float(scores[finite].max()) if finite.any() else float("inf")
    return bool(finite.all() and max_score < 1.0), max_score


def evaluate_object(
    path: Path,
    *,
    library: Library,
    config: SolverConfig,
) -> ObjectRow | str:
    """One comparison row, or a skip reason string."""
    try:
        entries = load_task_graph(path / "task_graph.json")
        layout = layout_from_task_graph(
            entries,
            catalog=stablelego_catalog(library),
            library=library,
        )
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        return f"{path.name}: load failed: {error}"
    theirs = np.asarray(np.load(path / "stability_score.npy"), dtype=float).ravel()
    if theirs.size != len(layout):
        return f"{path.name}: score length {theirs.size} != {len(layout)} bricks"
    theirs_stable, theirs_max = release_verdict(theirs)
    ours = analyze(layout, config)
    return ObjectRow(
        name=path.name,
        bricks=len(layout),
        ours_stable=ours.stable,
        ours_max_score=round(ours.max_score, 9),
        theirs_stable=theirs_stable,
        theirs_max_score=round(theirs_max, 9) if np.isfinite(theirs_max) else 1.0e9,
    )


def to_markdown(rows: list[ObjectRow], skipped: list[str]) -> str:
    """Human-readable report: disagreements first, then the tallies."""
    lines = ["# StableLego verdict sweep", ""]
    disagreements = [row for row in rows if not row.agree]
    lines.append(
        f"objects={len(rows)} agree={len(rows) - len(disagreements)} "
        f"disagree={len(disagreements)} skipped={len(skipped)}"
    )
    lines.append("")
    if disagreements:
        lines.append("| object | bricks | ours | ours max | theirs | theirs max |")
        lines.append("|---|---:|---|---:|---|---:|")
        lines.extend(
            f"| {row.name} | {row.bricks} "
            f"| {'stands' if row.ours_stable else 'collapses'} "
            f"| {row.ours_max_score:.4f} "
            f"| {'stands' if row.theirs_stable else 'collapses'} "
            f"| {row.theirs_max_score:.4f} |"
            for row in disagreements
        )
        lines.append("")
    lines.extend(f"- skipped {reason}" for reason in skipped)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    if not args.dataset.is_dir():
        print(f"error: --dataset {args.dataset} is not a directory", file=sys.stderr)
        return 1
    objects = discover_objects(args.dataset)
    if not objects:
        print("error: no objects with task_graph.json found", file=sys.stderr)
        return 1
    chosen = sample_objects(objects, args.sample, args.seed)
    library = load_library(args.library)
    config = (
        SolverConfig(rotate_contact_pattern=False)
        if args.release_parity
        else SolverConfig()
    )
    rows: list[ObjectRow] = []
    skipped: list[str] = []
    for index, path in enumerate(chosen, start=1):
        outcome = evaluate_object(path, library=library, config=config)
        if isinstance(outcome, str):
            skipped.append(outcome)
        else:
            rows.append(outcome)
        if sys.stderr.isatty():
            print(f"\r{index}/{len(chosen)}", end="", file=sys.stderr)
    if sys.stderr.isatty():
        print(file=sys.stderr)
    if not rows:
        print("error: every sampled object was skipped", file=sys.stderr)
        return 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": stamp,
        "dataset": str(args.dataset),
        "seed": args.seed,
        "sample": args.sample,
        "release_parity": args.release_parity,
        "agree": sum(row.agree for row in rows),
        "disagree": sum(not row.agree for row in rows),
        "skipped": skipped,
        "rows": [dict(asdict(row), agree=row.agree) for row in rows],
    }
    (out_dir / "report.json").write_text(json.dumps(payload, indent=2) + "\n")
    (out_dir / "report.md").write_text(to_markdown(rows, skipped) + "\n")
    print(f"wrote {out_dir / 'report.json'}")
    print(to_markdown(rows, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
