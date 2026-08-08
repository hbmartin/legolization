"""Reduced-variable model for the BrickSim-style candidate screen.

BrickSim (arXiv:2603.16853) replaces per-contact-point force variables
with a small set of affine-field coefficients per snap-fit connection.
This module ports that *parameterization* — not the paper's constants —
onto this project's exact force families: per brick-pair interface the
normal and drag magnitudes become affine fields ``p(f) = phi_f @ coeffs``
evaluated at the exact model's own contact points, and the four
knob-press families become one affine field per press direction.

The reduced model is built as a linear **restriction** of the exact LP's
variable space: an expansion matrix ``E`` maps reduced coefficients to
the exact model's per-point magnitudes, so ``A_reduced = A_exact @ E``
by construction and any reduced-feasible solution (``E @ x >= 0``)
expands to an exact-feasible force assignment with the same equilibrium
residual. Under exact equilibrium a reduced solve can therefore only
overestimate the drag the exact LP needs; the screen's soft equilibrium
term relaxes that to a strong tendency (see ``stability/screen.py``).
Either way the direction is safe for a screen whose accepted candidates
are always cold-certified by the exact solver.

Side contacts keep their per-generator variables (they are few), and
lateral (SNOT) mates are declined entirely: :func:`build_reduced_model`
returns ``None`` and callers fall back to the cold path, mirroring the
warm prefix solver's gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix

from legolization import telemetry
from legolization.graph import ConnectionGraph
from legolization.stability.constants import K_DIRECTIONS
from legolization.stability.model import StabilityModel, _PatternSource
from legolization.stability.solver import SolverConfig, build_model_from_config

if TYPE_CHECKING:
    from legolization.layout import Layout

_FIELD_WIDTH = 3
"""Affine basis width ``[1, u, v]`` for the normal and drag fields."""

_TWO_KNOBS = 2


@dataclass(frozen=True, slots=True)
class ReducedModel:
    """The reduced system plus the exact model it restricts.

    ``expansion`` is ``E`` (exact vars x reduced vars): ``E @ x`` yields
    every exact per-point force magnitude in the exact model's column
    order, so the exact model's bookkeeping (``drag_cols``,
    ``bottom_drag_cols``, ``rows_of``) applies to the expanded vector
    unchanged. ``a_matrix`` is ``exact.a_matrix @ expansion``.
    """

    exact: StabilityModel
    expansion: csr_matrix
    a_matrix: csr_matrix
    b_vector: np.ndarray
    connection_pairs: tuple[tuple[int, int], ...]
    drag_connection: np.ndarray
    """Connection ordinal per exact drag variable (parallel to
    ``exact.drag_cols`` order) — the ``S`` map for the per-connection
    capacity relaxation."""

    var_count: int

    @property
    def brick_ids(self) -> tuple[int, ...]:
        """Brick ids in exact-model (row) order."""
        return self.exact.brick_ids

    @property
    def rows_per_brick(self) -> int:
        """Residual rows per brick (mirrors the exact model)."""
        return self.exact.rows_per_brick


@dataclass(slots=True)
class _KnobRecord:
    """One vertical knob's exact columns and geometry."""

    center: tuple[float, float]
    point_cols: list[tuple[int, int, float, float]] = field(default_factory=list)
    """Per pattern point: (normal_col, drag_col, x, y)."""

    press_cols: list[int] = field(default_factory=list)
    """Four press columns in ``K_DIRECTIONS`` order."""


@dataclass(slots=True)
class _Triplets:
    """Sparse COO accumulator for the expansion matrix."""

    rows: list[int] = field(default_factory=list)
    cols: list[int] = field(default_factory=list)
    data: list[float] = field(default_factory=list)

    def add(self, row: int, col: int, weight: float) -> None:
        if weight:
            self.rows.append(row)
            self.cols.append(col)
            self.data.append(weight)


