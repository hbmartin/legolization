"""BrickSim's own friction-pyramid fields — the research basis.

``screen_fields="restricted"`` (the production default in
``stability/reduced.py``) restricts THIS project's force families;
this module instead ports the paper's model faithfully
(arXiv:2603.16853 §VI-A): per connection nine coefficients — an axial
field ``p_n = phi @ alpha`` plus two in-plane fields ``p_u``/``p_v`` —
with per-point forces along the connection frame, tension-only axial
components, and the linearized friction pyramid

    |F_t| + F_a <= mu * (F_r + F_0)

under the paper's constants ``mu = 0.2`` and ``mu * F_0 = 0.7 N``
(sourced to Legolization) — deliberately NOT ``T_CAPACITY_N``. The
resulting utilization ``u_k = max_f (|F_t| + F_a) / (mu (F_r + F_0))``
is a different scale from the exact solver's ``max_score``.

Research flag only: never a production default, never a certifier.
The study deliverable is an accuracy/speed comparison against the
restricted basis (``scripts/benchmark_screen.py --fields bricksim``).

Simplifications, documented rather than hidden: ground connections are
treated like any snap-fit (the paper's baseplate studs interlock, so
``ground_pull`` does not apply), and side contacts reuse the exact
model's per-generator compression columns unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix, hstack

from legolization import telemetry
from legolization.graph import ConnectionGraph
from legolization.stability.constants import FOUR_POINT_OFFSETS
from legolization.stability.model import (
    _PatternSource,
    brick_centroid,
    force_entries,
    rows_per_brick,
)
from legolization.stability.reduced import _PLATES_PER_STUD
from legolization.stability.solver import SolverConfig, build_model_from_config

if TYPE_CHECKING:
    from legolization.layout import Layout

MU = 0.2
"""Static friction coefficient for ABS (the paper's value)."""

MU_F0 = 0.7
"""Preload friction budget ``mu * F_0`` in newtons (the paper's value)."""

_COEFFS = 12
"""Per connection: alpha, beta, gamma (the paper's nine) plus a
compression field in R^3 — the paper's co-located frictionless contact
family C, folded onto the same points so gravity has a compression
path (tension-only alpha alone cannot support a stacked brick)."""


@dataclass(frozen=True, slots=True)
class BricksimModel:
    """The paper-basis system for one layout.

    ``a_matrix`` rows follow the exact model's per-brick residual
    layout; ``axial``/``radial``/``tangential`` map coefficients to the
    per-point scalars entering the tension and pyramid constraints.
    """

    brick_ids: tuple[int, ...]
    a_matrix: csr_matrix
    b_vector: np.ndarray
    axial: csr_matrix
    radial: csr_matrix
    tangential: csr_matrix
    compression: csr_matrix
    point_connection: np.ndarray
    point_above: np.ndarray
    connection_pairs: tuple[tuple[int, int], ...]
    side_start: int
    var_count: int
    rows_per_brick: int


@dataclass(slots=True)
class _Frames:
    """One connection's frame and collected points."""

    normal: tuple[float, float, float]
    u_axis: tuple[float, float, float]
    v_axis: tuple[float, float, float]
    points: list[tuple[tuple[float, float, float], float, float]]
    """(world position, u, v) per contact point, u/v in stud units."""


def _connection_frame(normal: tuple[int, int, int]) -> _Frames:
    nx, ny, nz = normal
    if nz:
        return _Frames(
            normal=(0.0, 0.0, 1.0),
            u_axis=(1.0, 0.0, 0.0),
            v_axis=(0.0, 1.0, 0.0),
            points=[],
        )
    return _Frames(
        normal=(float(nx), float(ny), 0.0),
        u_axis=(float(-ny), float(nx), 0.0),
        v_axis=(0.0, 0.0, 1.0),
        points=[],
    )


def build_bricksim_model(
    layout: Layout,
    config: SolverConfig,
    graph: ConnectionGraph | None = None,
) -> BricksimModel | None:
    """Build the paper-basis model, or decline contact-free layouts."""
    with telemetry.span("stability.bricksim.build", n=len(layout)):
        return _build_body(layout, config, graph)


def _collect_frames(
    layout: Layout,
    config: SolverConfig,
    graph: ConnectionGraph,
) -> tuple[dict[tuple[int, int, tuple[int, int, int]], _Frames], list]:
    patterns = _PatternSource(
        layout,
        paper_knob_rule=config.paper_knob_rule,
        rotate_contact_pattern=config.rotate_contact_pattern,
    )
    frames: dict[tuple[int, int, tuple[int, int, int]], _Frames] = {}
    order: list = []
    for knob in graph.knob_contacts:
        key = (knob.below_id, knob.above_id, knob.normal)
        if key not in frames:
            frames[key] = _connection_frame(knob.normal)
            order.append(key)
        frame = frames[key]
        if knob.normal == (0, 0, 1):
            z_plane = float(knob.interface_layer)
            for ox, oy in patterns.for_knob(knob):
                position = (knob.x + ox, knob.y + oy, z_plane)
                frame.points.append((position, knob.x + ox, knob.y + oy))
        else:
            nx, ny, _ = knob.normal
            tx, ty = float(-ny), float(nx)
            x_pos = knob.x + 0.5 * nx
            y_pos = knob.y + 0.5 * ny
            z_center = knob.interface_layer + 0.5
            t_center = knob.x * tx + knob.y * ty
            v_center = z_center / _PLATES_PER_STUD
            for ox, oy in FOUR_POINT_OFFSETS:
                position = (
                    x_pos + ox * tx,
                    y_pos + ox * ty,
                    z_center + oy * _PLATES_PER_STUD,
                )
                frame.points.append((position, t_center + ox, v_center + oy))
    return frames, order


@dataclass(slots=True)
class _Collector:
    """Mutable assembly state for one build pass."""

    index: dict[int, int]
    rpb: int
    centroids: dict[int, tuple[float, float, float]]
    torque_z: bool
    rows: list[int] = field(default_factory=list)
    cols: list[int] = field(default_factory=list)
    data: list[float] = field(default_factory=list)
    axial_triplets: list[tuple[int, int, float]] = field(default_factory=list)
    compression_triplets: list[tuple[int, int, float]] = field(default_factory=list)
    plane_triplets: dict[str, list[tuple[int, int, float]]] = field(
        default_factory=lambda: {"u": [], "v": []}
    )
    point_connection: list[int] = field(default_factory=list)
    point_above: list[int] = field(default_factory=list)
    point_radial: list[tuple[float, float]] = field(default_factory=list)
    point_index: int = 0

    def add_force(
        self,
        brick_id: int,
        col: int,
        weight: float,
        direction: tuple[float, float, float],
        position: tuple[float, float, float],
    ) -> None:
        if brick_id not in self.index:
            return
        base = self.rpb * self.index[brick_id]
        for row_offset, coeff in force_entries(
            self.centroids[brick_id], position, direction, torque_z=self.torque_z
        ):
            self.rows.append(base + row_offset)
            self.cols.append(col)
            self.data.append(weight * coeff)

    def emit_connection(
        self, ordinal: int, key: tuple[int, int, tuple[int, int, int]], frame: _Frames
    ) -> None:
        """Emit one connection's four field families over its points."""
        below, above, _normal = key
        block = ordinal * _COEFFS
        cu = sum(u for _, u, _ in frame.points) / len(frame.points)
        cv = sum(v for _, _, v in frame.points) / len(frame.points)
        negative = (-frame.normal[0], -frame.normal[1], -frame.normal[2])
        for position, u, v in frame.points:
            phi = (1.0, u - cu, v - cv)
            for j, weight in enumerate(phi):
                if not weight:
                    continue
                # alpha: tension along the stud axis; beta/gamma:
                # in-plane; the fourth family is the co-located
                # compression contact pushing along -n.
                for offset, direction in (
                    (0, frame.normal),
                    (3, frame.u_axis),
                    (6, frame.v_axis),
                    (9, negative),
                ):
                    self.add_force(
                        above, block + offset + j, weight, direction, position
                    )
                    self.add_force(
                        below, block + offset + j, -weight, direction, position
                    )
                self.axial_triplets.append((self.point_index, block + j, weight))
                self.plane_triplets["u"].append(
                    (self.point_index, block + 3 + j, weight)
                )
                self.plane_triplets["v"].append(
                    (self.point_index, block + 6 + j, weight)
                )
                self.compression_triplets.append(
                    (self.point_index, block + 9 + j, weight)
                )
            du, dv = u - cu, v - cv
            norm = float(np.hypot(du, dv))
            self.point_radial.append((-du / norm, -dv / norm) if norm else (1.0, 0.0))
            self.point_connection.append(ordinal)
            self.point_above.append(above)
            self.point_index += 1


def _build_body(
    layout: Layout,
    config: SolverConfig,
    graph: ConnectionGraph | None,
) -> BricksimModel | None:
    graph = graph or ConnectionGraph.from_layout(layout)
    if not graph.knob_contacts and not graph.side_contacts:
        return None
    exact = build_model_from_config(layout, config, graph)
    frames, order = _collect_frames(layout, config, graph)

    brick_ids = exact.brick_ids
    collector = _Collector(
        index={bid: i for i, bid in enumerate(brick_ids)},
        rpb=rows_per_brick(torque_z=config.torque_z),
        centroids={bid: brick_centroid(layout, bid) for bid in brick_ids},
        torque_z=config.torque_z,
    )
    for ordinal, key in enumerate(order):
        collector.emit_connection(ordinal, key, frames[key])

    side_start = len(order) * _COEFFS
    side_count = (4 if config.torque_z else 2) * len(graph.side_contacts)
    var_count = side_start + side_count

    a_fields = coo_matrix(
        (collector.data, (collector.rows, collector.cols)),
        shape=(collector.rpb * len(brick_ids), side_start),
    ).tocsr()
    if side_count:
        side_block = exact.a_matrix[:, exact.var_count - side_count :]
        a_matrix = cast("csr_matrix", hstack([a_fields, side_block]).tocsr())
    else:
        a_matrix = cast("csr_matrix", a_fields)

    axial, compression, radial, tangential = _point_maps(
        axial=collector.axial_triplets,
        compression=collector.compression_triplets,
        planes=collector.plane_triplets,
        radial_dirs=collector.point_radial,
        point_count=collector.point_index,
        var_count=var_count,
    )

    return BricksimModel(
        brick_ids=brick_ids,
        a_matrix=a_matrix,
        b_vector=exact.b_vector,
        axial=axial,
        radial=radial,
        tangential=tangential,
        compression=compression,
        point_connection=np.asarray(collector.point_connection, dtype=np.int64),
        point_above=np.asarray(collector.point_above, dtype=np.int64),
        connection_pairs=tuple((below, above) for below, above, _ in order),
        side_start=side_start,
        var_count=var_count,
        rows_per_brick=collector.rpb,
    )


def _point_maps(  # noqa: PLR0913 - four families over one point census
    *,
    axial: list[tuple[int, int, float]],
    compression: list[tuple[int, int, float]],
    planes: dict[str, list[tuple[int, int, float]]],
    radial_dirs: list[tuple[float, float]],
    point_count: int,
    var_count: int,
) -> tuple[csr_matrix, csr_matrix, csr_matrix, csr_matrix]:
    """Assemble the per-point scalar maps from collected triplets."""

    def scalar_map(triplets: list[tuple[int, int, float]]) -> csr_matrix:
        return cast(
            "csr_matrix",
            coo_matrix(
                (
                    [w for _, _, w in triplets],
                    ([r for r, _, _ in triplets], [c for _, c, _ in triplets]),
                ),
                shape=(point_count, var_count),
            ).tocsr(),
        )

    def diagonal(values: np.ndarray) -> coo_matrix:
        return coo_matrix(
            (values, (range(point_count), range(point_count))),
            shape=(point_count, point_count),
        )

    plane_u = scalar_map(planes["u"])
    plane_v = scalar_map(planes["v"])
    dirs = np.asarray(radial_dirs).reshape(point_count, 2)
    radial = cast(
        "csr_matrix",
        (diagonal(dirs[:, 0]) @ plane_u + diagonal(dirs[:, 1]) @ plane_v).tocsr(),
    )
    tangential = cast(
        "csr_matrix",
        (diagonal(-dirs[:, 1]) @ plane_u + diagonal(dirs[:, 0]) @ plane_v).tocsr(),
    )
    return scalar_map(axial), scalar_map(compression), radial, tangential


__all__ = ["MU", "MU_F0", "BricksimModel", "build_bricksim_model"]
