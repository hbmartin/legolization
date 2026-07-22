"""LDraw emission: grid placements → pyldraw3 pieces → .ldr/.mpd files.

Coordinate transform (LDraw Y points **down**):

- ``X = 20·x`` and ``Z = 20·y`` (one stud = 20 LDU),
- ``Y = -8·(layer + height_plates)`` — LDraw part origins sit at the top
  face of the body (stud base plane) with the body extending downward, so a
  part whose bottom rests on grid ``layer`` has its origin one part-height
  above it.

Bricks are emitted bottom-up by layer with a ``0 STEP`` meta after each
layer, which doubles as buildable stud-up instructions.
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import PurePath
from typing import TYPE_CHECKING

from ldraw.geometry import Identity, Matrix, Vector, YAxis
from ldraw.pieces import Piece

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from legolization.catalog import Part
    from legolization.instructions.sequencer import InstructionPlan
    from legolization.layout import Layout, PlacedBrick
    from legolization.stability.solver import BrickScore

STUD_LDU = 20.0
PLATE_LDU = 8.0

_HEADER_LICENSE = "0 !LICENSE Licensed under GPL-3.0-or-later"


def piece_for(layout: Layout, brick: PlacedBrick) -> Piece:
    """Build the pyldraw3 piece for one placed brick.

    Claddings (sideways facade parts) have their own origin convention;
    everything else — including carriers, whose bodies are ordinary
    bricks with a data-driven extra emission yaw — takes the standard
    footprint-centroid path.
    """
    part = layout.part_of(brick)
    if part.mount_normal is not None:
        return _cladding_piece(layout, brick, part)
    columns = [
        (brick.x + rx, brick.y + ry) for rx, ry in _rotated_footprint(layout, brick)
    ]
    center_x = sum(c[0] for c in columns) / len(columns)
    center_y = sum(c[1] for c in columns) / len(columns)
    offset_x, offset_y, offset_z = part.origin_offset
    rotated_ox, rotated_oz = _rotate_ldu(offset_x, offset_z, brick.yaw)
    position = Vector(
        STUD_LDU * center_x + rotated_ox,
        -PLATE_LDU * (brick.layer + part.height_plates) + offset_y,
        STUD_LDU * center_y + rotated_oz,
    )
    emit_yaw = (brick.yaw + part.emit_yaw_offset) % 360
    matrix = Identity().rotate(emit_yaw, YAxis) if emit_yaw else Identity()
    return Piece(
        colour=brick.colour_code,
        position=position,
        matrix=matrix,
        part=part.ldraw_part,
    )


def _fmt(value: float) -> str:
    """LDraw numeric formatting: integers stay integral."""
    return f"{value:g}"


def _cladding_piece(layout: Layout, brick: PlacedBrick, part: Part) -> Piece:
    """Emit a sideways facade part hanging its smooth face outward.

    Outward is the yaw-rotated negation of the part's socket direction;
    the emission rotation for that direction is pinned catalog data
    (``mount_matrices``). The origin sits at the clad columns' centroid
    shifted by ``mount_offset_ldu`` along ``(outward, vertical,
    transverse)`` — vertical measured from the window's bottom plate,
    transverse being outward rotated 90° counterclockwise.
    """
    from legolization.catalog import rotate_offset  # noqa: PLC0415 - cycle guard

    if (normal := part.mount_normal) is None:  # dispatched on mount_normal
        msg = f"{part.key} is not a cladding"
        raise ValueError(msg)
    ox, oy, _ = rotate_offset((-normal[0], -normal[1], 0), brick.yaw)
    rows = part.mount_matrix((ox, oy))
    if rows is None:
        msg = f"{part.key} pins no mount matrix for outward {(ox, oy)}"
        raise ValueError(msg)
    matrix = Matrix(rows=[list(rows[0:3]), list(rows[3:6]), list(rows[6:9])])
    columns = [
        (brick.x + rx, brick.y + ry) for rx, ry in _rotated_footprint(layout, brick)
    ]
    center_x = sum(c[0] for c in columns) / len(columns)
    center_y = sum(c[1] for c in columns) / len(columns)
    offset_out, offset_up, offset_across = part.mount_offset_ldu
    across_x, across_y = -oy, ox
    position = Vector(
        STUD_LDU * center_x + offset_out * ox + offset_across * across_x,
        -PLATE_LDU * brick.layer + offset_up,
        STUD_LDU * center_y + offset_out * oy + offset_across * across_y,
    )
    return Piece(
        colour=brick.colour_code,
        position=position,
        matrix=matrix,
        part=part.ldraw_part,
    )


def model_lines(
    layout: Layout,
    *,
    name: str = "model.ldr",
    steps: bool = True,
    plan: InstructionPlan | None = None,
    submodels: bool = False,
) -> Iterator[str]:
    """LDraw file lines for a layout.

    Without a ``plan`` this is the legacy per-layer emission (one ``0
    STEP`` per plate layer, bottom-up). With a plan the steps follow its
    sequencing, with ``0 ROTSTEP`` view hints where the planner asked for
    them (``0 STEP`` boundaries are always emitted, so viewers that treat
    ROTSTEP as a comment still step correctly). ``submodels`` switches
    attach steps to submodel reference lines (``.mpd`` emission).
    """
    # PurePath.stem is case-insensitive about the suffix; the previous
    # literal removesuffix left "MODEL.MPD" intact, so the root's
    # submodel references and the FILE sections disagreed (PR #17
    # review) and viewers lost the subassemblies.
    stem = PurePath(name).stem
    yield f"0 {stem}"
    yield f"0 Name: {name}"
    yield "0 Author: legolization"
    yield "0 !LDRAW_ORG Unofficial_Model"
    yield _HEADER_LICENSE
    if plan is not None:
        yield from _plan_lines(layout, plan, submodels=submodels, stem=stem)
        return
    ordered = sorted(layout, key=lambda b: (b.layer, b.y, b.x, b.brick_id))
    previous_layer: int | None = None
    for brick in ordered:
        if steps and previous_layer is not None and brick.layer != previous_layer:
            yield "0 STEP"
        previous_layer = brick.layer
        yield piece_for(layout, brick).to_ldraw()
    if steps and ordered:
        yield "0 STEP"


def _plan_lines(
    layout: Layout,
    plan: InstructionPlan,
    *,
    submodels: bool = False,
    stem: str = "model",
) -> Iterator[str]:
    """Yield the main model's step lines.

    With ``submodels`` attach steps emit a reference line placing the
    subassembly's FILE section as a unit; without, they flatten to the
    sub's bricks in world frame.
    """
    subs = {sub.name: sub for sub in plan.subassemblies}
    rotated = False
    for step in plan.steps:
        if step.submodel is not None:
            continue  # sub-build steps live in their own FILE section
        if step.rotstep is not None:
            rotated = True
            yield f"0 ROTSTEP 0 {step.rotstep.yaw} 0 {step.rotstep.mode}"
        if step.attaches is not None:
            sub = subs[step.attaches]
            if submodels:
                offset_y = -PLATE_LDU * sub.anchor_layer
                yield (
                    f"1 16 0 {_fmt(offset_y)} 0 1 0 0 0 1 0 0 0 1 "
                    f"{_submodel_file(stem, sub.name)}"
                )
            else:
                for brick_id in _world_order(layout, sub.brick_ids):
                    yield piece_for(layout, layout.bricks[brick_id]).to_ldraw()
        for brick_id in step.brick_ids:
            yield piece_for(layout, layout.bricks[brick_id]).to_ldraw()
        yield "0 STEP"
    if rotated:
        yield "0 ROTSTEP END"


def _world_order(layout: Layout, brick_ids: tuple[int, ...]) -> list[int]:
    return sorted(
        brick_ids,
        key=lambda bid: (
            layout.bricks[bid].layer,
            layout.bricks[bid].y,
            layout.bricks[bid].x,
            bid,
        ),
    )


def _submodel_name(stem: str, sub_name: str) -> str:
    """Build the stem-qualified submodel identity for titles and filenames."""
    return f"{stem}-{sub_name}"


def _submodel_file(stem: str, sub_name: str) -> str:
    """Canonical FILE-section filename for a subassembly (single source)."""
    return f"{_submodel_name(stem, sub_name)}.ldr"


def _submodel_lines(
    layout: Layout,
    plan: InstructionPlan,
    sub_name: str,
) -> Iterator[str]:
    """One subassembly's FILE section body, in its grounded local frame."""
    subs = {sub.name: sub for sub in plan.subassemblies}
    sub = subs[sub_name]
    local = layout.subset(sub.brick_ids).translated(dz=sub.anchor_layer)
    for step in plan.sub_steps(sub_name):
        for brick_id in step.brick_ids:
            yield piece_for(local, local.bricks[brick_id]).to_ldraw()
        yield "0 STEP"


