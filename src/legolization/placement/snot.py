"""Sideways (SNOT) finishing pass: clad flat vertical wall faces.

Tall, flat wall faces read as a stack of raw brick sides. This opt-in
pass finds brick-aligned 3-plate wall windows whose outward neighbour
column is strictly outside the target shape, in vertical runs of at
least ``min_run`` windows, and clads each: the wall cell column is
carved (via the shared carve-and-refill surgery) and replaced by a 1x1
side-stud bracket (87087), and a sideways 1x1 tile (3070b) hangs on the
lateral stud with its smooth face outward. Only real receiving geometry
is used — the tile is genuinely held by a stud, and the RBE prices the
lateral mate like any other contact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from legolization.grid import EMPTY, merge_colour
from legolization.placement.carve import covering_donors, refill_tiling

if TYPE_CHECKING:
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout, PlacedBrick

_BRICK_PLATES = 3

# Outward face direction → bracket yaw rotating the lateral stud
# (local +x) onto it.
_FACE_YAW = {
    (1, 0): 0,
    (0, 1): 90,
    (-1, 0): 180,
    (0, -1): 270,
}


def apply_snot(layout: Layout, grid: VoxelGrid, *, min_run: int = 2) -> int:
    """Clad qualifying wall windows with bracket+tile pairs; return count."""
    mounted = 0
    for x, y, z, face in _qualifying_sites(layout, grid, min_run=min_run):
        if _mount(layout, grid, x, y, z, face):
            mounted += 1
    return mounted


def _qualifying_sites(
    layout: Layout,
    grid: VoxelGrid,
    *,
    min_run: int,
) -> list[tuple[int, int, int, tuple[int, int]]]:
    """Wall windows in vertical runs of at least ``min_run``, sorted."""
    gx, gy, gz = grid.shape
    sites: dict[tuple[int, int, tuple[int, int]], list[int]] = {}
    for x in range(gx):
        for y in range(gy):
            for z in range(0, gz - _BRICK_PLATES + 1, _BRICK_PLATES):
                window = [(x, y, z + dz) for dz in range(_BRICK_PLATES)]
                if any(layout.brick_at(cell) is None for cell in window):
                    continue
                for face in _FACE_YAW:
                    if _slide_path_clear(layout, grid, x, y, z, face):
                        sites.setdefault((x, y, face), []).append(z)
    chosen: list[tuple[int, int, int, tuple[int, int]]] = []
    for (x, y, face), zs in sites.items():
        for run in _runs(sorted(zs), min_run=min_run):
            chosen.extend((x, y, rz, face) for rz in run)
    return sorted(chosen)


def _runs(zs: list[int], *, min_run: int) -> list[list[int]]:
    """Maximal consecutive-window runs of at least ``min_run``."""
    runs: list[list[int]] = []
    current: list[int] = []
    for z in zs:
        if current and z != current[-1] + _BRICK_PLATES:
            if len(current) >= min_run:
                runs.append(current)
            current = []
        current.append(z)
    if len(current) >= min_run:
        runs.append(current)
    return runs


def _slide_path_clear(  # noqa: PLR0913 - one site is five scalars plus the layout
    layout: Layout,
    grid: VoxelGrid,
    x: int,
    y: int,
    z: int,
    face: tuple[int, int],
) -> bool:
    """Whether a tile can slide onto this face from outside the model.

    A sideways tile approaches along its stud axis, so the straight ray
    from the wall face outward must be free all the way out of the grid
    at every window plate. This rejects enclosed cavities and open pits
    alike (v1 clad EMPTY cavity faces the builder could never reach —
    PR #17 review) and matches ``_outward_ray_blockers``' insertion
    model. Cladding parts already hung on the ray do not block it: the
    sequencer orders inner tiles before outer ones via those blockers.
    """
    gx, gy, gz = grid.shape
    for dz in range(_BRICK_PLATES):
        cz = z + dz
        step = 1
        while True:
            px, py = x + face[0] * step, y + face[1] * step
            if not (0 <= px < gx and 0 <= py < gy):
                break  # reached open space outside the grid
            if cz < gz and grid.codes[px, py, cz] != EMPTY:
                return False  # the shape itself blocks the approach
            occupant = layout.brick_at((px, py, cz))
            if occupant is not None and (layout.part_of(occupant).mount_normal is None):
                return False  # a structural brick blocks the approach
            step += 1
    return True


@dataclass(frozen=True, slots=True)
class _MountPlan:
    """Everything a validated mount needs; computed before any mutation."""

    donors: dict[int, PlacedBrick]
    bracket_colour: int
    tiling: list[tuple[str, tuple[int, int, int], int, int]]
    tile_colour: int
    yaw: int


def _mount_plan(  # noqa: PLR0913 - one site is five scalars plus the layout
    layout: Layout,
    grid: VoxelGrid,
    x: int,
    y: int,
    z: int,
    face: tuple[int, int],
) -> _MountPlan | None:
    """Validate one cladding site; None on any failed eligibility guard."""
    window = {(x, y, z + dz) for dz in range(_BRICK_PLATES)}
    # Sites are gathered before any mutation; an earlier mount may have
    # hung its tile into this face's neighbour column (inside corners
    # share it — hit on suzanne). Re-check occupancy now.
    target = {(x + face[0], y + face[1], z + dz) for dz in range(_BRICK_PLATES)}
    if any(layout.brick_at(cell) is not None for cell in target):
        return None
    if (donors := covering_donors(layout, window)) is None:
        return None
    # Only single-column donors may be carved: cutting a bracket out of a
    # wall-spanning brick would destroy the wall's horizontal bonding
    # (and cascade through its own refills — measured, not hypothetical).
    if any(
        (cx, cy) != (x, y)
        for donor in donors.values()
        for cx, cy, _ in layout.cells_of(donor)
    ):
        return None
    colours = {donor.colour_code for donor in donors.values()}
    if len(colours) != 1:
        return None
    bracket_colour = colours.pop()
    colour_of = {
        cell: donor.colour_code
        for donor in donors.values()
        for cell in layout.cells_of(donor)
    }
    remainder = set(colour_of) - window
    if (tiling := refill_tiling(layout, remainder, colour_of)) is None:
        return None
    return _MountPlan(
        donors=donors,
        bracket_colour=bracket_colour,
        tiling=tiling,
        tile_colour=_face_colour(grid, x, y, z, fallback=bracket_colour),
        yaw=_FACE_YAW[face],
    )


def _face_colour(
    grid: VoxelGrid,
    x: int,
    y: int,
    z: int,
    *,
    fallback: int,
) -> int:
    """Visible-face colour from the wall voxels, or the carved colour."""
    gx, gy, gz = grid.shape
    face_codes = [
        int(grid.codes[x, y, z + dz])
        for dz in range(_BRICK_PLATES)
        if 0 <= x < gx and 0 <= y < gy and z + dz < gz
    ]
    tile_colour = merge_colour(*face_codes) if face_codes else None
    if tile_colour is None or tile_colour < 0:
        return fallback
    return tile_colour


def _mount(  # noqa: PLR0913 - one site is five scalars plus the layout
    layout: Layout,
    grid: VoxelGrid,
    x: int,
    y: int,
    z: int,
    face: tuple[int, int],
) -> bool:
    """Carve the wall column, seat the bracket, hang the tile."""
    if (plan := _mount_plan(layout, grid, x, y, z, face)) is None:
        return False
    layout.remove_many(plan.donors)
    layout.add("brick_1x1_side_stud", x, y, z, plan.yaw, plan.bracket_colour)
    for part_key, (ax, ay, az), part_yaw, part_colour in plan.tiling:
        layout.add(part_key, ax, ay, az, part_yaw, part_colour)
    layout.add("tile_1x1_snot", x + face[0], y + face[1], z, plan.yaw, plan.tile_colour)
    return True