def _press_basis(centers: list[tuple[float, float]]) -> np.ndarray:
    """Per-knob-center basis rows for one press-direction field.

    Adaptive width keeps small interfaces from growing: one knob gets a
    constant field, two knobs a linear field along their axis, three or
    more the full ``[1, u, v]`` plane (collinear centers leave a dead
    zero column, harmless under the solver's ridge).
    """
    count = len(centers)
    if count == 1:
        return np.ones((1, 1))
    xs = np.asarray([c[0] for c in centers])
    ys = np.asarray([c[1] for c in centers])
    ux = xs - xs.mean()
    uy = ys - ys.mean()
    if count == _TWO_KNOBS:
        norm = math.hypot(float(ux[1] - ux[0]), float(uy[1] - uy[0])) or 1.0
        t_coords = (ux * float(ux[1] - ux[0]) + uy * float(uy[1] - uy[0])) / norm
        return np.stack([np.ones(count), t_coords], axis=1)
    return np.stack([np.ones(count), ux, uy], axis=1)


def _collect_connections(
    graph: ConnectionGraph,
    patterns: _PatternSource,
) -> tuple[dict[tuple[int, int], list[_KnobRecord]], list[tuple[int, int]], int]:
    """Group vertical knobs by brick pair in exact allocation order.

    Simulates ``_build_model_body``'s variable counter (two vars per
    pattern point, then four press vars per knob) so every record holds
    the exact model's column indices.
    """
    col = 0
    connections: dict[tuple[int, int], list[_KnobRecord]] = {}
    pair_order: list[tuple[int, int]] = []
    for knob in graph.knob_contacts:
        record = _KnobRecord(center=(knob.x, knob.y))
        for ox, oy in patterns.for_knob(knob):
            record.point_cols.append((col, col + 1, knob.x + ox, knob.y + oy))
            col += 2
        record.press_cols = [col + i for i in range(len(K_DIRECTIONS))]
        col += len(K_DIRECTIONS)
        pair = (knob.below_id, knob.above_id)
        if pair not in connections:
            connections[pair] = []
            pair_order.append(pair)
        connections[pair].append(record)
    return connections, pair_order, col


def _connection_fields(
    records: list[_KnobRecord],
    ordinal: int,
    start_col: int,
    triplets: _Triplets,
    drag_connection: dict[int, int],
) -> int:
    """Emit one connection's field columns; return the next reduced column."""
    points = [
        (nc, dc, x, y) for record in records for nc, dc, x, y in record.point_cols
    ]
    if not points:
        # Unreachable: every record comes from a knob contact and
        # _PatternSource.for_knob always yields the three- or four-point
        # pattern. Explicit rather than a ZeroDivisionError that the
        # screen would swallow into status="error".
        msg = f"connection {ordinal} has no contact points"
        raise RuntimeError(msg)
    cx = sum(x for _, _, x, _ in points) / len(points)
    cy = sum(y for _, _, _, y in points) / len(points)
    normal_block = start_col
    drag_block = start_col + _FIELD_WIDTH
    reduced_col = start_col + 2 * _FIELD_WIDTH
    for normal_col, drag_col, x, y in points:
        phi = (1.0, x - cx, y - cy)
        for j, weight in enumerate(phi):
            triplets.add(row=normal_col, col=normal_block + j, weight=weight)
            triplets.add(row=drag_col, col=drag_block + j, weight=weight)
        drag_connection[drag_col] = ordinal
    basis = _press_basis([record.center for record in records])
    width = basis.shape[1]
    for direction in range(len(K_DIRECTIONS)):
        block = reduced_col
        reduced_col += width
        for knob_index, record in enumerate(records):
            for j in range(width):
                triplets.add(
                    row=record.press_cols[direction],
                    col=block + j,
                    weight=float(basis[knob_index, j]),
                )
    return reduced_col