def write_model(
    layout: Layout,
    path: Path,
    *,
    steps: bool = True,
    plan: InstructionPlan | None = None,
) -> None:
    """Write a layout to ``.ldr`` (or ``.mpd`` with FILE wrappers).

    Plans with subassemblies need ``.mpd`` output to carry the submodel
    FILE sections; ``.ldr`` output flattens each attach step to the
    subassembly's bricks in the world frame (same main step count).
    """
    name = path.name
    stem = path.stem
    is_mpd = path.suffix.lower() == ".mpd"
    with_subs = plan is not None and bool(plan.subassemblies)
    lines = list(
        model_lines(
            layout, name=name, steps=steps, plan=plan, submodels=is_mpd and with_subs
        )
    )
    if is_mpd:
        lines = [f"0 FILE {name}", *lines, "0 NOFILE"]
        if with_subs and plan is not None:
            for sub in plan.subassemblies:
                sub_file = _submodel_file(stem, sub.name)
                lines.extend(
                    (
                        f"0 FILE {sub_file}",
                        f"0 {_submodel_name(stem, sub.name)}",
                        f"0 Name: {sub_file}",
                        "0 Author: legolization",
                        "0 !LDRAW_ORG Unofficial_Model",
                        _HEADER_LICENSE,
                    )
                )
                lines.extend(_submodel_lines(layout, plan, sub.name))
                lines.append("0 NOFILE")
    path.write_text("\n".join(lines) + "\n")


