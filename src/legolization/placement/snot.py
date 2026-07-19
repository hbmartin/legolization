"""Sideways (SNOT) finishing pass: clad flat vertical wall faces.

Tall, flat wall faces read as a stack of raw brick sides. This opt-in
pass finds brick-aligned 3-plate wall windows whose outward approach is
clear to the edge of the model, in vertical runs of at least
``min_run`` windows, and clads them: wall columns are carved (via the
shared carve-and-refill surgery) and replaced by a side-stud carrier,
and a sideways tile hangs on the lateral studs with its smooth face
outward. Only real receiving geometry is used — the tile is genuinely
held by studs, and the RBE prices the lateral mates like any other
contact.

v2 works in two-column sites where adjacent windows qualify (an 11211
two-stud carrier plus a sideways 1x2 tile), falling back to single
columns (87087 + 1x1 tile); courses alternate their pairing phase like
a running bond so stacked cladding crosses the seams below. Donors may
span wall columns — carving bonded walls is allowed because every mount
is validated on a copy first: a mount is accepted only if the layout's
stud-graph component count and floating count do not increase (the
re-bond guard). The pipeline keeps its whole-pass stability snapshot as
the outer rail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from legolization.catalog import Category, rotate_offset
from legolization.graph import ConnectionGraph
from legolization.grid import EMPTY, merge_colour
from legolization.placement.carve import covering_donors, refill_tiling

if TYPE_CHECKING:
    from legolization.catalog import Catalog, Part
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout, PlacedBrick

_BRICK_PLATES = 3
_FACES = ((1, 0), (0, 1), (-1, 0), (0, -1))


def apply_snot(layout: Layout, grid: VoxelGrid, *, min_run: int = 2) -> int:
    """Clad qualifying wall windows; return the number of mounts."""
    mounted = 0
    graph = ConnectionGraph.from_layout(layout)
    baseline = (graph.component_count(), len(graph.floating_ids()))
    for site in _mount_sites(layout, grid, min_run=min_run):
        if (result := _mount(layout, grid, site, baseline)) is not None:
            mounted += 1
            baseline = result
    return mounted


@dataclass(frozen=True, slots=True)
class _Site:
    """One mount attempt: 1 or 2 wall columns (in along-the-wall order)."""

    columns: tuple[tuple[int, int], ...]
    z: int
    face: tuple[int, int]


def _mount_sites(layout: Layout, grid: VoxelGrid, *, min_run: int) -> list[_Site]:
    """Pair qualifying windows along each wall line, staggered per course.

    Windows on one wall line (same face, course, and position along the
    face normal) are paired along the wall; odd courses skip their first
    window before pairing so the cladding courses cross seams like a
    running bond. Leftover windows become single-column sites.
    """
    lines: dict[tuple[tuple[int, int], int, int], list[int]] = {}
    for x, y, z, face in _qualifying_windows(layout, grid, min_run=min_run):
        span = x * face[0] + y * face[1]
        along = -x * face[1] + y * face[0]
        lines.setdefault((face, z, span), []).append(along)
    sites: list[_Site] = []
    for (face, z, span), alongs in sorted(
        lines.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2])
    ):
        for run in _consecutive_runs(sorted(alongs)):
            index = (z // _BRICK_PLATES) % 2  # running-bond stagger
            if index:
                sites.append(
                    _Site(columns=(_column(face, span, run[0]),), z=z, face=face)
                )
            while index < len(run):
                paired = index + 1 < len(run) and run[index + 1] == run[index] + 1
                count = 2 if paired else 1
                sites.append(
                    _Site(
                        columns=tuple(
                            _column(face, span, run[index + i]) for i in range(count)
                        ),
                        z=z,
                        face=face,
                    )
                )
                index += count
    return sites


def _column(face: tuple[int, int], span: int, along: int) -> tuple[int, int]:
    """Recover the wall column from its (span, along) wall-line frame."""
    return (
        span * face[0] - along * face[1],
        span * face[1] + along * face[0],
    )


def _consecutive_runs(values: list[int]) -> list[list[int]]:
    """Split sorted ints into maximal consecutive runs."""
    runs: list[list[int]] = []
    for value in values:
        if runs and value == runs[-1][-1] + 1:
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


def _qualifying_windows(
    layout: Layout,
    grid: VoxelGrid,
    *,
    min_run: int,
) -> list[tuple[int, int, int, tuple[int, int]]]:
    """Wall windows in vertical runs of at least ``min_run``, sorted."""
    gx, gy, gz = grid.shape
    windows: dict[tuple[int, int, tuple[int, int]], list[int]] = {}
    for x in range(gx):
        for y in range(gy):
            for z in range(0, gz - _BRICK_PLATES + 1, _BRICK_PLATES):
                window = [(x, y, z + dz) for dz in range(_BRICK_PLATES)]
                if any(layout.brick_at(cell) is None for cell in window):
                    continue
                for face in _FACES:
                    if _slide_path_clear(layout, grid, x, y, z, face):
                        windows.setdefault((x, y, face), []).append(z)
    chosen: list[tuple[int, int, int, tuple[int, int]]] = []
    for (x, y, face), zs in windows.items():
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


def _carrier_for(catalog: Catalog, columns: int) -> Part | None:
    """Find the side-stud carrier covering ``columns`` wall columns."""
    for part in catalog.by_category(Category.SNOT):
        if part.mount_normal is not None or len(part.footprint) != columns:
            continue
        laterals = [c for c in part.top_connectors if c.direction[2] == 0]
        if len(laterals) == columns:
            return part
    return None


def _cladding_for(catalog: Catalog, columns: int) -> Part | None:
    """Find the sideways facade part covering ``columns`` wall columns."""
    for part in catalog.by_category(Category.SNOT):
        if part.mount_normal is not None and len(part.footprint) == columns:
            return part
    return None


def _carrier_yaw(part: Part, face: tuple[int, int]) -> int | None:
    """Yaw rotating the carrier's lateral studs onto the outward face.

    Multi-stud carriers keep their body along the wall automatically:
    the local long axis is the stud direction rotated 90° in the part
    frame, so mapping studs to the face maps the body to the wall line.
    """
    stud = next((c for c in part.top_connectors if c.direction[2] == 0), None)
    if stud is None:
        return None
    target = (face[0], face[1], 0)
    for yaw in part.orientations:
        if rotate_offset(stud.direction, yaw) == target:
            return yaw
    return None


def _cladding_yaw(part: Part, face: tuple[int, int]) -> int | None:
    """Yaw rotating the cladding's sockets back onto the wall face."""
    if (normal := part.mount_normal) is None:
        return None
    target = (-face[0], -face[1], normal[2])
    for yaw in part.orientations:
        if rotate_offset(normal, yaw) == target:
            return yaw
    return None


