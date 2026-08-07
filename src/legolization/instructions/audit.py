"""Machine-check a model's build instructions for sensibility.

Re-runs the (deterministic) pipeline on an input, then audits the resulting
instruction plan: ``verify_plan`` invariants, ``plan_quality`` aggregates,
and a per-step after-state — floating (dangling) bricks and component count
of every prefix — that the plan's own verdicts don't cover. Optionally dumps
per-step PNGs so the steps can be inspected visually.

Usage::

    uv run python -m legolization.instructions.audit INPUT [--strategy greedy]
        [--seed 0] [--step-size 7] [--json PATH|-] [--render-dir DIR]
        [--target-studs N] [--up x|y|z]

Exit codes: 0 = clean, 2 = warnings only (unstable or flagged steps),
1 = plan invariant violations.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from legolization.graph import ConnectionGraph
from legolization.instructions.metrics import plan_quality
from legolization.instructions.render import RenderConfig, render_step_images
from legolization.instructions.sequencer import (
    InstructionsConfig,
    verify_plan,
)
from legolization.ldraw_out import write_model
from legolization.mesh import MeshOptions
from legolization.pipeline import PipelineConfig, PipelineResult, load_grid, run
from legolization.stability.solver import SolverConfig, analyze

if TYPE_CHECKING:
    from collections.abc import Callable

    from legolization.instructions.sequencer import InstructionPlan
    from legolization.layout import Layout


def check_steps(
    result_layout: Layout,
    plan: InstructionPlan,
    max_step_size: int,
    *,
    insertion_mass_kg: float | None = None,
    solver: SolverConfig | None = None,
) -> list[dict]:
    """Audit each step's after-state; returns one JSON-safe row per step.

    Sub-build steps are audited in the subassembly's own grounded frame
    (the unit sits on the table); attach steps merge the whole unit into
    the world before the check.
    """
    subs = {sub.name: sub for sub in plan.subassemblies}
    sub_placed: dict[str, set[int]] = {name: set() for name in subs}
    rows: list[dict] = []
    placed: set[int] = set()
    for step in plan.steps:
        if step.submodel is not None:
            sub = subs[step.submodel]
            seen = sub_placed[step.submodel]
            seen |= set(step.brick_ids)
            audit_layout = result_layout.subset(seen).translated(dz=sub.anchor_layer)
            size = len(step.brick_ids)
        else:
            if step.attaches is not None:
                placed |= set(subs[step.attaches].brick_ids)
                size = len(subs[step.attaches].brick_ids)
            else:
                size = len(step.brick_ids)
            placed |= set(step.brick_ids)
            audit_layout = result_layout.subset(placed)
        graph = ConnectionGraph.from_layout(audit_layout)
        floating_after = len(graph.floating_ids())
        # "attach" is neutral metadata (the ``attaches`` field), never a
        # flag: flags drive the exit-2 warning status, and a valid
        # subassembly plan must be able to come back clean (PR #17
        # review).
        flags = []
        if floating_after:
            flags.append("floating")
        if not step.prefix_stable:
            flags.append("unstable-prefix")
        if len(step.brick_ids) > max_step_size:
            flags.append("oversized")
        # Attach steps place no direct bricks: press the whole seated
        # unit instead of skipping them (PR #20 review, severity 2).
        pressed_ids = (
            subs[step.attaches].brick_ids
            if step.attaches is not None
            else step.brick_ids
        )
        if (
            insertion_mass_kg is not None
            and step.prefix_stable
            and pressed_ids
            and not analyze(
                audit_layout,
                solver,
                extra_masses=dict.fromkeys(pressed_ids, insertion_mass_kg),
            ).stable
        ):
            # Statically fine, but pressing the new bricks home would
            # collapse the prefix (Liu et al. 2024, virtual-brick model).
            flags.append("insertion-fragile")
        rows.append(
            {
                "index": step.index,
                "size": size,
                "prefix_stable": step.prefix_stable,
                "prefix_max_score": round(step.prefix_max_score, 4),
                "floating_after": floating_after,
                "components_after": graph.component_count(),
                "rotstep": step.rotstep.yaw if step.rotstep else None,
                "submodel": step.submodel,
                "attaches": step.attaches,
                "flags": flags,
            }
        )
    return rows


def _dump_step_images(
    result: PipelineResult,
    render_dir: Path,
    progress: Callable[[str], None],
) -> list[str]:
    """Render per-step PNGs into ``render_dir``; returns warnings."""
    render_dir.mkdir(parents=True, exist_ok=True)
    if result.plan is None:
        return ["no plan to render"]
    with tempfile.TemporaryDirectory() as tmp:
        suffix = ".mpd" if result.plan.subassemblies else ".ldr"
        model_path = Path(tmp) / f"model{suffix}"
        write_model(result.layout, model_path, plan=result.plan)
        images = render_step_images(
            model_path,
            result.plan,
            config=RenderConfig(),
            progress=progress,
        )
    written = 0
    for index, image in enumerate(images.images, start=1):
        if image is None:
            continue
        (render_dir / f"step-{index:03d}.png").write_bytes(image)
        written += 1
    progress(f"wrote {written}/{len(images.images)} step images to {render_dir}")
    return list(images.warnings)


def _positive_mass(text: str) -> float:
    """Reject non-finite or non-positive press masses at the boundary."""
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        msg = f"must be a finite positive mass, got {text}"
        raise argparse.ArgumentTypeError(msg)
    return value


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--strategy", default="greedy")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--step-size", type=int, default=7)
    parser.add_argument(
        "--subassemblies", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument(
        "--insertion-check",
        action="store_true",
        help=(
            "re-analyze each prefix with a press mass on the just-placed "
            "chunk (Liu et al. 2024's virtual-brick insertion model); "
            "statically-stable-but-press-fragile steps gain the "
            "insertion-fragile flag"
        ),
    )
    parser.add_argument(
        "--insertion-mass-kg",
        type=_positive_mass,
        default=1.0,
        metavar="KG",
    )
    parser.add_argument("--json", dest="json_path", default=None, metavar="PATH")
    parser.add_argument("--render-dir", type=Path, default=None, metavar="DIR")
    parser.add_argument("--target-studs", type=int, default=32, metavar="N")
    parser.add_argument("--up", choices=("x", "y", "z"), default="z")
    args = parser.parse_args(argv)

    def progress(message: str) -> None:
        print(f"  {message}", file=sys.stderr)

    config = PipelineConfig(
        strategy=args.strategy,
        seed=args.seed,
        instructions=InstructionsConfig(
            target_step_size=args.step_size,
            subassemblies=args.subassemblies is not False,
            # With the check on the SEQUENCER avoids fragile orderings,
            # so the audit below measures the residual, not the raw count.
            insertion_check=args.insertion_check,
            insertion_mass_kg=args.insertion_mass_kg,
        ),
        mesh=MeshOptions(target_studs=args.target_studs, up=args.up),
        progress=progress,
    )
    # The module CLI kept verbatim from the retired checker script.
    # lizard forgives(cyclomatic_complexity)
    grid = load_grid(args.input, config)
    result = run(grid, config)
    if result.plan is None:
        print("error: pipeline produced no instruction plan", file=sys.stderr)
        return 1

    instructions_config = (
        config.instructions
        if config.instructions.solver is not None
        else replace(config.instructions, solver=config.solver)
    )
    violations = verify_plan(result.layout, result.plan, config=instructions_config)
    quality = plan_quality(result.plan)
    steps = check_steps(
        result.layout,
        result.plan,
        instructions_config.max_step_size,
        insertion_mass_kg=args.insertion_mass_kg if args.insertion_check else None,
        solver=instructions_config.solver,
    )
    warnings = list(result.plan.warnings)
    if args.render_dir is not None:
        warnings.extend(_dump_step_images(result, args.render_dir, progress))

    flagged = [row for row in steps if row["flags"]]
    payload = {
        "input": str(args.input),
        "strategy": args.strategy,
        "seed": args.seed,
        "brick_count": result.brick_count,
        "buildable": result.buildable,
        "violations": violations,
        "warnings": warnings,
        "quality": {
            "step_count": quality.step_count,
            "unstable_steps": quality.unstable_steps,
            "max_prefix_score": round(quality.max_prefix_score, 4),
            "mean_prefix_score": round(quality.mean_prefix_score, 4),
            "subassembly_count": quality.subassembly_count,
            "attach_steps": quality.attach_steps,
        },
        "flagged_steps": [row["index"] for row in flagged],
        "steps": steps,
    }
    _emit_json(payload, args.json_path)
    report_stream = sys.stderr if args.json_path == "-" else sys.stdout
    for violation in violations:
        print(f"VIOLATION: {violation}", file=report_stream)
    for warning in warnings:
        print(f"warning: {warning}", file=report_stream)
    for row in flagged:
        print(
            f"flagged step {row['index']}: {', '.join(row['flags'])}",
            file=report_stream,
        )
    print(
        f"{quality.step_count} steps, {quality.unstable_steps} unstable, "
        f"max prefix score {quality.max_prefix_score:.4f}, "
        f"{len(flagged)} flagged",
        file=report_stream,
    )
    if violations:
        return 1
    if flagged or warnings or quality.unstable_steps:
        return 2
    return 0


def _emit_json(payload: dict, json_path: str | None) -> None:
    """Write the payload to a file, stdout for ``-``, or nowhere for None."""
    if json_path == "-":
        print(json.dumps(payload, indent=2))
    elif json_path is not None:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    sys.exit(main())


AUDIT_SCHEMA = "legolization.instructions-audit/v1"


def reconstruct_plan(imported: object) -> InstructionPlan:
    """Rebuild a flat instruction plan from a model's STEP annotations.

    Groups bricks by their source step so an existing step-annotated
    ``.ldr``/``.mpd`` can be audited without re-running placement.
    Steps declare ``prefix_stable=True``; the certification's cold
    physics decides whether the declared order actually holds up.
    """
    from collections import defaultdict  # noqa: PLC0415

    from legolization.instructions.bom import bill_of_materials  # noqa: PLC0415
    from legolization.instructions.sequencer import (  # noqa: PLC0415
        BuildStep,
        InstructionPlan,
    )
    from legolization.ldraw_in import ImportedLdrawModel  # noqa: PLC0415

    assert isinstance(imported, ImportedLdrawModel)  # noqa: S101 - internal seam
    groups: dict[int, list[int]] = defaultdict(list)
    for brick in imported.layout:
        ref = imported.source_refs.get(brick.brick_id)
        step_no = 1
        if ref is not None:
            step_no = ref.global_step or ref.source_step or 1
        groups[step_no].append(brick.brick_id)
    steps = tuple(
        BuildStep(
            index=index,
            brick_ids=tuple(sorted(groups[key])),
            prefix_stable=True,
            prefix_max_score=0.0,
        )
        for index, key in enumerate(sorted(groups), start=1)
    )
    return InstructionPlan(
        steps=steps,
        warnings=(),
        bom=bill_of_materials(imported.layout),
    )


def audit_model(
    path: Path,
    *,
    insertion_mass_kg: float = 1.0,
    render_dir: Path | None = None,
) -> dict:
    """Audit an existing step-annotated model file.

    Returns the ``legolization.instructions-audit/v1`` report payload;
    the ``verdict`` key is ``certified``, ``findings``, or
    ``infeasible`` (the completed model itself is disconnected).
    """
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415
    from legolization.instructions.sequencer import (  # noqa: PLC0415
        automatic_target_step_size,
    )
    from legolization.instructions.verification import (  # noqa: PLC0415
        certify_instructions,
    )
    from legolization.ldraw_in import import_ldraw  # noqa: PLC0415

    imported = import_ldraw(path)
    plan = reconstruct_plan(imported)
    layout = imported.layout
    config = InstructionsConfig(
        insertion_check=True,
        insertion_mass_kg=insertion_mass_kg,
        solver=SolverConfig(),
    )
    certification = certify_instructions(layout, plan, config=config)
    target = automatic_target_step_size(len(layout))
    rows = check_steps(
        layout,
        plan,
        target,
        insertion_mass_kg=insertion_mass_kg,
        solver=config.solver,
    )
    render_warnings: list[str] = []
    if render_dir is not None:
        render_warnings = _render_existing_steps(path, plan, render_dir)
    final = rows[-1] if rows else None
    infeasible = final is not None and (
        final["components_after"] != 1 or final["floating_after"] > 0
    )
    flagged = [row for row in rows if row["flags"]]
    findings = bool(
        certification.violations or certification.earliest_failure or flagged
    )
    if infeasible:
        verdict = "infeasible"
    elif findings:
        verdict = "findings"
    else:
        verdict = "certified"
    return {
        "schema": AUDIT_SCHEMA,
        "input": {
            "filename": path.name,
            "sha256": input_sha256(path),
            "brick_count": len(layout),
            "step_count": len(plan.steps),
            "has_explicit_steps": imported.has_explicit_steps,
        },
        "target_step_size": target,
        "certification": {
            "valid": certification.valid,
            "violations": list(certification.violations),
            "cold_prefix_count": certification.cold_prefix_count,
            "earliest_failure": (
                dataclasses.asdict(certification.earliest_failure)
                if certification.earliest_failure is not None
                else None
            ),
        },
        "steps": rows,
        "quality": dataclasses.asdict(plan_quality(plan)),
        "render_warnings": render_warnings,
        "verdict": verdict,
    }


def _render_existing_steps(
    model_path: Path,
    plan: InstructionPlan,
    render_dir: Path,
) -> list[str]:
    """Render the annotated model's own steps for visual inspection."""
    render_dir.mkdir(parents=True, exist_ok=True)
    images = render_step_images(model_path, plan)
    for index, png in enumerate(images.images, start=1):
        if png is not None:
            (render_dir / f"step-{index:03d}.png").write_bytes(png)
    return list(images.warnings)
