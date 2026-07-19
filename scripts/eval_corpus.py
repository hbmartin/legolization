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
MESH_BASELINE = _REPO / "eval" / "baselines" / "scorecard-mesh.json"
_BASELINE_BY_KIND = {"synthetic": BASELINE, "mesh": MESH_BASELINE}
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
    largest_component_only: bool

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
                    keep_largest=model.largest_component_only,
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
    buildable_strategies = {
        candidate["strategy"]
        for candidate in candidates
        if candidate["metrics"] is not None and candidate["metrics"]["buildable"]
    }
    buildable_count = len(buildable_strategies)
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
    _add_seed_spread(row, report_dict, candidates)
    winner = next(
        (
            c
            for c in candidates
            if c["strategy"] == report_dict["winner"]
            and c.get("seed", 0) == report_dict.get("winner_seed", c.get("seed", 0))
        ),
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


def _add_seed_spread(row: dict, report_dict: dict, candidates: list[dict]) -> None:
    """Attach multi-seed spread stats (what each single-seed run would give)."""
    seeds_present = sorted({c.get("seed", 0) for c in candidates})
    if len(seeds_present) <= 1:
        return
    per_seed_best: list[dict] = []
    buildable_seeds: list[int] = []
    for seed in seeds_present:
        seed_metrics = [
            c["metrics"]
            for c in candidates
            if c.get("seed", 0) == seed
            and c["metrics"] is not None
            and c["metrics"]["buildable"]
        ]
        if seed_metrics:
            buildable_seeds.append(seed)
            per_seed_best.append(
                min(
                    seed_metrics,
                    key=lambda m: (
                        m["objective_total"],
                        -m["maximin_capacity"],
                        m["brick_count"],
                    ),
                )
            )
    row["seeds"] = seeds_present
    row["winner_seed"] = report_dict.get("winner_seed")
    if per_seed_best:
        row["seed_spread"] = {
            "buildable_seeds": buildable_seeds,
            "brick_min": min(m["brick_count"] for m in per_seed_best),
            "brick_max": max(m["brick_count"] for m in per_seed_best),
            "objective_min": round(min(m["objective_total"] for m in per_seed_best), 4),
            "objective_max": round(max(m["objective_total"] for m in per_seed_best), 4),
        }


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


def _seed_list(value: str) -> tuple[int, ...]:
    """Parse a comma-separated seed list for argparse."""
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        msg = f"{value!r} must be comma-separated integers"
        raise argparse.ArgumentTypeError(msg)
    try:
        return tuple(int(part) for part in parts)
    except ValueError as error:
        msg = f"{value!r} must be comma-separated integers"
        raise argparse.ArgumentTypeError(msg) from error


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the evaluator CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=None, metavar="NAME,...")
    parser.add_argument("--traits", default=None, metavar="TRAIT,...")
    parser.add_argument("--kind", choices=("mesh", "synthetic"), default=None)
    parser.add_argument("--strategies", default=None, metavar="NAME,...")
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--seeds", type=_seed_list, default=None, metavar="N,N,...")
    parser.add_argument("--out", type=Path, default=RUNS)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--tolerance", type=float, default=0.05)
    return parser.parse_args(argv)


def _validate_baseline_scope(args: argparse.Namespace) -> None:
    """Require the committed baselines' canonical, reproducible sweep scope.

    Each corpus kind owns one committed baseline file (synthetic:
    ``scorecard.json``, mesh: ``scorecard-mesh.json``); a write must be a
    full-kind, seed-0, unfiltered sweep so the file stays reproducible.
    """
    filtered = any(
        value is not None
        for value in (args.models, args.traits, args.strategies, args.seeds)
    )
    if args.write_baseline and (
        args.kind not in _BASELINE_BY_KIND or args.seed != 0 or filtered
    ):
        msg = (
            "--write-baseline requires an unfiltered --kind synthetic (or mesh) "
            "sweep with --seed 0 and no --seeds"
        )
        raise SystemExit(msg)


