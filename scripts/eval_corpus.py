"""Sweep the evaluation corpus through every strategy and score the field.

For each manifest model (see ``scripts/corpus.py``) this runs the full
``--strategy all`` machinery in-process (``compare.run_all`` +
``select_best``), collects a scorecard row, and writes
``eval/runs/<UTC>/scorecard.{json,md}``. With a committed baseline
(``eval/baselines/scorecard.json``) it also reports regressions: a drop in
buildable-strategy count, a newly failed manifest expectation, or a winner
objective that worsened beyond ``--tolerance`` are HARD regressions (exit
1); winner identity and brick-count drift are informational.

Usage::

    uv run python scripts/eval_corpus.py [--models a,b] [--traits fast]
        [--kind mesh|synthetic] [--strategies greedy,fast] [--jobs 0]
        [--timeout 300] [--seed 0] [--write-baseline] [--tolerance 0.05]

Timings are never compared. Synthetic models are regenerated in memory
(never stale); mesh models missing from disk are recorded as skipped -
run ``scripts/corpus.py download`` first for full coverage.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from legolization.compare import run_all, select_best
from legolization.grid import VoxelGrid
from legolization.mesh import MeshOptions, mesh_to_grid
from legolization.pipeline import PipelineConfig

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
BASELINE = _REPO / "eval" / "baselines" / "scorecard.json"
RUNS = _REPO / "eval" / "runs"


class CorpusModelLike(Protocol):
    """Shape of scripts/corpus.py's CorpusModel (loaded dynamically)."""

    name: str
    kind: str
    traits: tuple[str, ...]
    expect_min_buildable: int
    plates_per_voxel: int
    target_studs: int | None
    up: Literal["x", "y", "z"] | None
    generator: str | None

    @property
    def abs_path(self) -> Path:
        """Absolute on-disk location of this model's file."""
        ...


def load_corpus_module() -> ModuleType:
    """Import the neighbouring corpus.py script as a module."""
    spec = importlib.util.spec_from_file_location(
        "corpus_script", _SCRIPTS / "corpus.py"
    )
    if spec is None or spec.loader is None:
        msg = "cannot load scripts/corpus.py"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def model_grid(corpus: ModuleType, model: CorpusModelLike) -> VoxelGrid | None:
    """Materialize a manifest model as a grid (None = mesh not on disk)."""
    match model.kind:
        case "synthetic":
            codes = corpus.GENERATORS[model.generator]()
            return VoxelGrid.from_array(codes, plates_per_voxel=model.plates_per_voxel)
        case _:
            if not model.abs_path.exists():
                return None
            return mesh_to_grid(
                model.abs_path,
                options=MeshOptions(
                    target_studs=model.target_studs or 32,
                    up=model.up or "z",
                ),
            )


def build_row(
    model: CorpusModelLike,
    report_dict: dict | None,
    status: str,
) -> dict:
    """Assemble one JSON-safe scorecard row."""
    row: dict = {
        "model": model.name,
        "kind": model.kind,
        "traits": list(model.traits),
        "status": status,
        "expect_min_buildable": model.expect_min_buildable,
        "buildable_count": 0,
        "expectation_ok": model.expect_min_buildable == 0,
        "winner": None,
        "buildable": False,
        "objective_total": None,
        "brick_count": None,
        "max_score": None,
        "maximin_capacity": None,
        "candidates": [],
    }
    if report_dict is None:
        return row
    candidates = report_dict["candidates"]
    buildable_count = sum(
        1
        for candidate in candidates
        if candidate["metrics"] is not None and candidate["metrics"]["buildable"]
    )
    row.update(
        {
            "buildable_count": buildable_count,
            "expectation_ok": buildable_count >= model.expect_min_buildable,
            "winner": report_dict["winner"],
            "reason": report_dict["reason"],
            "buildable": report_dict["buildable"],
            "candidates": candidates,
        }
    )
    winner = next(
        (c for c in candidates if c["strategy"] == report_dict["winner"]),
        None,
    )
    if winner is not None and winner["metrics"] is not None:
        metrics = winner["metrics"]
        row.update(
            {
                "objective_total": round(metrics["objective_total"], 4),
                "brick_count": metrics["brick_count"],
                "max_score": round(metrics["max_score"], 4),
                "maximin_capacity": round(metrics["maximin_capacity"], 4),
            }
        )
    return row


