"""Collect resumable model/strategy/seed evaluation artifacts.

Before launching placement, this command writes a collection manifest with
every expected candidate. Each completion is then persisted immediately as an
atomic, identity-stamped JSON artifact. Exact successful artifacts are reused;
failed, missing, corrupt, or identity-mismatched candidates are retried.

Collection deliberately does not assemble a scorecard or write a baseline.
Pass its emitted manifest to ``scripts/assemble_eval.py`` after collection.

Usage::

    uv run python scripts/eval_corpus.py [--models a,b] [--traits fast]
        [--kind mesh|synthetic] [--strategies greedy,fast] [--jobs 0]
        [--timeout 300] [--seeds 0,1] [--fresh]

Synthetic models are regenerated in memory and are the default fast scope.
Mesh evaluation is deliberately opt-in via ``--kind mesh``; run
``scripts/corpus.py download`` first for full coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from legolization.compare import Candidate, run_all, select_best
from legolization.eval_artifacts import (
    SourceIdentity,
    atomic_json,
    candidate_path,
    candidate_payload,
    configuration_hash,
    input_sha256,
    matching_candidate,
    source_identity,
)
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


def model_mesh_options(model: CorpusModelLike) -> MeshOptions | None:
    """Effective mesh options a manifest model resolves with (None = synthetic).

    Single source of the manifest-default fallbacks so profile artifacts
    can stamp the values a corpus run ACTUALLY used (PR #18 review: the
    payload recorded the CLI flag, which corpus names ignore).
    """
    if model.kind == "synthetic":
        return None
    return MeshOptions(
        target_studs=model.target_studs or 32,
        up=model.up or "z",
        keep_largest=model.largest_component_only,
    )


def model_grid(corpus: ModuleType, model: CorpusModelLike) -> VoxelGrid | None:
    """Materialize a manifest model as a grid (None = mesh not on disk)."""
    if (options := model_mesh_options(model)) is None:
        codes = corpus.GENERATORS[model.generator]()
        return VoxelGrid.from_array(codes, plates_per_voxel=model.plates_per_voxel)
    if not model.abs_path.exists():
        return None
    return mesh_to_grid(model.abs_path, options=options)


def unsupported_ratio(grid: VoxelGrid | None) -> float | None:
    """Liu et al. 2024's Cs: fraction of filled voxels with nothing below.

    A cheap difficulty stat — higher means more overhang and a layout
    that needs more sophisticated support design.
    """
    if grid is None or grid.filled_count == 0:
        return None
    filled = grid.filled_mask
    # z=0 rests on the ground and never counts, matching the definition.
    unsupported = filled[:, :, 1:] & ~filled[:, :, :-1]
    return round(int(unsupported.sum()) / int(filled.sum()), 4)


def build_row(
    model: CorpusModelLike,
    report_dict: dict | None,
    status: str,
    *,
    grid: VoxelGrid | None = None,
) -> dict:
    """Assemble one JSON-safe scorecard row."""
    row: dict = {
        "model": model.name,
        "kind": model.kind,
        "traits": list(model.traits),
        "status": status,
        "unsupported_ratio": unsupported_ratio(grid),
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
    parser.add_argument(
        "--kind",
        choices=("mesh", "synthetic"),
        default="synthetic",
        help="corpus kind to sweep (default: synthetic; mesh is opt-in)",
    )
    parser.add_argument("--strategies", default=None, metavar="NAME,...")
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--seeds", type=_seed_list, default=None, metavar="N,N,...")
    parser.add_argument("--out", type=Path, default=RUNS)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="ignore exact successful artifacts and rerun selected candidates",
    )
    # Retained only to give existing automation a useful migration error.
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

    An explicit ``--baseline`` names one file; otherwise the baseline
    for the selected kind contributes rows.
    """
    if args.baseline is not None and not args.baseline.exists():
        # A typo'd explicit path must fail loudly, not silently run
        # baseline-free (PR #20 review); only the committed per-kind
        # defaults may be legitimately absent.
        msg = f"--baseline {args.baseline} does not exist"
        raise SystemExit(msg)
    paths = (
        [args.baseline] if args.baseline is not None else [_BASELINE_BY_KIND[args.kind]]
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


def _grid_hash(grid: VoxelGrid) -> str:
    """Hash a generated grid's exact shape, dtype, and colour codes."""
    digest = hashlib.sha256()
    digest.update(str(grid.codes.dtype).encode())
    digest.update(repr(grid.shape).encode())
    digest.update(grid.codes.tobytes(order="C"))
    return digest.hexdigest()


def _model_input_hash(model: CorpusModelLike, grid: VoxelGrid) -> str:
    """Return source-byte identity for meshes and exact grid identity otherwise."""
    if model.kind == "mesh":
        return input_sha256(model.abs_path)
    return _grid_hash(grid)


def _effective_config(
    args: argparse.Namespace,
    *,
    strategy: str,
    seed: int,
) -> PipelineConfig:
    """Mirror compare.run_all's result-affecting per-candidate config."""
    return replace(
        PipelineConfig(seed=args.seed),
        strategy=strategy,
        seed=seed,
        progress=None,
        time_budget_s=args.timeout,
    )


def _model_config(model: CorpusModelLike) -> dict[str, object]:
    """Return manifest settings that affect materialized model geometry."""
    mesh = model_mesh_options(model)
    return {
        "kind": model.kind,
        "plates_per_voxel": model.plates_per_voxel,
        "generator": model.generator,
        "mesh": mesh,
    }


def _selected_names(args: argparse.Namespace) -> tuple[str, ...]:
    if args.strategies is not None:
        return tuple(name.strip() for name in args.strategies.split(","))
    from legolization.placement.registry import strategy_names  # noqa: PLC0415

    return tuple(strategy_names())


def _selected_seeds(args: argparse.Namespace) -> tuple[int, ...]:
    return tuple(dict.fromkeys(args.seeds)) if args.seeds else (args.seed,)


def _initial_manifest(
    models: list[CorpusModelLike],
    args: argparse.Namespace,
    *,
    identity: SourceIdentity,
    stamp: str,
) -> dict[str, object]:
    """Build the complete expected collection matrix before work starts."""
    names = _selected_names(args)
    seeds = _selected_seeds(args)
    return {
        "schema": 1,
        "collection_id": stamp,
        "generated": stamp,
        "identity": identity.to_dict(),
        "scope": {
            "kind": args.kind,
            "models": [model.name for model in models],
            "strategies": list(names),
            "seeds": list(seeds),
            "timeout_s": args.timeout,
        },
        "models": [
            {
                "model": model.name,
                "kind": model.kind,
                "traits": list(model.traits),
                "expect_min_buildable": model.expect_min_buildable,
                "status": "pending",
                "unsupported_ratio": None,
                "input_hash": None,
                "candidates": [
                    {
                        "strategy": strategy,
                        "seed": seed,
                        "status": "pending",
                        "config_hash": None,
                        "artifact": None,
                    }
                    for strategy in names
                    for seed in seeds
                ],
            }
            for model in models
        ],
    }


def _manifest_model(manifest: dict[str, object], name: str) -> dict[str, object]:
    models = cast("list[dict[str, object]]", manifest["models"])
    return next(model for model in models if model["model"] == name)


def _manifest_candidate(
    model_entry: dict[str, object],
    strategy: str,
    seed: int,
) -> dict[str, object]:
    candidates = cast("list[dict[str, object]]", model_entry["candidates"])
    return next(
        candidate
        for candidate in candidates
        if candidate["strategy"] == strategy and candidate["seed"] == seed
    )


def _relative(path: Path) -> str:
    try:
        return path.relative_to(_REPO).as_posix()
    except ValueError:
        return path.as_posix()


def _collect_model(  # noqa: PLR0913 - one model needs all collection state
    corpus: ModuleType,
    model: CorpusModelLike,
    args: argparse.Namespace,
    manifest: dict[str, object],
    manifest_path: Path,
    identity: SourceIdentity,
) -> bool:
    """Materialize, resume, and persist one model's candidate matrix."""
    print(f"=== {model.name}", file=sys.stderr)
    model_entry = _manifest_model(manifest, model.name)
    try:
        grid = model_grid(corpus, model)
    except ValueError as error:
        model_entry["status"] = f"error: {error}"
        atomic_json(manifest_path, manifest)
        return False
    if grid is None:
        model_entry["status"] = "skipped: mesh not on disk (download)"
        atomic_json(manifest_path, manifest)
        return False

    input_hash = _model_input_hash(model, grid)
    model_entry["input_hash"] = input_hash
    model_entry["unsupported_ratio"] = unsupported_ratio(grid)
    model_entry["status"] = "collecting"
    names = _selected_names(args)
    seeds = _selected_seeds(args)
    cached: list[Candidate] = []
    skipped: set[tuple[str, int]] = set()
    paths: dict[tuple[str, int], Path] = {}
    hashes: dict[tuple[str, int], str] = {}

    for strategy in names:
        for seed in seeds:
            key = (strategy, seed)
            config_hash = configuration_hash(
                {"evaluation_schema": 1},
                _effective_config(args, strategy=strategy, seed=seed),
                _model_config(model),
            )
            path = candidate_path(
                args.out,
                model=model.name,
                strategy=strategy,
                seed=seed,
                identity=identity,
                config_hash=config_hash,
                input_hash=input_hash,
            )
            paths[key] = path
            hashes[key] = config_hash
            entry = _manifest_candidate(model_entry, strategy, seed)
            entry["config_hash"] = config_hash
            entry["artifact"] = _relative(path)
            hit = (
                None
                if args.fresh
                else matching_candidate(
                    path,
                    identity=identity,
                    config_hash=config_hash,
                    input_hash=input_hash,
                    model=model.name,
                    strategy=strategy,
                    seed=seed,
                )
            )
            if hit is not None:
                entry["status"] = "reused"
                cached.append(hit)
                skipped.add(key)
    atomic_json(manifest_path, manifest)

    def completed(candidate: Candidate) -> None:
        key = (candidate.strategy, candidate.seed)
        payload = candidate_payload(
            candidate,
            identity=identity,
            config_hash=hashes[key],
            input_hash=input_hash,
            model=model.name,
        )
        atomic_json(paths[key], payload)
        entry = _manifest_candidate(model_entry, *key)
        entry["status"] = "ok" if candidate.ok else "error"
        atomic_json(manifest_path, manifest)

    fresh = run_all(
        grid,
        PipelineConfig(seed=args.seed),
        jobs=args.jobs,
        names=names,
        seeds=seeds,
        timeout_s=args.timeout,
        progress=lambda message: print(f"  {message}", file=sys.stderr),
        skip=skipped,
        on_complete=completed,
    )
    candidates = [*cached, *fresh]
    report = select_best(candidates)
    model_entry["status"] = "ok" if report.winner is not None else "error: all failed"
    atomic_json(manifest_path, manifest)
    return report.winner is not None


def main(argv: list[str] | None = None) -> int:
    """Collect candidate artifacts; scorecard assembly is a separate command."""
    args = parse_args(argv)
    if args.write_baseline or args.baseline is not None:
        msg = (
            "baseline assembly moved to scripts/assemble_eval.py; "
            "pass it the collection manifest written by this command"
        )
        raise SystemExit(msg)
    corpus = load_corpus_module()
    models = corpus.select_models(corpus.load_manifest(), args.models)
    if args.traits is not None:
        wanted = {trait.strip() for trait in args.traits.split(",")}
        models = [m for m in models if wanted & set(m.traits)]
    excluded_kinds = sorted({m.kind for m in models})
    if args.kind is not None:
        models = [m for m in models if m.kind == args.kind]
    if not models:
        hint = (
            f"; the selection matched only kind {', '.join(excluded_kinds)}"
            f" - pass --kind {excluded_kinds[0]}"
            if excluded_kinds and args.kind is not None
            else ""
        )
        print(f"error: no corpus models selected{hint}", file=sys.stderr)
        return 1
    identity = source_identity(_REPO)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    manifest = _initial_manifest(models, args, identity=identity, stamp=stamp)
    manifest_path = args.out / "collections" / f"{stamp}.json"
    atomic_json(manifest_path, manifest)
    outcomes = [
        _collect_model(
            corpus,
            model,
            args,
            manifest,
            manifest_path,
            identity,
        )
        for model in models
    ]
    success = all(outcomes)
    manifest["status"] = "complete" if success else "incomplete"
    atomic_json(manifest_path, manifest)
    print(f"collection manifest: {manifest_path}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
