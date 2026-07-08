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
from typing import TYPE_CHECKING

from ldraw.geometry import Identity, Vector, YAxis
from ldraw.pieces import Piece

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from legolization.layout import Layout, PlacedBrick

STUD_LDU = 20.0
PLATE_LDU = 8.0

_HEADER_LICENSE = "0 !LICENSE Licensed under GPL-3.0-or-later"


def piece_for(layout: Layout, brick: PlacedBrick) -> Piece:
    """Build the pyldraw3 piece for one placed brick."""
    part = layout.part_of(brick)
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
    matrix = Identity().rotate(brick.yaw, YAxis) if brick.yaw else Identity()
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
) -> Iterator[str]:
    """LDraw file lines for a layout, bottom-up with per-layer steps."""
    yield f"0 {name.removesuffix('.ldr').removesuffix('.mpd')}"
    yield f"0 Name: {name}"
    yield "0 Author: legolization"
    yield "0 !LDRAW_ORG Unofficial_Model"
    yield _HEADER_LICENSE
    ordered = sorted(layout, key=lambda b: (b.layer, b.y, b.x, b.brick_id))
    previous_layer: int | None = None
    for brick in ordered:
        if steps and previous_layer is not None and brick.layer != previous_layer:
            yield "0 STEP"
        previous_layer = brick.layer
        yield piece_for(layout, brick).to_ldraw()
    if steps and ordered:
        yield "0 STEP"


def write_model(
    layout: Layout,
    path: Path,
    *,
    steps: bool = True,
) -> None:
    """Write a layout to ``.ldr`` (or ``.mpd`` with a FILE wrapper)."""
    name = path.name
    lines = list(model_lines(layout, name=name, steps=steps))
    if path.suffix.lower() == ".mpd":
        lines = [f"0 FILE {name}", *lines, "0 NOFILE"]
    path.write_text("\n".join(lines) + "\n")


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