def compare_to_baseline(
    rows: list[dict],
    baseline_rows: list[dict],
    *,
    tolerance: float,
) -> tuple[list[str], list[str]]:
    """Diff a run against the baseline; returns (hard, informational)."""
    hard: list[str] = []
    info: list[str] = []
    baseline_by_name = {row["model"]: row for row in baseline_rows}
    for row in rows:
        name = row["model"]
        old = baseline_by_name.get(name)
        if old is None:
            info.append(f"{name}: new model (not in baseline)")
            continue
        if row["buildable_count"] < old["buildable_count"]:
            hard.append(
                f"{name}: buildable strategies dropped "
                f"{old['buildable_count']} -> {row['buildable_count']}"
            )
        if old["expectation_ok"] and not row["expectation_ok"]:
            hard.append(f"{name}: manifest expectation newly failing")
        old_objective = old.get("objective_total")
        new_objective = row.get("objective_total")
        if (
            old_objective is not None
            and new_objective is not None
            and row["buildable"]
            and old["buildable"]
            and new_objective > old_objective + abs(old_objective) * tolerance
        ):
            hard.append(
                f"{name}: winner objective worsened "
                f"{old_objective:.4f} -> {new_objective:.4f}"
            )
        if row["winner"] != old["winner"]:
            info.append(f"{name}: winner {old['winner']} -> {row['winner']}")
        if row.get("brick_count") != old.get("brick_count"):
            info.append(
                f"{name}: bricks {old.get('brick_count')} -> {row.get('brick_count')}"
            )
    info.extend(
        f"{name}: in baseline but not in this run"
        for name in baseline_by_name.keys() - {row["model"] for row in rows}
    )
    return hard, info


def to_markdown(rows: list[dict]) -> str:
    """Render scorecard rows as a markdown table."""
    header = (
        "| model | status | winner | buildable | objective | bricks "
        "| max_score | expectation |"
    )
    lines = [header, "|---|---|---|---|---|---|---|---|"]
    lines.extend(
        f"| {row['model']} | {row['status']} | {row['winner']} "
        f"| {row['buildable_count']} | {row['objective_total']} "
        f"| {row['brick_count']} | {row['max_score']} "
        f"| {'PASS' if row['expectation_ok'] else 'FAIL'} |"
        for row in rows
    )
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=None, metavar="NAME,...")
    parser.add_argument("--traits", default=None, metavar="TRAIT,...")
    parser.add_argument("--kind", choices=("mesh", "synthetic"), default=None)
    parser.add_argument("--strategies", default=None, metavar="NAME,...")
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=RUNS)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--tolerance", type=float, default=0.05)
    return parser.parse_args(argv)


def _sweep(
    corpus: ModuleType,
    models: list[CorpusModelLike],
    args: argparse.Namespace,
) -> list[dict]:
    """Run the strategy sweep for every selected model."""
    names = (
        tuple(name.strip() for name in args.strategies.split(","))
        if args.strategies is not None
        else None
    )
    rows: list[dict] = []
    for model in models:
        print(f"=== {model.name}", file=sys.stderr)
        try:
            grid = model_grid(corpus, model)
        except ValueError as error:
            rows.append(build_row(model, None, f"error: {error}"))
            continue
        if grid is None:
            rows.append(build_row(model, None, "skipped: mesh not on disk (download)"))
            continue
        candidates = run_all(
            grid,
            PipelineConfig(seed=args.seed),
            jobs=args.jobs,
            names=names,
            timeout_s=args.timeout,
            progress=lambda message: print(f"  {message}", file=sys.stderr),
        )
        report = select_best(candidates)
        status = "ok" if report.winner is not None else "error: all failed"
        rows.append(build_row(model, report.to_dict(), status))
    return rows


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _parse_args(argv)
    corpus = load_corpus_module()
    models = corpus.select_models(corpus.load_manifest(), args.models)
    if args.traits is not None:
        wanted = {trait.strip() for trait in args.traits.split(",")}
        models = [m for m in models if wanted & set(m.traits)]
    if args.kind is not None:
        models = [m for m in models if m.kind == args.kind]
    if not models:
        print("error: no corpus models selected", file=sys.stderr)
        return 1
    rows = _sweep(corpus, models, args)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"seed": args.seed, "generated": stamp, "models": rows}
    (out_dir / "scorecard.json").write_text(json.dumps(payload, indent=2) + "\n")
    (out_dir / "scorecard.md").write_text(to_markdown(rows) + "\n")
    print(f"wrote {out_dir / 'scorecard.json'}")
    print(to_markdown(rows))

    exit_code = 0
    if not all(row["expectation_ok"] for row in rows):
        failed = [row["model"] for row in rows if not row["expectation_ok"]]
        print(f"expectation failures: {', '.join(failed)}")
        exit_code = 1
    if args.baseline.exists() and not args.write_baseline:
        baseline_rows = json.loads(args.baseline.read_text())["models"]
        hard, info = compare_to_baseline(rows, baseline_rows, tolerance=args.tolerance)
        for line in info:
            print(f"note: {line}")
        for line in hard:
            print(f"REGRESSION: {line}")
        if hard:
            exit_code = 1
    if args.write_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {args.baseline}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
