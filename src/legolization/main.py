"""Command-line interface: voxel model in, buildable LDraw model out."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from legolization.instructions.sequencer import InstructionsConfig
from legolization.pipeline import PipelineConfig, run_file
from legolization.placement.base import ObjectiveWeights
from legolization.placement.registry import strategy_names
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
        choices=strategy_names(),
        default="greedy",
        help="placement strategy (default: greedy)",
    )
    parser.add_argument(
        "--time-budget",
        type=float,
        default=None,
        metavar="SECONDS",
        help="soft time budget for slow strategies (smga, beauty)",
    )
    parser.add_argument(
        "--ga-generations",
        type=int,
        default=200,
        help="smga generation cap (the paper used 1000; default 200)",
    )
    parser.add_argument(
        "--beauty-preset",
        choices=("balanced", "stability", "aesthetics", "efficiency"),
        default="balanced",
        help="beauty strategy weight profile (default: balanced)",
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
        "--no-repair",
        action="store_true",
        help="skip the ALNS destroy-and-repair pass on unstable layouts",
    )
    parser.add_argument(
        "--milp",
        action="store_true",
        help="debug cross-check of the exact LP with MILP complementarity (slower)",
    )
    scale = parser.add_mutually_exclusive_group()
    scale.add_argument(
        "--plates-per-voxel",
        type=int,
        default=3,
        help="vertical plates per input voxel (default 3 = brick height)",
    )
    scale.add_argument(
        "--aspect-correct",
        action="store_true",
        help=(
            "resample to 2.5 plates per voxel so cubic voxels keep their "
            "aspect ratio (bricks are 20 LDU wide but 24 LDU tall)"
        ),
    )
    parser.add_argument(
        "--shell-plates",
        type=int,
        default=3,
        help="hollow-shell floor/ceiling thickness in plates (default 3)",
    )
    parser.add_argument("--seed", type=int, default=0, help="random seed")
    parser.add_argument(
        "--colour",
        choices=("hard", "soft"),
        default="hard",
        help=(
            "colour constraint for merges: hard = never cross colour "
            "boundaries; soft = Luo importance sampling may trade small "
            "colour errors for fewer bricks (luo strategy)"
        ),
    )
    parser.add_argument(
        "--colour-weight",
        type=float,
        default=1.0,
        help="soft-colour discard weight w_c (higher = stricter, default 1.0)",
    )
    parser.add_argument(
        "--dither",
        action="store_true",
        help="Floyd-Steinberg dither RGB inputs for smoother colour gradients",
    )
    parser.add_argument(
        "--steps",
        choices=("smart", "layer"),
        default="smart",
        help=(
            "step semantics: smart = digestible prefix-stable steps with "
            "ROTSTEP view hints; layer = legacy one step per plate layer"
        ),
    )
    parser.add_argument(
        "--step-size",
        type=int,
        default=7,
        help="target bricks per smart step (default 7)",
    )
    parser.add_argument(
        "--no-rotstep",
        action="store_true",
        help="omit 0 ROTSTEP view-rotation hints from smart steps",
    )
    parser.add_argument(
        "--bom",
        type=Path,
        default=None,
        metavar="PATH",
        help="also write a bill of materials (.json for JSON, else text)",
    )
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
    progress = (
        (lambda message: print(f"  {message}", file=sys.stderr, flush=True))
        if sys.stderr.isatty()
        else None
    )
    config = PipelineConfig(
        strategy=args.strategy,
        hollow=not args.solid,
        slopes=args.slopes,
        tiles=args.tiles,
        refine=not args.no_refine,
        repair=not args.no_repair,
        seed=args.seed,
        plates_per_voxel=args.plates_per_voxel,
        aspect_correct=args.aspect_correct,
        shell_plates=args.shell_plates,
        colour_mode=args.colour,
        colour_weight=args.colour_weight,
        dither=args.dither,
        time_budget_s=args.time_budget,
        ga_generations=args.ga_generations,
        beauty_preset=args.beauty_preset,
        progress=progress,
        instructions=InstructionsConfig(
            mode=args.steps,
            target_step_size=args.step_size,
            rotstep=not args.no_rotstep,
        ),
        weights=ObjectiveWeights(stability=args.stability_weight),
        solver=SolverConfig(mode="milp" if args.milp else "lp"),
    )
    try:
        result = run_file(args.input, output, config, bom_path=args.bom)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"wrote {output}")
    print(
        f"  bricks: {result.brick_count}   mass: {result.mass_g:.1f} g   "
        f"steps: {result.step_count}   slopes: {result.slopes_added}   "
        f"tiles: {result.tiles_added}"
    )
    if result.plan is not None:
        for warning in result.plan.warnings:
            print(f"  warning: {warning}", file=sys.stderr)
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
