"""Command-line interface: voxel model in, buildable LDraw model out."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from legolization.pipeline import PipelineConfig, run_file
from legolization.placement.base import ObjectiveWeights
from legolization.stability.solver import SolverConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legolization",
        description=(
            "Convert a colored voxel model (.vox or .npy) into a physically "
            "stable LEGO model in LDraw format with step-by-step instructions."
        ),
    )
    parser.add_argument("input", type=Path, help="input .vox or .npy voxel model")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output .ldr or .mpd path (default: input name with .ldr)",
    )
    parser.add_argument(
        "--strategy",
        choices=("greedy", "luo"),
        default="greedy",
        help="placement strategy (default: greedy)",
    )
    parser.add_argument(
        "--solid",
        action="store_true",
        help="keep the model solid instead of auto-hollowing",
    )
    parser.add_argument(
        "--slopes",
        action="store_true",
        help="smooth staircase surfaces with 45-degree slope bricks",
    )
    parser.add_argument(
        "--tiles",
        action="store_true",
        help="cap exposed top plates with smooth tiles",
    )
    parser.add_argument(
        "--no-refine",
        action="store_true",
        help="skip the stability-driven refinement loop",
    )
    parser.add_argument(
        "--milp",
        action="store_true",
        help="debug cross-check of the exact LP with MILP complementarity (slower)",
    )
    parser.add_argument(
        "--plates-per-voxel",
        type=int,
        default=3,
        help="vertical plates per input voxel (default 3 = brick height)",
    )
    parser.add_argument("--seed", type=int, default=0, help="random seed")
    parser.add_argument(
        "--stability-weight",
        type=float,
        default=4.0,
        help="objective weight of physical stability (default 4.0)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns a process exit code."""
    args = _build_parser().parse_args(argv)
    output: Path = args.output or args.input.with_suffix(".ldr")
    config = PipelineConfig(
        strategy=args.strategy,
        hollow=not args.solid,
        slopes=args.slopes,
        tiles=args.tiles,
        refine=not args.no_refine,
        seed=args.seed,
        plates_per_voxel=args.plates_per_voxel,
        weights=ObjectiveWeights(stability=args.stability_weight),
        solver=SolverConfig(mode="milp" if args.milp else "lp"),
    )
    try:
        result = run_file(args.input, output, config)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"wrote {output}")
    print(
        f"  bricks: {result.brick_count}   mass: {result.mass_g:.1f} g   "
        f"slopes: {result.slopes_added}   tiles: {result.tiles_added}"
    )
    print(
        f"  stability: {'STABLE' if result.stability.stable else 'UNSTABLE'} "
        f"(worst score {result.stability.max_score:.3f}, "
        f"min capacity {result.stability.min_capacity:.3f} N)"
    )
    if result.component_count != 1 or result.floating_count:
        print(
            f"  warning: {result.component_count} components, "
            f"{result.floating_count} floating bricks",
            file=sys.stderr,
        )
    if not result.buildable:
        print("  model is NOT fully buildable; try --strategy luo or --solid")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
