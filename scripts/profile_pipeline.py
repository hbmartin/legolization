"""Profile one pipeline run and record the result as a comparable artifact.

Runs a model through the in-process pipeline inside a telemetry recording
session (see ``legolization.telemetry``) and writes
``eval/profiles/<UTC>-<name>-<strategy>.json`` capturing per-span call
counts and wall seconds, run metadata, and the git sha — so a claim like
"build_model dominates" can be re-checked after any change against the
same pinned inputs. ``--cprofile`` additionally writes a sibling
``.pstats`` for line-level drilling (it inflates telemetry seconds, so
never compare timings across that flag; call counts stay comparable).

Usage::

    uv run python scripts/profile_pipeline.py MODEL [--strategy greedy]
        [--seed 0] [--target-studs N] [--up x|y|z] [--label TEXT]
        [--out eval/profiles] [--cprofile] [--solid] [--no-repair]
        [--steps smart|layer]

MODEL is a ``.vox/.npy/.obj/.stl/.ply`` path or a corpus manifest name
(synthetic models regenerate in memory; meshes use their manifest
``target_studs``/``up`` — the explicit flags apply to file paths only).
Timings from parallel sweeps are out of scope: telemetry does not cross
spawn workers, so this script always profiles a single strategy in-process.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from legolization import telemetry
from legolization.instructions.sequencer import InstructionsConfig
from legolization.mesh import MeshOptions
from legolization.pipeline import PipelineConfig, PipelineResult, load_grid, run

if TYPE_CHECKING:
    from legolization.grid import VoxelGrid

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
PROFILES = _REPO / "eval" / "profiles"


git_sha = telemetry.git_sha
"""Shared with the CLI --profile writer (legolization.telemetry)."""


def _resolve_grid(
    model: str,
    config: PipelineConfig,
) -> tuple[str, str, VoxelGrid]:
    """Resolve MODEL to (name, input description, grid)."""
    path = Path(model)
    if path.suffix and path.exists():
        return path.stem, str(path), load_grid(path, config)
    spec_path = _SCRIPTS / "eval_corpus.py"
    import importlib.util  # noqa: PLC0415 - only needed for corpus names

    spec = importlib.util.spec_from_file_location("eval_corpus_script", spec_path)
    if spec is None or spec.loader is None:
        msg = "cannot load scripts/eval_corpus.py"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    corpus = module.load_corpus_module()
    matches = [m for m in corpus.load_manifest() if m.name == model]
    if not matches:
        msg = f"{model!r} is neither an existing input file nor a corpus model"
        raise SystemExit(msg)
    grid = module.model_grid(corpus, matches[0])
    if grid is None:
        msg = f"corpus mesh {model!r} is not on disk; run scripts/corpus.py download"
        raise SystemExit(msg)
    return matches[0].name, str(matches[0].path), grid


def _run_profiled(
    grid: VoxelGrid,
    config: PipelineConfig,
    *,
    pstats_path: Path | None,
) -> tuple[PipelineResult, telemetry.Telemetry, float]:
    """Execute the pipeline under recording; returns (result, spans, wall)."""
    started = time.perf_counter()
    with telemetry.record() as session:
        if pstats_path is not None:
            profiler = cProfile.Profile()
            result = profiler.runcall(run, grid, config)
            profiler.dump_stats(pstats_path)
        else:
            result = run(grid, config)
    return result, session, time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model")
    parser.add_argument("--strategy", default="greedy")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--target-studs", type=int, default=32, metavar="N")
    parser.add_argument("--up", choices=("x", "y", "z"), default="z")
    parser.add_argument("--label", default="")
    parser.add_argument("--out", type=Path, default=PROFILES)
    parser.add_argument("--cprofile", action="store_true")
    parser.add_argument("--solid", action="store_true")
    parser.add_argument("--no-repair", action="store_true")
    parser.add_argument("--steps", choices=("smart", "layer"), default="smart")
    args = parser.parse_args(argv)

    config = PipelineConfig(
        strategy=args.strategy,
        seed=args.seed,
        hollow=not args.solid,
        repair=not args.no_repair,
        instructions=InstructionsConfig(mode=args.steps),
        mesh=MeshOptions(target_studs=args.target_studs, up=args.up),
        progress=lambda message: print(f"  {message}", file=sys.stderr),
    )
    name, input_desc, grid = _resolve_grid(args.model, config)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.out / f"{stamp}-{name}-{args.strategy}"
    pstats_path = base.with_suffix(".pstats") if args.cprofile else None

    result, session, total_seconds = _run_profiled(
        grid,
        config,
        pstats_path=pstats_path,
    )

    spans = session.to_dict()
    payload = {
        "schema": 1,
        "generated": stamp,
        "git_sha": git_sha(),
        "label": args.label,
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "run": {
            "model": name,
            "input": input_desc,
            "strategy": args.strategy,
            "seed": args.seed,
            "target_studs": args.target_studs,
            "hollow": not args.solid,
            "repair": not args.no_repair,
            "steps": args.steps,
        },
        "result": {
            "brick_count": result.brick_count,
            "step_count": result.step_count,
            "mass_g": round(result.mass_g, 2),
            "stable": result.stability.stable,
            "buildable": result.buildable,
        },
        "total_seconds": round(total_seconds, 3),
        "cprofile_active": args.cprofile,
        "cprofile_path": str(pstats_path) if pstats_path is not None else None,
        "spans": spans,
    }
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {json_path}")
    if pstats_path is not None:
        print(f"wrote {pstats_path}")

    rows = sorted(session.spans.items(), key=lambda item: -item[1].seconds)
    print(f"{'span':<28} {'calls':>7} {'seconds':>10}")
    for span_name, stats in rows:
        print(f"{span_name:<28} {stats.calls:>7} {stats.seconds:>10.3f}")
    print(
        f"{'TOTAL (wall)':<28} {'':>7} {total_seconds:>10.3f}   "
        f"bricks={result.brick_count} steps={result.step_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