def _check_drag_attribution(
    exact: StabilityModel,
    pair_order: list[tuple[int, int]],
    drag_connection: dict[int, int],
) -> None:
    """Pin the walk's column attribution to the exact model's own record.

    :func:`_collect_connections` re-simulates ``_build_model_body``'s
    variable counter, so the total-count check catches allocation drift
    but not a reorder that keeps the count: same-shaped interfaces could
    swap and every field would attach to another interface's columns.
    Each :class:`~legolization.stability.model.ContactPoint` carries the
    brick pair its columns belong to, which pins the attribution per
    column. Reduced mode declines lateral mates and side contacts create
    no contact point, so every point here is a vertical knob's.
    """
    for point in exact.contact_points:
        ordinal = drag_connection.get(point.drag_col)
        if ordinal is None:
            msg = (
                f"exact drag column {point.drag_col} unclaimed by the "
                "reduced walk — builder drift"
            )
            raise RuntimeError(msg)
        if (walked := pair_order[ordinal]) != (point.below_id, point.above_id):
            msg = (
                f"reduced walk put exact drag column {point.drag_col} on "
                f"pair {walked}, exact model says "
                f"{(point.below_id, point.above_id)} — builder drift"
            )
            raise RuntimeError(msg)


def build_reduced_model(
    layout: Layout,
    config: SolverConfig,
    graph: ConnectionGraph | None = None,
    *,
    extra_masses: dict[int, float] | None = None,
) -> ReducedModel | None:
    """Build the reduced restriction of the exact model, or decline.

    Returns ``None`` (decline — callers fall back to the cold path) for
    lateral/SNOT layouts, contact-free layouts, and the not-yet-ported
    ``screen_fields="bricksim"`` basis.
    """
    with telemetry.span("stability.reduced.build", n=len(layout)):
        return _build_reduced_body(layout, config, graph, extra_masses=extra_masses)


def _build_reduced_body(
    layout: Layout,
    config: SolverConfig,
    graph: ConnectionGraph | None,
    *,
    extra_masses: dict[int, float] | None,
) -> ReducedModel | None:
    """Run the body of :func:`build_reduced_model` without its span."""
    if config.screen_fields != "restricted":
        return None
    graph = graph or ConnectionGraph.from_layout(layout)
    if any(knob.normal != (0, 0, 1) for knob in graph.knob_contacts):
        return None
    if not graph.knob_contacts and not graph.side_contacts:
        return None

    exact = build_model_from_config(layout, config, graph, extra_masses=extra_masses)
    patterns = _PatternSource(
        layout,
        paper_knob_rule=config.paper_knob_rule,
        rotate_contact_pattern=config.rotate_contact_pattern,
    )
    connections, pair_order, side_start = _collect_connections(graph, patterns)
    side_vars_per_contact = 4 if config.torque_z else 2
    walked = side_start + side_vars_per_contact * len(graph.side_contacts)
    if walked != exact.var_count:
        msg = (
            f"reduced walk allocated {walked} exact columns, "
            f"exact model has {exact.var_count} — builder drift"
        )
        raise RuntimeError(msg)

    triplets = _Triplets()
    drag_connection: dict[int, int] = {}
    reduced_col = 0
    for ordinal, pair in enumerate(pair_order):
        reduced_col = _connection_fields(
            records=connections[pair],
            ordinal=ordinal,
            start_col=reduced_col,
            triplets=triplets,
            drag_connection=drag_connection,
        )
    for side_col in range(side_start, exact.var_count):
        triplets.add(row=side_col, col=reduced_col, weight=1.0)
        reduced_col += 1
    _check_drag_attribution(exact, pair_order, drag_connection)

    expansion = cast(
        "csr_matrix",
        coo_matrix(
            (triplets.data, (triplets.rows, triplets.cols)),
            shape=(exact.var_count, reduced_col),
        ).tocsr(),
    )
    a_matrix = cast("csr_matrix", (exact.a_matrix @ expansion).tocsr())
    return ReducedModel(
        exact=exact,
        expansion=expansion,
        a_matrix=a_matrix,
        b_vector=exact.b_vector,
        connection_pairs=tuple(pair_order),
        drag_connection=np.asarray(
            [drag_connection[int(c)] for c in exact.drag_cols], dtype=np.int64
        ),
        var_count=reduced_col,
    )


__all__ = ["ReducedModel", "build_reduced_model"]
