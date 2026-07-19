"""Trace a model's brick count and topology through every pipeline phase.

Runs one strategy on one model under telemetry recording and tabulates
the exact-value gauges the pipeline and layered engine emit, in global
emission order. Coverage per phase (PR #18 review made the promise
precise): the layered engine's tiled/compacted/connected phases carry
bricks + components (no stability — an LP per engine phase is not paid
for a diagnostic); the pipeline's placed/repaired/restored/remerged
phases carry bricks + components + stability. This is the measurement
tool behind ``docs/kollsker-drift-report.md`` — a per-layer-optimal
tiling that ends worse than a heuristic did so in one of these phases.

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
from legolization.placement.registry import strategy_names


def _positive_int(text: str) -> int:
    if (value := int(text)) < 1:
        msg = f"must be >= 1, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _non_negative_int(text: str) -> int:
    if (value := int(text)) < 0:
        msg = f"must be >= 0, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


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


def _rows(events: list[tuple[str, float]]) -> list[_Row]:
    """Order phase rows by gauge EMISSION sequence, not by phase name.

    Grouping per name printed pass-two tiling before pass-one repair
    (and even placed before repaired, which is emitted first inside
    _place_and_repair) — deltas were attributed to the wrong phase
    (PR #18 review). Rows now follow the global event log; the i-th
    ``.bricks`` reading of a phase pairs with that phase's i-th
    companion components/stable readings.
    """
    labels = dict(_PHASES)
    by_name: dict[str, list[float]] = {}
    for name, value in events:
        by_name.setdefault(name, []).append(value)
    rows: list[_Row] = []
    seen: dict[str, int] = {}
    for name, value in events:
        if not name.endswith(".bricks"):
            continue
        key = name.removesuffix(".bricks")
        if key not in labels:
            continue
        occurrence = seen.get(key, 0) + 1
        seen[key] = occurrence
        components = by_name.get(f"{key}.components", [])
        stable = by_name.get(f"{key}.stable", [])
        rows.append(
            _Row(
                phase=labels[key],
                occurrence=occurrence,
                bricks=int(value),
                components=(
                    int(components[occurrence - 1])
                    if occurrence <= len(components)
                    else None
                ),
                stable=(
                    bool(stable[occurrence - 1]) if occurrence <= len(stable) else None
                ),
            )
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model")
    parser.add_argument("--strategy", default="kollsker", choices=strategy_names())
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--solid", action="store_true")
    parser.add_argument("--no-repair", action="store_true")
    parser.add_argument("--fail-max", type=_non_negative_int, default=None, metavar="N")
    parser.add_argument("--no-milp-bridge", action="store_true")
    parser.add_argument("--target-studs", type=_positive_int, default=32, metavar="N")
    parser.add_argument("--out", type=Path, default=_REPO / "eval" / "profiles")
    args = parser.parse_args(argv)

    profiler = _profiler_module()
    config = PipelineConfig(
        strategy=args.strategy,
        seed=args.seed,
        hollow=not args.solid,
        repair=not args.no_repair,
        connectivity_fail_max=args.fail_max,
        milp_bridge=not args.no_milp_bridge,
        instructions=InstructionsConfig(mode="layer"),
        mesh=MeshOptions(target_studs=args.target_studs),
    )
    resolved = profiler._resolve_grid(args.model, config)  # noqa: SLF001
    name, grid = resolved.name, resolved.grid

    started = time.perf_counter()
    with telemetry.record() as session:
        result = run(grid, config)
    total = time.perf_counter() - started

    values = session.values_dict()
    rows = _rows(session.events_list())
    connectivity = session.spans.get("connectivity.attempt")
    accepts = session.spans.get("connectivity.accept")
    payload = {
        "schema": 1,
        "generated": datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%dT%H%M%SZ"),
        "git_sha": telemetry.git_sha(),
        "run": {
            **resolved.run_identity(),
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
        "events": [[name, value] for name, value in session.events_list()],
    }
    variant = "".join(
        tag
        for tag, active in (
            ("-solid", args.solid),
            ("-norepair", args.no_repair),
            (f"-fm{args.fail_max}", args.fail_max is not None),
            ("-nobridge", args.no_milp_bridge),
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
