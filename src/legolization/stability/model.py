"""Assemble the Rigid-Block-Equilibrium force/torque system from a layout.

Per brick there are five equilibrium rows (fx, fy, fz, τx, τy — no yaw
torque, following StableLego; an optional sixth yaw-torque row τz sits
behind ``torque_z``). Per mated knob there are 3 or 4 contact
points (pattern picked by the cavity side: 1xX cavities pinch studs at four
points, wider cavities at three), each carrying a shared **normal** variable
(support on the upper brick = press on the lower) and a shared **drag**
variable (drag on the upper = pull on the lower), so Newton's third law is
satisfied by construction. Each knob also carries four horizontal
**knob-press** variables, and each laterally touching brick pair two
**side-press** variables per axis — one at each vertical extreme of the
shared faces, so lateral load transfer can carry torque. The ground plane
provides unpaired reactions to layer-0 bricks.

The system is returned as one sparse matrix ``A`` and constant ``b`` such
that ``A @ F + b`` stacks every brick's residual ``(Cf, Cτ)``; gravity sits
in ``b`` (torque-free because levers are taken about each brick's filled-cell
centroid).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix

from legolization import telemetry
from legolization.graph import GROUND_ID, ConnectionGraph, KnobContact, SideContact
from legolization.stability.constants import (
    FOUR_POINT_OFFSETS,
    GRAVITY,
    K_DIRECTIONS,
    KNOB_PITCH_M,
    PLATE_HEIGHT_M,
    THREE_POINT_OFFSETS,
)

if TYPE_CHECKING:
    from legolization.layout import Layout

ROWS_PER_BRICK = 5
_FX, _FY, _FZ, _TX, _TY = range(ROWS_PER_BRICK)
_TZ = 5


def rows_per_brick(*, torque_z: bool) -> int:
    """Residual rows per brick: 5 following StableLego, 6 with yaw torque."""
    return 6 if torque_z else ROWS_PER_BRICK


@dataclass(frozen=True, slots=True)
class ContactPoint:
    """Bookkeeping for one interface contact point's shared variables."""

    normal_col: int
    drag_col: int
    below_id: int
    above_id: int


@dataclass(frozen=True, slots=True)
class StabilityModel:
    """The assembled linear force system for one layout."""

    brick_ids: tuple[int, ...]
    a_matrix: csr_matrix
    b_vector: np.ndarray
    contact_points: tuple[ContactPoint, ...]
    bottom_drag_cols: dict[int, np.ndarray]
    var_count: int
    rows_per_brick: int = ROWS_PER_BRICK

    @property
    def brick_count(self) -> int:
        """Number of bricks in the system."""
        return len(self.brick_ids)

    @property
    def drag_cols(self) -> np.ndarray:
        """Columns of every drag variable."""
        return np.asarray([p.drag_col for p in self.contact_points], dtype=np.int64)

    @property
    def normal_cols(self) -> np.ndarray:
        """Columns of every normal variable."""
        return np.asarray([p.normal_col for p in self.contact_points], dtype=np.int64)

    def rows_of(self, brick_id: int) -> slice:
        """Return the residual rows of a brick."""
        index = self.brick_ids.index(brick_id)
        return slice(self.rows_per_brick * index, self.rows_per_brick * (index + 1))


def brick_centroid(layout: Layout, brick_id: int) -> tuple[float, float, float]:
    """Filled-cell mass centroid in (stud, stud, plate) units."""
    cells = layout.filled_cells_of(layout.bricks[brick_id])
    xs = sum(c[0] for c in cells) / len(cells)
    ys = sum(c[1] for c in cells) / len(cells)
    zs = sum(c[2] for c in cells) / len(cells) + 0.5
    return (xs, ys, zs)


def force_entries(
    centroid: tuple[float, float, float],
    position: tuple[float, float, float],
    direction: tuple[float, float, float],
    *,
    torque_z: bool = False,
) -> tuple[tuple[int, float], ...]:
    """Nonzero (row-offset, coefficient) pairs for one applied force.

    ``position`` is in grid units (stud, stud, plate); levers are taken
    about ``centroid`` and converted to meters. Shared by the batch
    assembler and the incremental prefix solver so both engines compute
    identical float expressions. With ``torque_z`` the yaw-torque
    coefficient joins as a sixth row (vertical forces never load it:
    their yaw lever is identically zero).
    """
    cx, cy, cz = centroid
    rx = (position[0] - cx) * KNOB_PITCH_M
    ry = (position[1] - cy) * KNOB_PITCH_M
    rz = (position[2] - cz) * PLATE_HEIGHT_M
    fx, fy, fz = direction
    entries = [
        (_FX, fx),
        (_FY, fy),
        (_FZ, fz),
        (_TX, ry * fz - rz * fy),
        (_TY, rz * fx - rx * fz),
    ]
    if torque_z:
        entries.append((_TZ, rx * fy - ry * fx))
    return tuple((row_offset, coeff) for row_offset, coeff in entries if coeff)


