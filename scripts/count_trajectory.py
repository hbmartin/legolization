"""Trace a model's brick count and topology through every pipeline phase.

Runs one strategy on one model under telemetry recording and tabulates
the exact-value gauges the pipeline and layered engine emit: bricks,
components, and stability after tiling, vertical compaction,
improve_connectivity, repair, each hollow-restore round, and the final
remerge. This is the measurement tool behind
``docs/kollsker-drift-report.md`` — a per-layer-optimal tiling that ends
worse than a heuristic did so in one of these phases.

Usage::

    uv run python scripts/count_trajectory.py MODEL [--strategy kollsker]
        [--seed 0] [--solid] [--no-repair] [--fail-max N]
        [--target-studs N] [--out eval/profiles]

``MODEL`` is a file path or a corpus manifest name. ``--fail-max 0``
disables improve_connectivity (clean ablation); ``--solid`` disables
hollow/restore; ``--no-repair`` disables the ALNS repair.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from legolization import telemetry
from legolization.instructions.sequencer import InstructionsConfig
from legolization.mesh import MeshOptions
from legolization.pipeline import PipelineConfig, run

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).resolve().parent.parent


def _profiler_module() -> ModuleType:
    """Load profile_pipeline.py for its _resolve_grid helper."""
    spec = importlib.util.spec_from_file_location(
        "profile_pipeline_script", _REPO / "scripts" / "profile_pipeline.py"
    )
    if spec is None or spec.loader is None:
        msg = "profile_pipeline.py is not importable"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_PHASES = (
    ("place.tiled", "tiled (per-layer minimum)"),
    ("place.compacted", "compact_vertical"),
    ("place.connected", "improve_connectivity"),
    ("pipeline.placed", "post-place (incl. repair)"),
    ("pipeline.repaired", "post-repair"),
    ("pipeline.restored", "hollow-restore round"),
    ("pipeline.remerged", "final_remerge"),
)


class _Row(NamedTuple):
    phase: str
    occurrence: int
    bricks: int
    components: int | None
    stable: bool | None


def _rows(values: dict[str, list[float]]) -> list[_Row]:
    """Flatten the gauge readings into ordered phase rows."""
    rows: list[_Row] = []
    for key, label in _PHASES:
        bricks = values.get(f"{key}.bricks", [])
        components = values.get(f"{key}.components", [])
        stable = values.get(f"{key}.stable", [])
        for i, count in enumerate(bricks):
            rows.append(
                _Row(
                    phase=label,
                    occurrence=i + 1,
                    bricks=int(count),
                    components=int(components[i]) if i < len(components) else None,
                    stable=bool(stable[i]) if i < len(stable) else None,
                )
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model")
    parser.add_argument("--strategy", default="kollsker")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--solid", action="store_true")
    parser.add_argument("--no-repair", action="store_true")
    parser.add_argument("--fail-max", type=int, default=None, metavar="N")
    parser.add_argument("--target-studs", type=int, default=32, metavar="N")
    parser.add_argument("--out", type=Path, default=_REPO / "eval" / "profiles")
    args = parser.parse_args(argv)

    profiler = _profiler_module()
    config = PipelineConfig(
        strategy=args.strategy,
        seed=args.seed,
        hollow=not args.solid,
        repair=not args.no_repair,
        connectivity_fail_max=args.fail_max,
        instructions=InstructionsConfig(mode="layer"),
        mesh=MeshOptions(target_studs=args.target_studs),
    )
    name, input_used, grid = profiler._resolve_grid(args.model, config)  # noqa: SLF001

    started = time.perf_counter()
    with telemetry.record() as session:
        result = run(grid, config)
    total = time.perf_counter() - started

    values = session.values_dict()
    rows = _rows(values)
    connectivity = session.spans.get("connectivity.attempt")
    accepts = session.spans.get("connectivity.accept")
    payload = {
        "schema": 1,
        "generated": datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%dT%H%M%SZ"),
        "git_sha": telemetry.git_sha(),
        "run": {
            "model": name,
            "input": input_used,
            "strategy": args.strategy,
            "seed": args.seed,
            "hollow": config.hollow,
            "repair": config.repair,
            "fail_max": args.fail_max,
        },
        "result": {
            "brick_count": result.brick_count,
            "stable": result.stability.stable,
            "components": result.component_count,
        },
        "total_seconds": round(total, 3),
        "connectivity": {
            "attempts": connectivity.calls if connectivity else 0,
            "accepts": accepts.calls if accepts else 0,
        },
        "trajectory": [row._asdict() for row in rows],
        "values": values,
    }
    variant = "".join(
        tag
        for tag, active in (
            ("-solid", args.solid),
            ("-norepair", args.no_repair),
            (f"-fm{args.fail_max}", args.fail_max is not None),
        )
        if active
    )
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = (
        args.out
        / f"{payload['generated']}-{name}-{args.strategy}{variant}-trajectory.json"
    )
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    print(
        f"{name} / {args.strategy} (seed {args.seed}): "
        f"{result.brick_count} bricks in {total:.1f}s"
    )
    print(f"{'phase':<28} {'bricks':>7} {'Δ':>6} {'comps':>6} {'stable':>7}")
    previous: int | None = None
    for row in rows:
        delta = "" if previous is None else f"{row.bricks - previous:+d}"
        comps = "" if row.components is None else str(row.components)
        stable = "" if row.stable is None else str(row.stable)
        label = row.phase
        if row.occurrence > 1:
            label = f"{label} #{row.occurrence}"
        print(f"{label:<28} {row.bricks:>7} {delta:>6} {comps:>6} {stable:>7}")
        previous = row.bricks
    print(
        f"connectivity attempts={payload['connectivity']['attempts']} "
        f"accepts={payload['connectivity']['accepts']}"
    )
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