_HEATMAP_RAMP: tuple[tuple[float, int], ...] = (
    (0.125, 0),  # black: at rest
    (0.375, 320),  # dark red
    (0.625, 4),  # red
    (0.875, 12),  # light red
    (float("inf"), 15),  # white: at or beyond capacity
)


def heatmap_colour(score: float) -> int:
    """LDraw palette code for one stability score.

    StableLego's ramp — black (at rest) through red to white (at or
    beyond capacity) — quantized to five stock palette codes because the
    working headless renderer (LeoCAD) draws LDraw direct colours
    (``0x2RRGGBB``) as unresolved grey.
    """
    return next(
        code for threshold, code in _HEATMAP_RAMP if max(score, 0.0) < threshold
    )


def write_heatmap(
    layout: Layout,
    scores: Mapping[int, BrickScore],
    path: Path,
) -> None:
    """Write a copy of ``layout`` recoloured by per-brick stability score.

    Bricks absent from ``scores`` (an empty result's trivial verdict)
    render as score 0. The copy carries no plan or steps — the heatmap
    is a diagnostic view, not a build document.
    """
    heat = layout.copy()
    for brick_id, brick in heat.bricks.items():
        entry = scores.get(brick_id)
        heat.bricks[brick_id] = replace(
            brick,
            colour_code=heatmap_colour(entry.score if entry is not None else 0.0),
        )
    write_model(heat, path, steps=False)


def _rotated_footprint(
    layout: Layout,
    brick: PlacedBrick,
) -> list[tuple[int, int]]:
    from legolization.catalog import rotate_offset  # noqa: PLC0415 - cycle guard

    part = layout.part_of(brick)
    rotated = [
        rotate_offset((dx, dy, 0), brick.yaw) for dx, dy in sorted(part.footprint)
    ]
    return [(rx, ry) for rx, ry, _ in rotated]


def _rotate_ldu(x: float, z: float, yaw: int) -> tuple[float, float]:
    """Rotate a local LDU offset by yaw about the vertical axis."""
    radians = math.radians(yaw)
    cos_a = round(math.cos(radians))
    sin_a = round(math.sin(radians))
    return (cos_a * x - sin_a * z, sin_a * x + cos_a * z)