class _Assembler:
    """Accumulates sparse triplets for the equilibrium system."""

    def __init__(self, layout: Layout, *, torque_z: bool = False) -> None:
        self.layout = layout
        self.torque_z = torque_z
        self.rows_per_brick = rows_per_brick(torque_z=torque_z)
        self.brick_ids = tuple(sorted(layout.bricks))
        self.index = {bid: i for i, bid in enumerate(self.brick_ids)}
        self.rows: list[int] = []
        self.cols: list[int] = []
        self.data: list[float] = []
        self.var_count = 0
        self.centroids = {bid: brick_centroid(layout, bid) for bid in self.brick_ids}

    def new_var(self) -> int:
        """Allocate one nonnegative force-magnitude variable."""
        col = self.var_count
        self.var_count += 1
        return col

    def add_force(
        self,
        brick_id: int,
        col: int,
        direction: tuple[float, float, float],
        position: tuple[float, float, float],
    ) -> None:
        """Add ``F = magnitude·direction`` at ``position`` to a brick's rows."""
        base = self.rows_per_brick * self.index[brick_id]
        for row_offset, coeff in force_entries(
            self.centroids[brick_id], position, direction, torque_z=self.torque_z
        ):
            self.rows.append(base + row_offset)
            self.cols.append(col)
            self.data.append(coeff)


def _add_lateral_knob(
    asm: _Assembler,
    knob: KnobContact,
    contact_points: list[ContactPoint],
    bottom_drag_cols: dict[int, list[int]],
) -> None:
    """Contact forces for a sideways (SNOT) stud mate.

    The mating plane is vertical: the FOUR_POINT diamond is laid out in
    that plane (transverse axis in studs, vertical axis converted to
    plate units), the normal/drag pair acts along the stud axis
    (pull-off capacity rides the same T-bounded drag machinery as
    vertical mates), and the four in-plane presses carry the mounted
    part's weight in stud shear.
    """
    nx, ny, _ = knob.normal
    tx, ty = float(-ny), float(nx)  # transverse in-plane axis
    plates_per_stud = KNOB_PITCH_M / PLATE_HEIGHT_M
    x_pos = knob.x + 0.5 * nx
    y_pos = knob.y + 0.5 * ny
    z_center = knob.interface_layer + 0.5
    outward = (float(nx), float(ny), 0.0)
    inward = (float(-nx), float(-ny), 0.0)
    for ox, oy in FOUR_POINT_OFFSETS:
        position = (
            x_pos + ox * tx,
            y_pos + ox * ty,
            z_center + oy * plates_per_stud,
        )
        normal_col = asm.new_var()
        drag_col = asm.new_var()
        # Mounted part: pressed off the wall, dragged back onto the stud.
        asm.add_force(knob.above_id, normal_col, outward, position)
        asm.add_force(knob.above_id, drag_col, inward, position)
        if knob.below_id != GROUND_ID:
            asm.add_force(knob.below_id, normal_col, inward, position)
            asm.add_force(knob.below_id, drag_col, outward, position)
        contact_points.append(
            ContactPoint(
                normal_col=normal_col,
                drag_col=drag_col,
                below_id=knob.below_id,
                above_id=knob.above_id,
            )
        )
        bottom_drag_cols[knob.above_id].append(drag_col)
    stud_center = (float(x_pos), float(y_pos), float(z_center))
    shear_directions = (
        (tx, ty, 0.0),
        (-tx, -ty, 0.0),
        (0.0, 0.0, 1.0),
        (0.0, 0.0, -1.0),
    )
    for direction in shear_directions:
        col = asm.new_var()
        asm.add_force(knob.above_id, col, direction, stud_center)
        if knob.below_id != GROUND_ID:
            asm.add_force(
                knob.below_id,
                col,
                (-direction[0], -direction[1], -direction[2]),
                stud_center,
            )