@dataclass(frozen=True, slots=True)
class _MountPlan:
    """Everything a validated mount needs; computed before any mutation."""

    donors: dict[int, PlacedBrick]
    carrier_key: str
    carrier_yaw: int
    anchor: tuple[int, int]
    bracket_colour: int
    tiling: list[tuple[str, tuple[int, int, int], int, int]]
    tile_key: str
    tile_yaw: int
    tile_anchor: tuple[int, int]
    tile_colour: int


def _site_parts(
    catalog: Catalog,
    site: _Site,
) -> tuple[Part, int, Part, int] | None:
    """Resolve the carrier/cladding pair and their yaws for a site."""
    carrier = _carrier_for(catalog, len(site.columns))
    cladding = _cladding_for(catalog, len(site.columns))
    if carrier is None or cladding is None:
        return None
    carrier_yaw = _carrier_yaw(carrier, site.face)
    tile_yaw = _cladding_yaw(cladding, site.face)
    if carrier_yaw is None or tile_yaw is None:
        return None
    return (carrier, carrier_yaw, cladding, tile_yaw)


def _mount_plan(layout: Layout, grid: VoxelGrid, site: _Site) -> _MountPlan | None:
    """Validate one cladding site; None on any failed eligibility guard."""
    if (parts := _site_parts(layout.catalog, site)) is None:
        return None
    carrier, carrier_yaw, cladding, tile_yaw = parts
    window = {
        (cx, cy, site.z + dz) for cx, cy in site.columns for dz in range(_BRICK_PLATES)
    }
    # Sites are gathered before any mutation; an earlier mount may have
    # hung its tile into this face's neighbour columns (inside corners
    # share them — hit on suzanne). Re-check occupancy now.
    target = {
        (cx + site.face[0], cy + site.face[1], site.z + dz)
        for cx, cy in site.columns
        for dz in range(_BRICK_PLATES)
    }
    if any(layout.brick_at(cell) is not None for cell in target):
        return None
    if (donors := covering_donors(layout, window)) is None:
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
    anchor = site.columns[0]
    return _MountPlan(
        donors=donors,
        carrier_key=carrier.key,
        carrier_yaw=carrier_yaw,
        anchor=anchor,
        bracket_colour=bracket_colour,
        tiling=tiling,
        tile_key=cladding.key,
        tile_yaw=tile_yaw,
        tile_anchor=(anchor[0] + site.face[0], anchor[1] + site.face[1]),
        tile_colour=_face_colour(grid, window, fallback=bracket_colour),
    )


def _face_colour(
    grid: VoxelGrid,
    window: set[tuple[int, int, int]],
    *,
    fallback: int,
) -> int:
    """Visible-face colour from the wall voxels, or the carved colour."""
    gx, gy, gz = grid.shape
    face_codes = [
        int(grid.codes[x, y, z])
        for x, y, z in sorted(window)
        if 0 <= x < gx and 0 <= y < gy and z < gz
    ]
    tile_colour = merge_colour(*face_codes) if face_codes else None
    if tile_colour is None or tile_colour < 0:
        return fallback
    return tile_colour


def _mount(
    layout: Layout,
    grid: VoxelGrid,
    site: _Site,
    baseline: tuple[int, int],
) -> tuple[int, int] | None:
    """Carve, refill, and clad one site — accepted only via the re-bond guard.

    The whole surgery runs on a copy; the mount lands only if the trial
    layout's component count and floating count do not exceed
    ``baseline`` (carving a bonded wall must not cost connectivity).
    Returns the accepted layout's (components, floating), or None.
    """
    if (plan := _mount_plan(layout, grid, site)) is None:
        return None
    trial = layout.copy()
    trial.remove_many(plan.donors)
    trial.add(
        plan.carrier_key,
        plan.anchor[0],
        plan.anchor[1],
        site.z,
        plan.carrier_yaw,
        plan.bracket_colour,
    )
    for part_key, (ax, ay, az), part_yaw, part_colour in plan.tiling:
        trial.add(part_key, ax, ay, az, part_yaw, part_colour)
    trial.add(
        plan.tile_key,
        plan.tile_anchor[0],
        plan.tile_anchor[1],
        site.z,
        plan.tile_yaw,
        plan.tile_colour,
    )
    graph = ConnectionGraph.from_layout(trial)
    after = (graph.component_count(), len(graph.floating_ids()))
    if after[0] > baseline[0] or after[1] > baseline[1]:
        return None
    layout.replace_with(trial)
    return after
