"""Vertical block-edge computation (Ma et al.'s block relation, -z only).

A stud-up brick is inserted by a straight vertical sweep from above, so the
only insertion obstacle that matters is another brick anywhere higher in
one of its columns (the assembly-sequence paper's block edges along the
other axes never obstruct a vertical sweep). Pure bottom-up band order is
always insertion-feasible; blockers only bite when steps are reordered for
mid-build stability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from legolization.layout import Layout


def vertical_blockers(layout: Layout) -> dict[int, frozenset[int]]:
    """Map each brick to the bricks above any of its columns.

    ``blockers[b]`` must be disjoint from the already-placed set for ``b``
    to be insertable by a vertical sweep.
    """
    columns: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for (x, y, z), brick_id in layout.occupancy.items():
        columns.setdefault((x, y), []).append((z, brick_id))
    blockers: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    for cells in columns.values():
        cells.sort()
        above: set[int] = set()
        for z, brick_id in reversed(cells):
            del z
            blockers[brick_id] |= above - {brick_id}
            above.add(brick_id)
    return {brick_id: frozenset(ids) for brick_id, ids in blockers.items()}
