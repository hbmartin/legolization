"""Colour-bounded merge/split engine (Luo-style, spatially indexed).

The engine starts from 1x1 atoms (bricks on absolute 3-plate slabs, plates
elsewhere), then repeatedly merges random mergeable neighbour pairs until no
merge is possible ("maximal" layout). Two bricks merge iff they share a
layer and height, their colours are compatible (equal, or either side is
the colour-free ``IGNORE`` interior label), and their union footprint is a
solid rectangle that exists in the catalog; the optional soft-colour mode
additionally lets mismatched colours merge by Luo's importance sampling.
Splitting a region back to atoms enables the k-ring reconfiguration loop of
the refinement strategies.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from legolization import telemetry
from legolization.catalog import Category
from legolization.graph import ConnectionGraph
from legolization.grid import EMPTY, IGNORE, merge_colour
from legolization.layout import Layout

if TYPE_CHECKING:
    import numpy as np

    from legolization.catalog import Catalog, Cell
    from legolization.grid import VoxelGrid
    from legolization.layout import PlacedBrick
    from legolization.placement.base import ObjectiveWeights
    from legolization.stability.solver import SolverConfig

ColourMode = Literal["hard", "soft"]

_MERGEABLE = (Category.BRICK, Category.PLATE)
_RING_GROWTH = 10  # failures per extra reconfiguration ring (Luo's N)
BRIDGE_DRAWS = 5  # best-of-k candidates per bridging step (layered strategies)
_FALLBACK_COLOUR = 71  # light bluish gray, for IGNORE bricks with no neighbour


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
                if code == EMPTY:
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
    *,
    require_colour: bool = True,
) -> tuple[int, int, int, int] | None:
    """Return the union rect ``(x0, y0, x1, y1)`` if a+b can merge.

    ``require_colour=False`` (soft-colour mode) skips the colour
    compatibility check; the caller then resolves the merged colour.
    """
    part_a = layout.part_of(a)
    part_b = layout.part_of(b)
    if (
        part_a.category not in _MERGEABLE
        or part_b.category not in _MERGEABLE
        or a.layer != b.layer
        or part_a.height_plates != part_b.height_plates
        or (require_colour and merge_colour(a.colour_code, b.colour_code) is None)
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
    *,
    colour_mode: ColourMode = "hard",
    colour_weight: float = 1.0,
) -> None:
    """Merge random mergeable pairs in place until the layout is maximal.

    ``colour_mode="soft"`` lets mismatched colours merge via Luo's
    importance sampling: the merged colour is drawn with probability
    inversely proportional to how many of the other brick's stud columns
    it would miscolour, and the merge is discarded with probability
    ``colour_weight / (1/e_a + 1/e_b + colour_weight)`` — large weights
    recover the hard constraint.
    """
    _random_merge_from(
        layout,
        rng,
        seed_ids=set(layout.bricks),
        colour_mode=colour_mode,
        colour_weight=colour_weight,
    )


def regional_random_merge(
    layout: Layout,
    seed_ids: set[int],
    rng: np.random.Generator,
    *,
    colour_mode: ColourMode = "hard",
    colour_weight: float = 1.0,
) -> None:
    """Randomly merge outward from ``seed_ids`` until locally maximal.

    Only pairs touching the seed set (or bricks created by its merges) are
    considered, so a repaired region re-bonds — including across its own
    boundary — without churning the rest of the layout.
    """
    _random_merge_from(
        layout,
        rng,
        seed_ids=seed_ids,
        colour_mode=colour_mode,
        colour_weight=colour_weight,
    )


def _random_merge_from(
    layout: Layout,
    rng: np.random.Generator,
    *,
    seed_ids: set[int],
    colour_mode: ColourMode,
    colour_weight: float,
) -> None:
    if not math.isfinite(colour_weight) or colour_weight < 0:
        msg = "colour_weight must be finite and non-negative"
        raise ValueError(msg)
    pairs: set[tuple[int, int]] = set()
    for brick_id in sorted(seed_ids):  # id order keeps runs reproducible
        if brick_id not in layout.bricks:
            continue
        for other_id in neighbour_ids(layout, brick_id):
            pairs.add(_ordered(brick_id, other_id))
    pending = list(pairs)
    while pending:
        index = int(rng.integers(len(pending)))
        pending[index], pending[-1] = pending[-1], pending[index]
        a_id, b_id = pending.pop()
        if a_id not in layout.bricks or b_id not in layout.bricks:
            continue
        a = layout.bricks[a_id]
        b = layout.bricks[b_id]
        require_colour = colour_mode == "hard"
        if (rect := merged_rect(layout, a, b, require_colour=require_colour)) is None:
            continue
        if (colour := merge_colour(a.colour_code, b.colour_code)) is None:
            colour = _soft_colour(layout, a, b, colour_weight, rng)
            if colour is None:
                continue  # importance sampling discarded this merge
        layer = a.layer
        height = layout.part_of(a).height_plates
        layout.remove(a_id)
        layout.remove(b_id)
        merged = place_rect(layout, *rect, layer, height, colour)
        pending.extend(
            _ordered(merged.brick_id, other_id)
            for other_id in neighbour_ids(layout, merged.brick_id)
        )


def _soft_colour(
    layout: Layout,
    a: PlacedBrick,
    b: PlacedBrick,
    colour_weight: float,
    rng: np.random.Generator,
) -> int | None:
    """Luo's soft-colour draw: pick a side's colour or discard the merge."""
    error_a = len({(x, y) for x, y, _ in layout.cells_of(b)})  # cells a miscolours
    error_b = len({(x, y) for x, y, _ in layout.cells_of(a)})
    inv_a, inv_b = 1.0 / error_a, 1.0 / error_b
    draw = float(rng.random()) * (inv_a + inv_b + colour_weight)
    if draw < inv_a:
        return a.colour_code
    if draw < inv_a + inv_b:
        return b.colour_code
    return None


def improve_connectivity(  # noqa: PLR0913 - repair knobs are all keyword-only
    layout: Layout,
    grid: VoxelGrid,
    rng: np.random.Generator,
    *,
    fail_max: int = 30,
    colour_mode: ColourMode = "hard",
    colour_weight: float = 1.0,
    bridge_draws: int = 1,
) -> int:
    """Split-remerge around component borders until single-component.

    Straight vertical seams (e.g. a 7-wide row split 6+1 on every layer) can
    leave stud-disconnected towers no greedy refill can bridge; randomly
    remerging the atoms across the seam can. Accepts a candidate only when
    the component count strictly drops (Luo's Algorithm 5). Returns the
    final component count.

    ``bridge_draws > 1`` enables best-of-k acceptance: a random-maximal
    region rewrite is the count-inflation hotspot (measured on mushroom:
    one accepted rewrite added +179 bricks to a 112-brick minimum tiling),
    so among the draws that bridge components, keep the one with the
    fewest components, then fewest bricks. The bridging guarantee is
    unchanged (any bridging draw is still accepted), and the default of 1
    is byte-identical to the historical single-draw loop — the greedy
    path stays on it because its shipped goldens pin exact bytes, and
    local best-of-k choices shift downstream refinement chaotically
    (measured: heart 12 -> 29 bricks).
    """
    components = ConnectionGraph.from_layout(layout).component_count()
    failures = 0
    while components > 1 and failures < fail_max:
        seeds = component_border(layout)
        if not seeds:
            break
        region = k_ring(layout, seeds, failures // _RING_GROWTH + 1)
        best: Layout | None = None
        best_key: tuple[int, int] | None = None
        for _ in range(bridge_draws):
            with telemetry.span("connectivity.attempt"):
                candidate = layout.copy()
                atom_ids = split_to_atoms(candidate, region, grid)
                maximal_random_merge(
                    candidate, rng, colour_mode=colour_mode, colour_weight=colour_weight
                )
                # Reclaim bricks from whatever 1x1 plates the merge left
                # behind — after the merge, so the phase pre-commit
                # can't block seam bridging.
                if compact_columns(candidate, atom_ids):
                    maximal_random_merge(
                        candidate,
                        rng,
                        colour_mode=colour_mode,
                        colour_weight=colour_weight,
                    )
                candidate_components = ConnectionGraph.from_layout(
                    candidate
                ).component_count()
            if candidate_components < components:
                key = (candidate_components, len(candidate))
                if best_key is None or key < best_key:
                    best, best_key = candidate, key
        if best is not None and best_key is not None:
            with telemetry.span("connectivity.accept"):
                layout.replace_with(best)
                components = best_key[0]
                failures = 0
                telemetry.value("connectivity.bricks", len(layout))
                telemetry.value("connectivity.components", components)
        else:
            failures += 1
    return components


def component_border(layout: Layout) -> set[int]:
    """Bricks with a face-neighbour in a different brick-graph component.

    Empty when the components never touch (disjoint voxel islands) — such
    layouts are genuinely un-bridgeable and the repair loop should stop.
    """
    labels = ConnectionGraph.from_layout(layout).brick_components()
    border: set[int] = set()
    for brick in layout:
        label = labels[brick.brick_id]
        for other in neighbour_ids(layout, brick.brick_id):
            if labels[other] != label:
                border.add(brick.brick_id)
                break
    return border


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
                or above.layer != brick.layer + dz
                or layout.part_of(above).category is not Category.PLATE
                or frozenset((x, y) for x, y, _ in layout.cells_of(above)) != columns
            ):
                break
            stack.append(above)
        if len(stack) != 3:
            continue
        colour = merge_colour(*(plate.colour_code for plate in stack))
        if colour is None:
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
            colour,
        )
        merged += 1
    return merged


def compact_columns(layout: Layout, brick_ids: set[int]) -> int:
    """Re-form 1x1 bricks from stacked 1x1 plate runs on a shared phase.

    After :func:`split_to_atoms`, a repaired region is all 1x1 plates. 2D
    remerging can only grow *plates* (there is no 2-plate-tall part), and
    :func:`compact_vertical` only merges stacks whose footprints already
    align — so repaired regions used to stay plate rafts forever (~3x the
    part count). Voting one start phase (mod 3) for the whole region turns
    the atoms into 1x1 *bricks* on a shared alignment before the 2D merge,
    which can then grow them into real bricks. Returns the brick count.
    """
    runs = _plate_runs(layout, brick_ids)
    if not runs:
        return 0

    def eligible_cells(run: list[PlacedBrick], phase: int) -> int:
        z0, z1 = run[0].layer, run[-1].layer
        start = z0 + (phase - z0) % 3
        return 3 * max(0, (z1 - start + 1) // 3) if start + 2 <= z1 else 0

    votes = {
        phase: sum(eligible_cells(run, phase) for run in runs) for phase in (0, 1, 2)
    }
    phase = min((0, 1, 2), key=lambda p: (-votes[p], p))
    merged = 0
    for run in runs:
        by_layer = {plate.layer: plate for plate in run}
        z0, z1 = run[0].layer, run[-1].layer
        start = z0 + (phase - z0) % 3
        while start + 2 <= z1:
            triple = [by_layer[start + dz] for dz in (0, 1, 2)]
            colour = merge_colour(*(plate.colour_code for plate in triple))
            if colour is not None:
                x, y = triple[0].x, triple[0].y
                for plate in triple:
                    layout.remove(plate.brick_id)
                layout.add("brick_1x1", x, y, start, 0, colour)
                merged += 1
            start += 3
    return merged


def final_remerge(
    layout: Layout,
    grid: VoxelGrid,
    rng: np.random.Generator,
    *,
    weights: ObjectiveWeights | None = None,
    solver_config: SolverConfig | None = None,
) -> bool:
    """Global post-placement re-merge; keep only a strictly smaller layout.

    Two candidates are tried: a conservative merge pass, and a plate
    re-phase — split every plate back to atoms, re-form 1x1 bricks on one
    voted phase, and remerge. The re-phase is what reclaims the plate rafts
    connectivity repairs leave behind (2D remerging can only ever grow
    *plates*; only aligned columns can become bricks again). The objective
    check guards against merges that hurt physics or aesthetics more than
    the saved parts are worth, and the component check guards topology.
    Returns True when the layout was replaced.
    """
    from legolization.placement.base import evaluate  # noqa: PLC0415 - cycle guard

    conservative = layout.copy()
    maximal_random_merge(conservative, rng)
    compact_vertical(conservative)
    candidates = [conservative]

    rephased = layout.copy()
    plate_ids = {
        brick.brick_id
        for brick in rephased
        if rephased.part_of(brick).category is Category.PLATE
    }
    if plate_ids:
        atom_ids = split_to_atoms(rephased, plate_ids, grid)
        compact_columns(rephased, atom_ids)
        maximal_random_merge(rephased, rng)
        compact_vertical(rephased)
        candidates.append(rephased)

    baseline = evaluate(layout, grid, weights, solver_config)
    base_components = ConnectionGraph.from_layout(layout).component_count()
    accepted: list[Layout] = []
    for candidate in candidates:
        if len(candidate) >= len(layout):
            continue
        report = evaluate(candidate, grid, weights, solver_config)
        components = ConnectionGraph.from_layout(candidate).component_count()
        if report.total <= baseline.total and components <= base_components:
            accepted.append(candidate)
    if not accepted:
        return False
    layout.replace_with(min(accepted, key=len))
    return True


def _plate_runs(layout: Layout, brick_ids: set[int]) -> list[list[PlacedBrick]]:
    """Contiguous vertical runs of 1x1 plates among ``brick_ids``."""
    plates: dict[tuple[int, int], dict[int, PlacedBrick]] = {}
    for brick_id in brick_ids:
        brick = layout.bricks.get(brick_id)
        if brick is not None and brick.part_key == "plate_1x1":
            plates.setdefault((brick.x, brick.y), {})[brick.layer] = brick
    runs: list[list[PlacedBrick]] = []
    for _, by_layer in sorted(plates.items()):
        run: list[PlacedBrick] = []
        for z in sorted(by_layer):
            if run and z != run[-1].layer + 1:
                runs.append(run)
                run = []
            run.append(by_layer[z])
        if run:
            runs.append(run)
    return runs


def resolve_ignore_colours(layout: Layout) -> int:
    """Recolour IGNORE bricks from their nearest coloured neighbour (BFS).

    Interior bricks are invisible, but LDraw files still need a concrete
    colour code. Multi-source BFS from every coloured brick assigns each
    IGNORE brick the colour of its nearest coloured neighbour; isolated
    ones fall back to light bluish gray. Returns the recolour count.
    """
    resolved: dict[int, int] = {}
    queue: deque[int] = deque()
    pending: set[int] = set()
    for brick in sorted(layout, key=lambda b: b.brick_id):
        if brick.colour_code == IGNORE:
            pending.add(brick.brick_id)
        else:
            resolved[brick.brick_id] = brick.colour_code
            queue.append(brick.brick_id)
    if not pending:
        return 0
    while queue and pending:
        current = queue.popleft()
        for other in sorted(neighbour_ids(layout, current)):
            if other in pending:
                pending.discard(other)
                resolved[other] = resolved[current]
                queue.append(other)
    recoloured = 0
    for brick in list(layout):
        if brick.colour_code != IGNORE:
            continue
        colour = resolved.get(brick.brick_id, _FALLBACK_COLOUR)
        layout.bricks[brick.brick_id] = replace(brick, colour_code=colour)
        recoloured += 1
    return recoloured


def _cell_code(grid: VoxelGrid, cell: Cell, fallback: int) -> int:
    x, y, z = cell
    nx, ny, nz = grid.shape
    if 0 <= x < nx and 0 <= y < ny and 0 <= z < nz:
        code = int(grid.codes[x, y, z])
        if code != EMPTY:  # IGNORE is a real (colour-free) fill
            return code
    return fallback


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)
