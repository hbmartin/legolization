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

from bisect import bisect_left, bisect_right
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from legolization.layout import Layout, PlacedBrick

_ID_MAX = 2**63 - 1  # sorts after any real brick id at equal coordinate


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
    rows_x: dict[tuple[int, int], list[tuple[int, int]]] = {}
    rows_y: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for (x, y, z), brick_id in layout.occupancy.items():
        columns.setdefault((x, y), []).append((z, brick_id))
        rows_x.setdefault((y, z), []).append((x, brick_id))
        rows_y.setdefault((x, z), []).append((y, brick_id))
    for line in rows_x.values():
        line.sort()
    for line in rows_y.values():
        line.sort()
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
    for brick in layout:
        if brick.brick_id not in lateral:
            blockers[brick.brick_id] |= _stud_sweep_blockers(layout, brick, columns)
    for brick_id, brick in lateral.items():
        blockers[brick_id] = _outward_ray_blockers(layout, brick, rows_x, rows_y)
    return {brick_id: frozenset(ids) for brick_id, ids in blockers.items()}


def _stud_sweep_blockers(
    layout: Layout,
    brick: PlacedBrick,
    columns: dict[tuple[int, int], list[tuple[int, int]]],
) -> set[int]:
    """Bricks swept by a carrier's protruding side studs on the way down.

    A carrier is still inserted vertically, but its side stud protrudes
    into the neighbour column and sweeps it from above: anything
    occupying that column at or above the stud's final height blocks the
    insertion (conservative full-cell model, same as the collision
    grid). Ordinary bricks have no lateral top connectors and pay one
    generator pass; bottom-up sequencing already satisfies the
    constraint, it only bites when steps are reordered.
    """
    found: set[int] = set()
    for conn in layout.connectors_of(brick, top=True):
        if conn.direction[2] == 0:  # lateral studs only
            target = (
                conn.cell[0] + conn.direction[0],
                conn.cell[1] + conn.direction[1],
            )
            found.update(
                occupant
                for z, occupant in columns.get(target, [])
                if z >= conn.cell[2] and occupant != brick.brick_id
            )
    return found


def _outward_ray_blockers(
    layout: Layout,
    brick: PlacedBrick,
    rows_x: dict[tuple[int, int], list[tuple[int, int]]],
    rows_y: dict[tuple[int, int], list[tuple[int, int]]],
) -> set[int]:
    """Bricks on the sideways part's outward slide-in path.

    The ray iterates the indexed occupied cells on the outward half-ray
    instead of stepping one coordinate at a time to the model bounds — a
    sparse LDraw import (two pieces a billion studs apart) must cost a
    bisect, not a billion occupancy lookups (PR #18 review; the fixed
    scan cap it replaced silently approved impossible insertions,
    PR #17 review).
    """
    sockets = [
        conn
        for conn in layout.connectors_of(brick, top=False)
        if conn.direction[2] == 0
    ]
    if not sockets or not layout.occupancy:
        return set()
    found: set[int] = set()
    for conn in sockets:
        ox, oy = -conn.direction[0], -conn.direction[1]
        for x, y, z in layout.cells_of(brick):
            if ox:
                line = rows_x.get((y, z), [])
                coordinate = x
                direction = ox
            else:
                line = rows_y.get((x, z), [])
                coordinate = y
                direction = oy
            if direction > 0:
                span = line[bisect_right(line, (coordinate, _ID_MAX)) :]
            else:
                span = line[: bisect_left(line, (coordinate, -1))]
            found.update(occupant for _, occupant in span if occupant != brick.brick_id)
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
