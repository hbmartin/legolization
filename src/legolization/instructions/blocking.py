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
