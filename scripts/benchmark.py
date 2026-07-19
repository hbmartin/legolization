"""Compare every placement strategy on the example models.

Runs each registered strategy over the ``data/examples`` inputs (and a
synthetic hollow sphere) and prints a markdown table of part count, mass,
stability margin, bonding, aesthetics, and runtime; ``--json PATH`` also
dumps the raw rows.

Usage: ``uv run python scripts/benchmark.py [--seed N] [--json out.json]``

To pick the best model for one input instead of tabulating, use the CLI's
``--strategy all`` sweep (``legolization.compare``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

from legolization.grid import EMPTY, VoxelGrid
from legolization.pipeline import PipelineConfig, PipelineResult, run
from legolization.placement.base import evaluate
from legolization.placement.registry import strategy_names

_REPO = Path(__file__).resolve().parent.parent


def _example_grids() -> dict[str, VoxelGrid]:
    examples = _REPO / "data" / "examples"
    return {
        "pyramid": VoxelGrid.from_npy(examples / "pyramid.npy"),
        "arch": VoxelGrid.from_npy(examples / "arch.npy"),
        "heart": VoxelGrid.from_vox(examples / "heart.vox"),
        "sphere": _sphere_grid(radius=5),
    }


def _sphere_grid(radius: int) -> VoxelGrid:
    n = 2 * radius + 1
    codes = np.full((n, n, n), EMPTY, dtype=np.int16)
    xs, ys, zs = np.mgrid[0:n, 0:n, 0:n]
    inside = (xs - radius) ** 2 + (ys - radius) ** 2 + (zs - radius) ** 2
    codes[inside <= radius * radius] = 4
    return VoxelGrid.from_array(codes, plates_per_voxel=3)


def _placed_grid(result: PipelineResult) -> VoxelGrid:
    """Narrow the optional grid; run() always keeps its voxel grid."""
    if result.grid is None:
        msg = "pipeline result lost its voxel grid"
        raise RuntimeError(msg)
    return result.grid


def benchmark(seed: int) -> list[dict]:
    """Run all strategies over all example grids."""
    rows: list[dict] = []
    for model_name, grid in _example_grids().items():
        for strategy in strategy_names():
            try:
                config = PipelineConfig(strategy=strategy, seed=seed)
                started = time.perf_counter()
                result = run(grid, config)
                elapsed = time.perf_counter() - started
                report = evaluate(result.layout, _placed_grid(result), config.weights)
                rows.append(
                    {
                        "model": model_name,
                        "strategy": strategy,
                        "bricks": result.brick_count,
                        "mass_g": round(result.mass_g, 1),
                        "steps": result.step_count,
                        "buildable": result.buildable,
                        "max_score": round(result.stability.max_score, 4),
                        "min_capacity_n": round(result.stability.min_capacity, 4),
                        "seam_alignment": round(report.aesthetics, 3),
                        "perpendicularity": round(report.perpendicularity, 3),
                        "symmetry": round(report.symmetry, 3),
                        "colour_error": round(report.colour_error, 3),
                        "seconds": round(elapsed, 2),
                    }
                )
            except Exception as error:  # noqa: BLE001 - isolate benchmark cases
                print(
                    f"{model_name:>8} | {strategy:>7} | ERROR: {error}",
                    file=sys.stderr,
                )
                continue
            print(
                f"{model_name:>8} | {strategy:>7} | "
                f"{rows[-1]['bricks']:>4} bricks | "
                f"{'OK ' if rows[-1]['buildable'] else 'BAD'} | "
                f"{rows[-1]['seconds']:>6.2f}s",
                file=sys.stderr,
            )
    return rows


def to_markdown(rows: list[dict]) -> str:
    """Render the benchmark rows as a markdown table."""
    if not rows:
        return ""
    columns = list(rows[0].keys())
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    lines.extend(
        "| " + " | ".join(str(row[column]) for column in columns) + " |" for row in rows
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    rows = benchmark(args.seed)
    print(to_markdown(rows))
    if args.json is not None:
        args.json.write_text(json.dumps(rows, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