def baseline_write_path(args: argparse.Namespace) -> Path:
    """Resolve the canonical baseline file this run may write."""
    if args.baseline is not None:
        return args.baseline
    return _BASELINE_BY_KIND[args.kind]


def baseline_rows(args: argparse.Namespace) -> list[dict] | None:
    """Baseline rows to diff against, or None when no baseline exists.

    An explicit ``--baseline`` names one file; otherwise every committed
    per-kind baseline that exists contributes rows (models are keyed by
    name, so a mixed-kind sweep diffs each model against its own kind).
    """
    paths = (
        [args.baseline]
        if args.baseline is not None
        else list(_BASELINE_BY_KIND.values())
    )
    rows: list[dict] = []
    found = False
    for path in paths:
        if path.exists():
            found = True
            rows.extend(json.loads(path.read_text())["models"])
    return rows if found else None


def _assess_rows(rows: list[dict]) -> tuple[int, list[dict]]:
    """Return the status code and successful physics-verdict rows."""
    evaluated_rows = [row for row in rows if not row["status"].startswith("skipped:")]
    failed_runs = [
        row["model"] for row in evaluated_rows if row["status"].startswith("error:")
    ]
    successful_rows = [row for row in evaluated_rows if row["status"] == "ok"]
    expectation_failures = [
        row["model"] for row in successful_rows if not row["expectation_ok"]
    ]
    if failed_runs:
        print(f"evaluation failures: {', '.join(failed_runs)}")
    if expectation_failures:
        print(f"expectation failures: {', '.join(expectation_failures)}")
    return int(bool(failed_runs or expectation_failures)), successful_rows


def _baseline_regression_status(
    args: argparse.Namespace,
    rows: list[dict],
) -> int:
    """Compare successful rows with the baseline and return an exit status."""
    if args.write_baseline or (known := baseline_rows(args)) is None:
        return 0
    if args.seeds is not None and len(args.seeds) > 1:
        # An any-seed-buildable run against the seed-0 baseline would mask
        # regressions; spread stats are the multi-seed deliverable instead.
        print("note: multi-seed run; baseline comparison skipped")
        return 0
    hard, info = compare_to_baseline(
        rows=rows,
        baseline_rows=known,
        tolerance=args.tolerance,
    )
    for line in info:
        print(f"note: {line}")
    for line in hard:
        print(f"REGRESSION: {line}")
    return int(bool(hard))


def _write_baseline(
    args: argparse.Namespace,
    payload: dict,
    *,
    exit_code: int,
) -> None:
    """Write a clean canonical baseline, preserving any prior file on failure."""
    if not args.write_baseline:
        return
    if exit_code:
        print("baseline not written because the evaluation failed", file=sys.stderr)
        return
    target = baseline_write_path(args)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {target}")


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
            seeds=args.seeds,
            timeout_s=args.timeout,
            progress=lambda message: print(f"  {message}", file=sys.stderr),
        )
        report = select_best(candidates)
        status = "ok" if report.winner is not None else "error: all failed"
        rows.append(build_row(model, report.to_dict(), status))
    return rows


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    _validate_baseline_scope(args)
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
    payload = {
        "seed": args.seed,
        "seeds": list(args.seeds) if args.seeds is not None else None,
        "generated": stamp,
        "models": rows,
    }
    (out_dir / "scorecard.json").write_text(json.dumps(payload, indent=2) + "\n")
    (out_dir / "scorecard.md").write_text(to_markdown(rows) + "\n")
    print(f"wrote {out_dir / 'scorecard.json'}")
    print(to_markdown(rows))

    exit_code, successful_rows = _assess_rows(rows)
    exit_code = max(
        exit_code,
        _baseline_regression_status(args=args, rows=successful_rows),
    )
    _write_baseline(args=args, payload=payload, exit_code=exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
