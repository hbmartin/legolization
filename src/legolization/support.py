"""Optional physical support-plate emission for generated layouts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.placement.carve import refill_tiling

if TYPE_CHECKING:
    from legolization.layout import Layout


def emit_support_plate(layout: Layout, *, colour_code: int = 7) -> tuple[int, ...]:
    """Lift a layout one plate and exactly tile its original ground footprint."""
    ground_cells = {(x, y, 0) for x, y, z in layout.occupancy if z == 0}
    if not ground_cells:
        return ()
    lifted = layout.translated(dz=-1)
    colours = dict.fromkeys(ground_cells, colour_code)
    tiling = refill_tiling(lifted, ground_cells, colours)
    if tiling is None:
        msg = "ground footprint cannot be tiled by physical support plates"
        raise ValueError(msg)
    support_ids: list[int] = []
    for part_key, anchor, yaw, colour in tiling:
        brick = lifted.add(part_key, *anchor, yaw, colour)
        support_ids.append(brick.brick_id)
    layout.replace_with(lifted)
    return tuple(support_ids)
