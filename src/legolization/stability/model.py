"""Assemble the Rigid-Block-Equilibrium force/torque system from a layout.

Per brick there are five equilibrium rows (fx, fy, fz, τx, τy — no yaw
torque, following StableLego). Per mated knob there are 3 or 4 contact
points (pattern picked by the cavity side: 1xX cavities pinch studs at four
points, wider cavities at three), each carrying a shared **normal** variable
(support on the upper brick = press on the lower) and a shared **drag**
variable (drag on the upper = pull on the lower), so Newton's third law is
satisfied by construction. Each knob also carries four horizontal
**knob-press** variables, and each laterally touching brick pair one
**side-press** variable. The ground plane provides unpaired reactions to
layer-0 bricks.

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

from legolization.graph import GROUND_ID, ConnectionGraph
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
        """Return the five residual rows of a brick."""
        index = self.brick_ids.index(brick_id)
        return slice(ROWS_PER_BRICK * index, ROWS_PER_BRICK * (index + 1))


class _Assembler:
    """Accumulates sparse triplets for the equilibrium system."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.brick_ids = tuple(sorted(layout.bricks))
        self.index = {bid: i for i, bid in enumerate(self.brick_ids)}
        self.rows: list[int] = []
        self.cols: list[int] = []
        self.data: list[float] = []
        self.var_count = 0
        self.centroids = {bid: self._centroid(bid) for bid in self.brick_ids}

    def _centroid(self, brick_id: int) -> tuple[float, float, float]:
        """Filled-cell mass centroid in (stud, stud, plate) units."""
        cells = self.layout.filled_cells_of(self.layout.bricks[brick_id])
        xs = sum(c[0] for c in cells) / len(cells)
        ys = sum(c[1] for c in cells) / len(cells)
        zs = sum(c[2] for c in cells) / len(cells) + 0.5
        return (xs, ys, zs)

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
        """Add ``F = magnitude·direction`` at ``position`` to a brick's rows.

        ``position`` is in grid units (stud, stud, plate); levers are taken
        about the brick's centroid and converted to meters.
        """
        base = ROWS_PER_BRICK * self.index[brick_id]
        cx, cy, cz = self.centroids[brick_id]
        rx = (position[0] - cx) * KNOB_PITCH_M
        ry = (position[1] - cy) * KNOB_PITCH_M
        rz = (position[2] - cz) * PLATE_HEIGHT_M
        fx, fy, fz = direction
        entries = (
            (_FX, fx),
            (_FY, fy),
            (_FZ, fz),
            (_TX, ry * fz - rz * fy),
            (_TY, rz * fx - rx * fz),
        )
        for row_offset, coeff in entries:
            if coeff:
                self.rows.append(base + row_offset)
                self.cols.append(col)
                self.data.append(coeff)


def _cavity_pattern(layout: Layout, brick_id: int) -> tuple[tuple[float, float], ...]:
    """Contact-point offsets for a brick's bottom cavities (by min width)."""
    footprint = layout.part_of(layout.bricks[brick_id]).footprint
    xs = [dx for dx, _ in footprint]
    ys = [dy for _, dy in footprint]
    min_dim = min(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
    return FOUR_POINT_OFFSETS if min_dim == 1 else THREE_POINT_OFFSETS


def build_model(
    layout: Layout,
    graph: ConnectionGraph | None = None,
) -> StabilityModel:
    """Build the sparse equilibrium system for a layout."""
    graph = graph or ConnectionGraph.from_layout(layout)
    asm = _Assembler(layout)
    contact_points: list[ContactPoint] = []
    bottom_drag_cols: dict[int, list[int]] = {bid: [] for bid in asm.brick_ids}

    up: tuple[float, float, float] = (0.0, 0.0, 1.0)
    down: tuple[float, float, float] = (0.0, 0.0, -1.0)

    for knob in graph.knob_contacts:
        pattern = _cavity_pattern(layout, knob.above_id)
        z_plane = float(knob.interface_layer)
        for ox, oy in pattern:
            position = (knob.x + ox, knob.y + oy, z_plane)
            normal_col = asm.new_var()
            drag_col = asm.new_var()
            # Upper brick: support up, drag down.
            asm.add_force(knob.above_id, normal_col, up, position)
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
        col = asm.new_var()
        unit = (1.0, 0.0, 0.0) if side.axis == 0 else (0.0, 1.0, 0.0)
        away = (-unit[0] * side.direction, -unit[1] * side.direction, 0.0)
        toward = (unit[0] * side.direction, unit[1] * side.direction, 0.0)
        asm.add_force(side.a_id, col, away, side.centroid)
        asm.add_force(side.b_id, col, toward, side.centroid)

    b_vector = np.zeros(ROWS_PER_BRICK * len(asm.brick_ids))
    for brick_id in asm.brick_ids:
        mass_kg = layout.part_of(layout.bricks[brick_id]).mass_g / 1000.0
        b_vector[ROWS_PER_BRICK * asm.index[brick_id] + _FZ] = -mass_kg * GRAVITY

    a_matrix = coo_matrix(
        (asm.data, (asm.rows, asm.cols)),
        shape=(ROWS_PER_BRICK * len(asm.brick_ids), max(asm.var_count, 1)),
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
    )
