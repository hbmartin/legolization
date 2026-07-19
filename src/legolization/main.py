"""Command-line interface: voxel model in, buildable LDraw model out."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from legolization import telemetry
from legolization.compare import Candidate, SelectionReport, run_all, select_best
from legolization.graph import ConnectionGraph
from legolization.instructions.sequencer import InstructionsConfig, plan_instructions
from legolization.ldraw_in import LdrawImportError, layout_from_ldraw
from legolization.ldraw_out import write_model
from legolization.mesh import DEFAULT_MESH_COLOUR, MESH_SUFFIXES, MeshOptions
from legolization.pipeline import (
    PipelineConfig,
    PipelineResult,
    load_grid,
    run_file,
    write_outputs,
)
from legolization.placement.base import ObjectiveWeights
from legolization.placement.registry import strategy_names
from legolization.stability.solver import SolverConfig, analyze

if TYPE_CHECKING:
    from collections.abc import Callable

LDRAW_SUFFIXES = {".ldr", ".mpd"}


def _non_negative_int(value: str) -> int:
    """Parse an integer greater than or equal to zero for argparse."""
    try:
        parsed = int(value)
    except ValueError as error:
        msg = f"{value!r} is not an integer"
        raise argparse.ArgumentTypeError(msg) from error
    if parsed < 0:
        msg = f"{value!r} must be greater than or equal to zero"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _positive_int(value: str) -> int:
    """Parse an integer greater than zero for argparse."""
    parsed = _non_negative_int(value)
    if parsed == 0:
        msg = f"{value!r} must be greater than zero"
        raise argparse.ArgumentTypeError(msg)
    return parsed


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


def _positive_float(value: str) -> float:
    """Parse a finite float greater than zero for argparse."""
    try:
        parsed = float(value)
    except ValueError as error:
        msg = f"{value!r} is not a number"
        raise argparse.ArgumentTypeError(msg) from error
    if not math.isfinite(parsed) or parsed <= 0.0:
        msg = f"{value!r} must be a finite number greater than zero"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legolization",
        description=(
            "Convert a colored voxel model (.vox or .npy) or a triangle mesh "
            "(.obj, .stl, .ply) into a physically stable LEGO model in LDraw "
            "format with step-by-step instructions."
        ),
    )
    parser.add_argument(
        "input",
        type=Path,
        help="input .vox/.npy voxel model or .obj/.stl/.ply mesh",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output .ldr or .mpd path (default: input name with .ldr)",
    )
    parser.add_argument(
        "--strategy",
        choices=(*strategy_names(), "all"),
        default="greedy",
        help=(
            "placement strategy (default: greedy); 'all' runs every strategy "
            "and keeps the best result"
        ),
    )
    sweep = parser.add_argument_group("strategy sweep (--strategy all)")
    sweep.add_argument(
        "--jobs",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help=(
            "parallel worker processes for the sweep (default 0 = one per "
            "strategy up to the CPU count; 1 = sequential)"
        ),
    )
    sweep.add_argument(
        "--timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help=(
            "soft sweep-wide deadline in parallel mode; also folded into "
            "--time-budget for strategies that honour it (running workers "
            "may continue after the sweep returns)"
        ),
    )
    sweep.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help="write a JSON comparison report of every strategy and the winner",
    )
    sweep.add_argument(
        "--keep-candidates",
        type=Path,
        default=None,
        metavar="DIR",
        help="also write every successful strategy's model into DIR",
    )
    sweep.add_argument(
        "--seeds",
        type=_seed_list,
        default=None,
        metavar="N,N,...",
        help=(
            "run every strategy once per listed seed and pick the overall "
            "best (restarts; --timeout still bounds the whole sweep)"
        ),
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
        nargs="?",
        const="preserve",
        choices=("preserve", "smooth"),
        default=None,
        help=(
            "fit slope bricks onto staircase surfaces: 'preserve' (default) "
            "swaps exact in-shape matches without adding material; 'smooth' "
            "adds slopes outside the shape"
        ),
    )
    parser.add_argument(
        "--tiles",
        action="store_true",
        help="cap exposed top plates with smooth tiles",
    )
    parser.add_argument(
        "--snot",
        action="store_true",
        help=(
            "clad tall flat wall faces with sideways tiles hung on "
            "side-stud brackets (studs-not-on-top)"
        ),
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
    mesh = parser.add_argument_group("mesh input (.obj/.stl/.ply)")
    mesh_pitch = mesh.add_mutually_exclusive_group()
    mesh_pitch.add_argument(
        "--target-studs",
        type=_positive_int,
        default=None,
        metavar="N",
        help="widest horizontal extent of the model in studs (default 32)",
    )
    mesh_pitch.add_argument(
        "--pitch",
        type=_positive_float,
        default=None,
        metavar="UNITS",
        help="mesh model units per stud (overrides --target-studs)",
    )
    mesh.add_argument(
        "--up",
        choices=("x", "y", "z"),
        default=None,
        help="mesh vertical axis (default z; most .obj files are y-up)",
    )
    mesh.add_argument(
        "--mesh-colour",
        type=int,
        default=None,
        metavar="CODE",
        help=(
            f"LDraw colour code for all mesh voxels "
            f"(default {DEFAULT_MESH_COLOUR} = light grey)"
        ),
    )
    mesh.add_argument(
        "--mesh-colour-mode",
        choices=("uniform", "sampled"),
        default=None,
        help=(
            "uniform (default) paints every voxel --mesh-colour; sampled "
            "takes each voxel's colour from the mesh's texture/vertex "
            "colours, falling back to --mesh-colour when the mesh has none"
        ),
    )
    mesh.add_argument(
        "--no-fill",
        action="store_true",
        help="keep shell meshes unfilled instead of flooding the interior",
    )
    mesh.add_argument(
        "--largest-component-only",
        action="store_true",
        help=(
            "discard disconnected voxel islands outside the largest mesh "
            "component (always reported)"
        ),
    )
    scale = parser.add_mutually_exclusive_group()
    scale.add_argument(
        "--plates-per-voxel",
        type=int,
        default=None,
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
        "--subassemblies",
        action="store_true",
        help=(
            "extract persistently floating clusters as separately built "
            "subassemblies with attach steps (write .mpd output to get "
            "submodel FILE sections; .ldr flattens them)"
        ),
    )
    parser.add_argument(
        "--bom",
        type=Path,
        default=None,
        metavar="PATH",
        help="also write a bill of materials (.json for JSON, else text)",
    )
    parser.add_argument(
        "--instructions",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "also write a step-by-step instruction booklet with rendered "
            "images (.html or .pdf); step images need LeoCAD or LDView"
        ),
    )
    parser.add_argument(
        "--stability-weight",
        type=float,
        default=4.0,
        help="objective weight of physical stability (default 4.0)",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "record telemetry span timings for this run to a JSON file "
            "(single-strategy runs only; see scripts/profile_pipeline.py)"
        ),
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject invalid flag combinations in milliseconds, not after a run."""
    if args.strategy != "all" and (
        args.jobs != 0
        or args.timeout is not None
        or args.report is not None
        or args.keep_candidates is not None
        or args.seeds is not None
    ):
        parser.error(
            "--jobs/--timeout/--report/--keep-candidates/--seeds require --strategy all"
        )
    if args.subassemblies and args.steps == "layer":
        parser.error("--subassemblies requires --steps smart")
    if args.instructions is not None:
        if args.steps == "layer":
            parser.error("--instructions requires --steps smart")
        if args.instructions.suffix.lower() not in {".html", ".pdf"}:
            parser.error("--instructions must end in .html or .pdf")
    if args.input.suffix.lower() in LDRAW_SUFFIXES:
        _validate_ldraw_args(parser, args)
    mesh_input = args.input.suffix.lower() in MESH_SUFFIXES
    mesh_flags = (
        args.target_studs is not None
        or args.pitch is not None
        or args.up is not None
        or args.mesh_colour_mode is not None
        or args.mesh_colour is not None
        or args.no_fill
        or args.largest_component_only
    )
    if mesh_flags and not mesh_input:
        parser.error(
            "--target-studs/--pitch/--up/--mesh-colour/--mesh-colour-mode/"
            "--no-fill/--largest-component-only apply only to mesh inputs "
            "(.obj/.stl/.ply)"
        )
    if mesh_input and (
        args.plates_per_voxel is not None or args.aspect_correct or args.dither
    ):
        parser.error(
            "--plates-per-voxel/--aspect-correct/--dither do not apply to "
            "mesh inputs (meshes are voxelized at plate resolution and are "
            "always aspect-correct)"
        )


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    output: Path = args.output or args.input.with_suffix(".ldr")
    progress = (
        (lambda message: print(f"  {message}", file=sys.stderr, flush=True))
        if sys.stderr.isatty()
        else None
    )
    if args.input.suffix.lower() in LDRAW_SUFFIXES:
        return _run_import(args, output=output, progress=progress)
    config = PipelineConfig(
        strategy=args.strategy,
        hollow=not args.solid,
        slopes=args.slopes or False,
        tiles=args.tiles,
        snot=args.snot,
        refine=not args.no_refine,
        repair=not args.no_repair,
        seed=args.seed,
        plates_per_voxel=(
            args.plates_per_voxel if args.plates_per_voxel is not None else 3
        ),
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
            subassemblies=args.subassemblies,
        ),
        mesh=MeshOptions(
            target_studs=(args.target_studs if args.target_studs is not None else 32),
            pitch=args.pitch,
            up=args.up if args.up is not None else "z",
            colour_code=(
                args.mesh_colour
                if args.mesh_colour is not None
                else DEFAULT_MESH_COLOUR
            ),
            colour_mode=(
                args.mesh_colour_mode
                if args.mesh_colour_mode is not None
                else "uniform"
            ),
            fill=not args.no_fill,
            keep_largest=args.largest_component_only,
        ),
        weights=ObjectiveWeights(stability=args.stability_weight),
        solver=SolverConfig(mode="milp" if args.milp else "lp"),
    )
    if args.strategy == "all":
        return _run_sweep(args, config=config, output=output)
    try:
        if args.profile is not None:
            started = time.perf_counter()
            with telemetry.record() as session:
                result = run_file(
                    args.input,
                    output,
                    config,
                    bom_path=args.bom,
                    instructions_path=args.instructions,
                )
            _write_profile(
                args,
                session=session,
                result=result,
                total_seconds=time.perf_counter() - started,
            )
        else:
            result = run_file(
                args.input,
                output,
                config,
                bom_path=args.bom,
                instructions_path=args.instructions,
            )
    except (ValueError, OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return _print_result(result, output, instructions_path=args.instructions)


def _validate_ldraw_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """Reject flags that presume placement when the input is a brick model."""
    if args.strategy != "greedy":
        parser.error(
            "--strategy does not apply to .ldr/.mpd inputs (import skips placement)"
        )
    placement_flags = (
        args.solid
        or args.slopes is not None
        or args.tiles
        or args.snot
        or args.no_refine
        or args.no_repair
        or args.milp
        or args.plates_per_voxel is not None
        or args.aspect_correct
        or args.dither
    )
    if placement_flags:
        parser.error(
            "placement/voxelization flags do not apply to .ldr/.mpd "
            "inputs (the model's bricks are imported as-is)"
        )
    if args.output is None:
        parser.error(
            ".ldr/.mpd input needs an explicit -o/--output "
            "(the default would overwrite the input)"
        )


def _run_import(
    args: argparse.Namespace,
    *,
    output: Path,
    progress: Callable[[str], None] | None,
) -> int:
    """Instructions for an existing LDraw model: import, analyze, sequence.

    Placement never runs — the model's own bricks are the layout. Strict
    import: any part outside the catalog is an error.
    """
    solver = SolverConfig()
    try:
        layout = layout_from_ldraw(args.input)
    except (LdrawImportError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    stability = analyze(layout, solver)
    plan = None
    if args.steps == "smart":
        plan = plan_instructions(
            layout,
            config=InstructionsConfig(
                target_step_size=args.step_size,
                rotstep=not args.no_rotstep,
                subassemblies=args.subassemblies,
                solver=solver,
            ),
        )
    graph = ConnectionGraph.from_layout(layout)
    result = PipelineResult(
        layout=layout,
        stability=stability,
        grid=None,
        brick_count=len(layout),
        mass_g=layout.total_mass_g(),
        component_count=graph.component_count(),
        floating_count=len(graph.floating_ids()),
        plan=plan,
    )
    try:
        write_outputs(
            result,
            output,
            bom_path=args.bom,
            instructions_path=args.instructions,
            progress=progress,
        )
    except (ValueError, OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return _print_result(result, output, instructions_path=args.instructions)


def _write_profile(
    args: argparse.Namespace,
    *,
    session: telemetry.Telemetry,
    result: PipelineResult,
    total_seconds: float,
) -> None:
    """Dump a run's telemetry spans plus metadata to ``args.profile``."""
    payload = {
        "schema": 1,
        "input": str(args.input),
        "strategy": args.strategy,
        "seed": args.seed,
        "brick_count": result.brick_count,
        "step_count": result.step_count,
        "total_seconds": round(total_seconds, 3),
        "spans": session.to_dict(),
    }
    args.profile.parent.mkdir(parents=True, exist_ok=True)
    args.profile.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {args.profile}")


def _print_result(
    result: PipelineResult,
    output: Path,
    *,
    instructions_path: Path | None = None,
) -> int:
    """Print the standard result summary; returns the process exit code."""
    print(f"wrote {output}")
    if instructions_path is not None:
        print(f"wrote {instructions_path}")
    snot_note = f"   snot: {result.snot_added}" if result.snot_added else ""
    print(
        f"  bricks: {result.brick_count}   mass: {result.mass_g:.1f} g   "
        f"steps: {result.step_count}   slopes: {result.slopes_added}   "
        f"tiles: {result.tiles_added}{snot_note}"
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


def _run_sweep(
    args: argparse.Namespace,
    *,
    config: PipelineConfig,
    output: Path,
) -> int:
    """Run every strategy, report the field, and write the winning model."""
    try:
        grid = load_grid(args.input, config)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    candidates = run_all(
        grid,
        config,
        jobs=args.jobs,
        seeds=args.seeds,
        timeout_s=args.timeout,
        progress=config.progress,
    )
    _print_table(candidates)
    report = select_best(candidates)
    try:
        if args.report is not None:
            payload = {
                "input": str(args.input),
                "seed": config.seed,
                "seeds": list(args.seeds) if args.seeds is not None else None,
                "jobs": args.jobs,
                **report.to_dict(),
            }
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(payload, indent=2) + "\n")
            print(f"wrote {args.report}")
        winner = report.winner
        if winner is None or winner.result is None:
            print("error: every strategy failed", file=sys.stderr)
            for candidate in report.candidates:
                print(f"  {candidate.strategy}: {candidate.error}", file=sys.stderr)
            return 1
        if args.keep_candidates is not None:
            _write_candidates(report, directory=args.keep_candidates, output=output)
        write_outputs(
            winner.result,
            output,
            bom_path=args.bom,
            instructions_path=args.instructions,
            progress=config.progress,
        )
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    winner_tag = (
        f" (seed {winner.seed})" if len({c.seed for c in report.candidates}) > 1 else ""
    )
    print(f"selected {winner.strategy}{winner_tag}: {report.reason}")
    return _print_result(winner.result, output, instructions_path=args.instructions)


def _print_table(candidates: list[Candidate]) -> None:
    """Print one summary line per (strategy, seed) candidate."""
    multi_seed = len({candidate.seed for candidate in candidates}) > 1
    seed_header = f" {'seed':>4}" if multi_seed else ""
    print(
        f"  {'strategy':<8}{seed_header} {'bricks':>6} {'buildable':>9} "
        f"{'objective':>9} {'capacity':>8} {'seconds':>7}"
    )
    for candidate in candidates:
        seed_cell = f" {candidate.seed:>4}" if multi_seed else ""
        if (metrics := candidate.metrics) is None:
            print(
                f"  {candidate.strategy:<8}{seed_cell} "
                f"error: {candidate.error} ({candidate.seconds:.1f}s)"
            )
        else:
            print(
                f"  {candidate.strategy:<8}{seed_cell} {metrics.brick_count:>6} "
                f"{'yes' if metrics.buildable else 'no':>9} "
                f"{metrics.objective_total:>9.4f} "
                f"{metrics.maximin_capacity:>8.3f} {candidate.seconds:>7.1f}"
            )


def _write_candidates(
    report: SelectionReport,
    *,
    directory: Path,
    output: Path,
) -> None:
    """Write every successful candidate's model into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    multi_seed = len({c.seed for c in report.candidates}) > 1
    for candidate in report.candidates:
        if candidate.result is None:
            continue
        seed_tag = f".seed{candidate.seed}" if multi_seed else ""
        path = (
            directory / f"{output.stem}.{candidate.strategy}{seed_tag}{output.suffix}"
        )
        write_model(candidate.result.layout, path, plan=candidate.result.plan)
        print(f"wrote {path}")


if __name__ == "__main__":
    sys.exit(main())
