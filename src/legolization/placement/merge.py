"""Color-bounded merge/split engine (Luo-style, spatially indexed).

The engine starts from 1x1 atoms (bricks on absolute 3-plate slabs, plates
elsewhere), then repeatedly merges random mergeable neighbour pairs until no
merge is possible ("maximal" layout). Two bricks merge iff they share a
layer, height, and colour, and their union footprint is a solid rectangle
that exists in the catalog. Splitting a region back to atoms enables the
k-ring reconfiguration loop of the refinement strategies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.catalog import Category
from legolization.graph import ConnectionGraph
from legolization.layout import Layout

if TYPE_CHECKING:
    import numpy as np

    from legolization.catalog import Catalog, Cell
    from legolization.grid import VoxelGrid
    from legolization.layout import PlacedBrick

_MERGEABLE = (Category.BRICK, Category.PLATE)
_RING_GROWTH = 10  # failures per extra reconfiguration ring (Luo's N)


def place_rect(  # noqa: PLR0913 - a rect placement is naturally seven scalars
    layout: Layout,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    layer: int,
    height_plates: int,
    colour_code: int,
) -> PlacedBrick:
    """Place the catalog part covering the inclusive rect ``(x0,y0)-(x1,y1)``.

    Catalog footprints run length-along-local-dx; when the rect is longer in
    y the part is placed at yaw 90, whose rotation maps local dx to world +y
    and local dy to world -x (hence the max-x anchor).
    """
    x_len = x1 - x0 + 1
    y_len = y1 - y0 + 1
    key = layout.catalog.rect_key(x_len, y_len, height_plates)
    if key is None:
        msg = f"no catalog part for {x_len}x{y_len}x{height_plates}"
        raise ValueError(msg)
    if y_len <= x_len:
        return layout.add(key, x0, y0, layer, 0, colour_code)
    return layout.add(key, x1, y0, layer, 90, colour_code)


def atomize(grid: VoxelGrid, catalog: Catalog) -> Layout:
    """Cover every filled voxel with 1x1 atoms.

    Columns are chunked on absolute 3-plate slabs so brick atoms in adjacent
    columns line up (mergeable); colour changes or partial slabs fall back to
    plates.
    """
    layout = Layout(catalog=catalog)
    codes = grid.codes
    nx, ny, nz = grid.shape
    for x in range(nx):
        for y in range(ny):
            z = 0
            while z < nz:
                code = int(codes[x, y, z])
                if code < 0:
                    z += 1
                    continue
                slab_aligned = z % 3 == 0
                slab_uniform = z + 3 <= nz and all(
                    int(codes[x, y, z + dz]) == code for dz in (1, 2)
                )
                if slab_aligned and slab_uniform:
                    layout.add("brick_1x1", x, y, z, 0, code)
                    z += 3
                else:
                    layout.add("plate_1x1", x, y, z, 0, code)
                    z += 1
    return layout


def neighbour_ids(layout: Layout, brick_id: int) -> set[int]:
    """Ids of bricks sharing a face (side or vertical) with this brick."""
    result: set[int] = set()
    brick = layout.bricks[brick_id]
    for x, y, z in layout.cells_of(brick):
        for dx, dy, dz in (
            (1, 0, 0),
            (-1, 0, 0),
            (0, 1, 0),
            (0, -1, 0),
            (0, 0, 1),
            (0, 0, -1),
        ):
            neighbour = layout.brick_at((x + dx, y + dy, z + dz))
            if neighbour is not None and neighbour.brick_id != brick_id:
                result.add(neighbour.brick_id)
    return result


def k_ring(layout: Layout, seed_ids: set[int], k: int) -> set[int]:
    """Grow ``k`` rings of face-neighbours around the seed bricks."""
    region = {bid for bid in seed_ids if bid in layout.bricks}
    frontier = set(region)
    for _ in range(k):
        grown: set[int] = set()
        for brick_id in frontier:
            grown |= neighbour_ids(layout, brick_id)
        frontier = grown - region
        if not frontier:
            break
        region |= frontier
    return region


def merged_rect(
    layout: Layout,
    a: PlacedBrick,
    b: PlacedBrick,
) -> tuple[int, int, int, int] | None:
    """Return the union rect ``(x0, y0, x1, y1)`` if a+b can merge."""
    part_a = layout.part_of(a)
    part_b = layout.part_of(b)
    if (
        part_a.category not in _MERGEABLE
        or part_b.category not in _MERGEABLE
        or a.layer != b.layer
        or part_a.height_plates != part_b.height_plates
        or a.colour_code != b.colour_code
    ):
        return None
    columns = {(x, y) for x, y, _ in layout.cells_of(a)}
    columns |= {(x, y) for x, y, _ in layout.cells_of(b)}
    xs = [x for x, _ in columns]
    ys = [y for _, y in columns]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if (x1 - x0 + 1) * (y1 - y0 + 1) != len(columns):
        return None  # union is not a solid rectangle
    if layout.catalog.rect_key(x1 - x0 + 1, y1 - y0 + 1, part_a.height_plates) is None:
        return None
    return (x0, y0, x1, y1)


def maximal_random_merge(
    layout: Layout,
    rng: np.random.Generator,
) -> None:
    """Merge random mergeable pairs in place until the layout is maximal."""
    pairs: set[tuple[int, int]] = set()
    for brick in list(layout):
        for other_id in neighbour_ids(layout, brick.brick_id):
            pairs.add(_ordered(brick.brick_id, other_id))
    pending = list(pairs)
    while pending:
        index = int(rng.integers(len(pending)))
        pending[index], pending[-1] = pending[-1], pending[index]
        a_id, b_id = pending.pop()
        if a_id not in layout.bricks or b_id not in layout.bricks:
            continue
        a = layout.bricks[a_id]
        b = layout.bricks[b_id]
        if (rect := merged_rect(layout, a, b)) is None:
            continue
        layer = a.layer
        height = layout.part_of(a).height_plates
        colour = a.colour_code
        layout.remove(a_id)
        layout.remove(b_id)
        merged = place_rect(layout, *rect, layer, height, colour)
        pending.extend(
            _ordered(merged.brick_id, other_id)
            for other_id in neighbour_ids(layout, merged.brick_id)
        )


def improve_connectivity(
    layout: Layout,
    grid: VoxelGrid,
    rng: np.random.Generator,
    *,
    fail_max: int = 30,
) -> int:
    """Split-remerge around component borders until single-component.

    Straight vertical seams (e.g. a 7-wide row split 6+1 on every layer) can
    leave stud-disconnected towers no greedy refill can bridge; randomly
    remerging the atoms across the seam can. Accepts a candidate only when
    the component count strictly drops (Luo's Algorithm 5). Returns the
    final component count.
    """
    components = ConnectionGraph.from_layout(layout).component_count()
    failures = 0
    while components > 1 and failures < fail_max:
        seeds = component_border(layout)
        if not seeds:
            break
        region = k_ring(layout, seeds, failures // _RING_GROWTH + 1)
        candidate = layout.copy()
        split_to_atoms(candidate, region, grid)
        maximal_random_merge(candidate, rng)
        candidate_components = ConnectionGraph.from_layout(candidate).component_count()
        if candidate_components < components:
            layout.replace_with(candidate)
            components = candidate_components
            failures = 0
        else:
            failures += 1
    return components


def component_border(layout: Layout) -> set[int]:
    """Bricks with a face-neighbour on the other side of the ground divide."""
    graph = ConnectionGraph.from_layout(layout)
    floating = graph.floating_ids()
    border: set[int] = set()
    for brick in layout:
        in_floating = brick.brick_id in floating
        for other in neighbour_ids(layout, brick.brick_id):
            if (other in floating) != in_floating:
                border.add(brick.brick_id)
                break
    return border or set(floating)


def split_to_atoms(layout: Layout, brick_ids: set[int], grid: VoxelGrid) -> set[int]:
    """Split bricks into 1x1x1-plate atoms (colours from the grid).

    Plates re-phase freely: columns whose bricks sit at incompatible layer
    phases (greedy fills each column run bottom-up) can only be re-bonded
    through plate atoms — brick atoms would keep the old phase forever.
    :func:`compact_vertical` re-forms bricks afterwards. Non-mergeable parts
    (slopes, tiles) are left intact. Returns the new atom ids.
    """
    atom_ids: set[int] = set()
    for brick_id in list(brick_ids):
        brick = layout.bricks.get(brick_id)
        if brick is None or layout.part_of(brick).category not in _MERGEABLE:
            continue
        cells = layout.cells_of(brick)
        layout.remove(brick_id)
        for x, y, z in cells:
            code = _cell_code(grid, (x, y, z), brick.colour_code)
            atom = layout.add("plate_1x1", x, y, z, 0, code)
            atom_ids.add(atom.brick_id)
    return atom_ids


def compact_vertical(layout: Layout) -> int:
    """Merge stacks of three same-footprint plates into bricks.

    2D remerging leaves plate stacks behind (there is no 2-plate-tall part,
    so pairwise merges can never form a brick). Returns the merge count.
    """
    merged = 0
    for brick in sorted(layout, key=lambda b: (b.layer, b.x, b.y, b.brick_id)):
        if brick.brick_id not in layout.bricks:
            continue
        part = layout.part_of(brick)
        if part.category is not Category.PLATE:
            continue
        columns = frozenset((x, y) for x, y, _ in layout.cells_of(brick))
        stack = [brick]
        for dz in (1, 2):
            above = layout.brick_at((brick.x, brick.y, brick.layer + dz))
            if (
                above is None
                or above.colour_code != brick.colour_code
                or above.layer != brick.layer + dz
                or layout.part_of(above).category is not Category.PLATE
                or frozenset((x, y) for x, y, _ in layout.cells_of(above)) != columns
            ):
                break
            stack.append(above)
        if len(stack) != 3:
            continue
        xs = [x for x, _ in columns]
        ys = [y for _, y in columns]
        if (
            layout.catalog.rect_key(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, 3)
            is None
        ):
            continue
        for plate in stack:
            layout.remove(plate.brick_id)
        place_rect(
            layout,
            min(xs),
            min(ys),
            max(xs),
            max(ys),
            brick.layer,
            3,
            brick.colour_code,
        )
        merged += 1
    return merged


def _cell_code(grid: VoxelGrid, cell: Cell, fallback: int) -> int:
    x, y, z = cell
    nx, ny, nz = grid.shape
    if 0 <= x < nx and 0 <= y < ny and 0 <= z < nz:
        code = int(grid.codes[x, y, z])
        if code >= 0:
            return code
    return fallback


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)