def cavity_pattern(layout: Layout, brick_id: int) -> tuple[tuple[float, float], ...]:
    """Contact-point offsets for a brick's bottom cavities (by min width).

    The StableLego *release* rule: 1-wide cavities pinch at four points,
    everything wider at three — uniform per brick.
    """
    footprint = layout.part_of(layout.bricks[brick_id]).footprint
    xs = [dx for dx, _ in footprint]
    ys = [dy for _, dy in footprint]
    min_dim = min(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
    return FOUR_POINT_OFFSETS if min_dim == 1 else THREE_POINT_OFFSETS


def footprint_columns(
    layout: Layout,
    brick_id: int,
) -> tuple[frozenset[tuple[int, int]], int]:
    """Return a brick's world footprint columns and min footprint dimension."""
    columns = frozenset((x, y) for x, y, _ in layout.cells_of(layout.bricks[brick_id]))
    xs = [x for x, _ in columns]
    ys = [y for _, y in columns]
    min_dim = min(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
    return columns, min_dim


def knob_pattern(
    columns: frozenset[tuple[int, int]],
    min_dim: int,
    column: tuple[int, int],
) -> tuple[tuple[float, float], ...]:
    """Per-knob contact offsets under the StableLego *paper* rule.

    1xX cavities pinch at four points; 2xX at three; on QxX bodies with
    Q ≥ 3 the edge connections take three points and the interior ones
    four. Inert for the shipped catalog (no part has min dimension ≥ 3)
    but exact for any future wide part.
    """
    if min_dim == 1:
        return FOUR_POINT_OFFSETS
    if min_dim == 2:
        return THREE_POINT_OFFSETS
    x, y = column
    interior = all(
        (x + dx, y + dy) in columns for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
    )
    return FOUR_POINT_OFFSETS if interior else THREE_POINT_OFFSETS


def build_model(  # noqa: PLR0913 - physics switches are keyword-only plumbing
    layout: Layout,
    graph: ConnectionGraph | None = None,
    *,
    torque_z: bool = False,
    paper_knob_rule: bool = False,
    rotate_contact_pattern: bool = False,
    ground_pull: bool = True,
    extra_masses: dict[int, float] | None = None,
) -> StabilityModel:
    """Build the sparse equilibrium system for a layout.

    ``ground_pull=False`` models bricks resting loose on a table: the
    baseplate can push but never pull, so top-heavy structures may tip.
    ``extra_masses`` adds per-brick external load in kilograms at the
    brick's centroid (torque-free, like gravity) — the hook for
    insertion-press and payload analyses.
    """
    with telemetry.span("stability.build_model", n=len(layout)):
        return _build_model_body(
            layout,
            graph,
            torque_z=torque_z,
            paper_knob_rule=paper_knob_rule,
            rotate_contact_pattern=rotate_contact_pattern,
            ground_pull=ground_pull,
            extra_masses=extra_masses,
        )


def _add_side_contact(asm: _Assembler, side: SideContact) -> None:
    """Press generators for one laterally touching brick pair.

    Presses act at the shared faces' extreme points. Without the yaw
    torque row a horizontal force's torque coefficient is linear in z
    alone, so the two vertical extremes span every achievable
    force/torque combination (Luo corner-point-equivalent for the
    modeled axes). With yaw torque the coefficient is also linear in
    the transverse coordinate, and a nonnegative distribution over the
    face rectangle reaches exactly the cone of its four (transverse,
    vertical) corners — so the four corner generators span the six-row
    system the same way.
    """
    unit = (1.0, 0.0, 0.0) if side.axis == 0 else (0.0, 1.0, 0.0)
    away = (-unit[0] * side.direction, -unit[1] * side.direction, 0.0)
    toward = (unit[0] * side.direction, unit[1] * side.direction, 0.0)
    cx, cy, _ = side.centroid
    for z_edge in (float(side.z_lo), float(side.z_hi + 1)):
        if asm.torque_z:
            spots = [side.t_lo, side.t_hi]
        else:
            spots = [cy if side.axis == 0 else cx]
        for t_coord in spots:
            col = asm.new_var()
            position = (
                (cx, t_coord, z_edge) if side.axis == 0 else (t_coord, cy, z_edge)
            )
            asm.add_force(side.a_id, col, away, position)
            asm.add_force(side.b_id, col, toward, position)


def rotate_pattern(
    pattern: tuple[tuple[float, float], ...],
    yaw: int,
) -> tuple[tuple[float, float], ...]:
    """Rotate contact offsets by a brick's yaw (multiples of 90 degrees).

    The FOUR_POINT diamond is rotation-invariant as a set; only the
    asymmetric THREE_POINT triangle (apex fixed at -x in the release)
    actually moves.
    """
    match yaw % 360:
        case 0:
            return pattern
        case 90:
            return tuple((-oy, ox) for ox, oy in pattern)
        case 180:
            return tuple((-ox, -oy) for ox, oy in pattern)
        case 270:
            return tuple((oy, -ox) for ox, oy in pattern)
        case _:
            # Mirror catalog.rotate_offset: a non-orthogonal yaw is a
            # caller bug, not a 270-degree rotation (PR #20 review).
            msg = f"yaw must be a multiple of 90, got {yaw}"
            raise ValueError(msg)


class _PatternSource:
    """Per-knob contact-pattern lookup under either knob rule."""

    def __init__(
        self,
        layout: Layout,
        *,
        paper_knob_rule: bool,
        rotate_contact_pattern: bool = False,
    ) -> None:
        self._layout = layout
        self._paper = paper_knob_rule
        self._rotate = rotate_contact_pattern
        self._footprints: dict[int, tuple[frozenset[tuple[int, int]], int]] = {}

    def for_knob(self, knob: KnobContact) -> tuple[tuple[float, float], ...]:
        """Contact offsets for the cavity gripping this knob."""
        if not self._paper:
            pattern = cavity_pattern(self._layout, knob.above_id)
        else:
            if knob.above_id not in self._footprints:
                self._footprints[knob.above_id] = footprint_columns(
                    self._layout, knob.above_id
                )
            columns, min_dim = self._footprints[knob.above_id]
            pattern = knob_pattern(columns, min_dim, (knob.x, knob.y))
        if self._rotate:
            pattern = rotate_pattern(pattern, self._layout.bricks[knob.above_id].yaw)
        return pattern


def _build_model_body(  # noqa: PLR0913 - mirrors build_model's switches
    layout: Layout,
    graph: ConnectionGraph | None,
    *,
    torque_z: bool = False,
    paper_knob_rule: bool = False,
    rotate_contact_pattern: bool = False,
    ground_pull: bool = True,
    extra_masses: dict[int, float] | None = None,
) -> StabilityModel:
    """Run the body of :func:`build_model` without its telemetry span."""
    graph = graph or ConnectionGraph.from_layout(layout)
    asm = _Assembler(layout, torque_z=torque_z)
    contact_points: list[ContactPoint] = []
    bottom_drag_cols: dict[int, list[int]] = {bid: [] for bid in asm.brick_ids}

    up: tuple[float, float, float] = (0.0, 0.0, 1.0)
    down: tuple[float, float, float] = (0.0, 0.0, -1.0)

    patterns = _PatternSource(
        layout,
        paper_knob_rule=paper_knob_rule,
        rotate_contact_pattern=rotate_contact_pattern,
    )
    for knob in graph.knob_contacts:
        if knob.normal != (0, 0, 1):
            _add_lateral_knob(asm, knob, contact_points, bottom_drag_cols)
            continue
        pattern = patterns.for_knob(knob)
        z_plane = float(knob.interface_layer)
        for ox, oy in pattern:
            position = (knob.x + ox, knob.y + oy, z_plane)
            normal_col = asm.new_var()
            drag_col = asm.new_var()
            # Upper brick: support up, drag down. A pull-free ground
            # (loose-on-a-table mode) keeps the drag variable but gives
            # it no force entries — same indices, zero at any optimum.
            asm.add_force(knob.above_id, normal_col, up, position)
            if ground_pull or knob.below_id != GROUND_ID:
                asm.add_force(knob.above_id, drag_col, down, position)
            if knob.below_id != GROUND_ID:
                # Lower brick reactions: press down, pull up.
                asm.add_force(knob.below_id, normal_col, down, position)
                asm.add_force(knob.below_id, drag_col, up, position)
            contact_points.append(
                ContactPoint(
                    normal_col=normal_col,
                    drag_col=drag_col,
                    below_id=knob.below_id,
                    above_id=knob.above_id,
                )
            )
            bottom_drag_cols[knob.above_id].append(drag_col)
        knob_center = (float(knob.x), float(knob.y), z_plane)
        for ux, uy in K_DIRECTIONS:
            col = asm.new_var()
            asm.add_force(knob.above_id, col, (ux, uy, 0.0), knob_center)
            if knob.below_id != GROUND_ID:
                asm.add_force(knob.below_id, col, (-ux, -uy, 0.0), knob_center)

    for side in graph.side_contacts:
        _add_side_contact(asm, side)

    rpb = asm.rows_per_brick
    extra = extra_masses or {}
    b_vector = np.zeros(rpb * len(asm.brick_ids))
    for brick_id in asm.brick_ids:
        mass_kg = layout.part_of(layout.bricks[brick_id]).mass_g / 1000.0
        mass_kg += extra.get(brick_id, 0.0)
        b_vector[rpb * asm.index[brick_id] + _FZ] = -mass_kg * GRAVITY

    a_matrix = coo_matrix(
        (asm.data, (asm.rows, asm.cols)),
        shape=(rpb * len(asm.brick_ids), max(asm.var_count, 1)),
    ).tocsr()
    return StabilityModel(
        brick_ids=asm.brick_ids,
        a_matrix=a_matrix,
        b_vector=b_vector,
        contact_points=tuple(contact_points),
        bottom_drag_cols={
            bid: np.asarray(cols, dtype=np.int64)
            for bid, cols in bottom_drag_cols.items()
        },
        var_count=max(asm.var_count, 1),
        rows_per_brick=rpb,
    )
