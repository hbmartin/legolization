"""Vertical block-edge computation (Ma et al.'s block relation, -z only).

A stud-up brick is inserted by a straight vertical sweep from above, so the
only insertion obstacle that matters is another brick anywhere higher in
one of its columns (the assembly-sequence paper's block edges along the
other axes never obstruct a vertical sweep). Pure bottom-up band order is
always insertion-feasible; blockers only bite when steps are reordered for
mid-build stability.

The ``Mapping[int, frozenset[int]]`` blocker-map interface is the seam for
future sideways (SNOT) building: ±X/±Y block maps computed the same way
would plug into the readiness and disassembly checks without redesign.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from legolization.layout import Layout, PlacedBrick


def vertical_blockers(layout: Layout) -> dict[int, frozenset[int]]:
    """Map each brick to the bricks obstructing its insertion sweep.

    ``blockers[b]`` must be disjoint from the already-placed set for ``b``
    to be insertable. Ordinary stud-up bricks sweep vertically, so their
    blockers are the bricks above any of their columns; sideways-mounted
    parts (a socket direction in the horizontal plane) slide in along
    their outward ray instead, so their blockers are the bricks on that
    ray at their own layers.
    """
    columns: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for (x, y, z), brick_id in layout.occupancy.items():
        columns.setdefault((x, y), []).append((z, brick_id))
    blockers: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    lateral = {
        brick.brick_id: brick
        for brick in layout
        if any(
            conn.direction[2] == 0 for conn in layout.connectors_of(brick, top=False)
        )
    }
    for cells in columns.values():
        cells.sort()
        above: set[int] = set()
        for z, brick_id in reversed(cells):
            del z
            if brick_id not in lateral:
                blockers[brick_id] |= above - {brick_id}
            above.add(brick_id)
    for brick_id, brick in lateral.items():
        blockers[brick_id] = _outward_ray_blockers(layout, brick)
    return {brick_id: frozenset(ids) for brick_id, ids in blockers.items()}


def _outward_ray_blockers(layout: Layout, brick: PlacedBrick) -> set[int]:
    """Bricks on the sideways part's outward slide-in path."""
    sockets = [
        conn
        for conn in layout.connectors_of(brick, top=False)
        if conn.direction[2] == 0
    ]
    found: set[int] = set()
    max_reach = 64  # far beyond any model extent
    for conn in sockets:
        ox, oy = -conn.direction[0], -conn.direction[1]
        for cell in layout.cells_of(brick):
            x, y, z = cell
            for step in range(1, max_reach):
                other = layout.brick_at((x + ox * step, y + oy * step, z))
                if other is None:
                    continue
                if other.brick_id != brick.brick_id:
                    found.add(other.brick_id)
    return found


def chunk_ready(
    chunk: tuple[int, ...],
    placed: set[int],
    supports: Mapping[int, set[int]],
    blockers: Mapping[int, frozenset[int]],
    blocks: Mapping[int, set[int]],
) -> bool:
    """Whether a chunk can be inserted onto ``placed`` right now.

    Ready means every member's supports are placed, no placed brick blocks
    its vertical insertion, and — pull-forward safety — placing it cannot
    strand a still-unplaced brick under a new overhang.
    """
    chunk_set = set(chunk)
    settled = placed | chunk_set
    for brick_id in chunk:
        if not supports[brick_id] <= placed:
            return False
        if blockers[brick_id] & placed:
            return False
        if not blocks[brick_id] <= settled:
            return False
    return True
