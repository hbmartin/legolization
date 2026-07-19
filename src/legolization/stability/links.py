"""Artificial-link instability localization (Kollsker & Malaguti's QP).

Add one *free* vertical shear force per laterally touching brick pair —
forces no real LEGO connection could transmit — and require exact
equilibrium with every real force bounded (drags by the friction capacity
T). Minimizing the sum of squared link forces then answers two questions at
once: ``q = 0`` means the structure stands on real forces alone, and a
positive ``q`` spreads over exactly the links that patch the deficit,
pinpointing where material must be rearranged (Whiting-style masonry
localization, adapted to the RBE force model).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import cvxpy as cp
import numpy as np
from scipy.sparse import coo_matrix

from legolization import telemetry
from legolization.graph import ConnectionGraph
from legolization.stability.constants import (
    KNOB_PITCH_M,
    T_CAPACITY_N,
)
from legolization.stability.model import ROWS_PER_BRICK, StabilityModel, build_model

if TYPE_CHECKING:
    from legolization.layout import Layout

_FZ, _TX, _TY = 2, 3, 4
_QP_SOLVERS = ("OSQP", "CLARABEL")
_Q_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class LinkForce:
    """One artificial link's absorbed shear, in newtons."""

    a_id: int
    b_id: int
    magnitude: float


@dataclass(frozen=True, slots=True)
class LinkReport:
    """Localization outcome: ``q == 0`` means stable on real forces."""

    q: float
    links: tuple[LinkForce, ...]
    status: str

    @property
    def stable(self) -> bool:
        """Whether equilibrium holds without artificial help."""
        return self.status == "optimal" and self.q <= _Q_TOLERANCE


def localize_instability(
    layout: Layout,
    graph: ConnectionGraph | None = None,
) -> LinkReport:
    """Solve the artificial-link QP; infeasible = unpatchable collapse."""
    if not len(layout):
        return LinkReport(q=0.0, links=(), status="optimal")
    with telemetry.span("stability.links", n=len(layout)):
        return _localize_body(layout, graph)


def _localize_body(
    layout: Layout,
    graph: ConnectionGraph | None,
) -> LinkReport:
    """Run the body of :func:`localize_instability` without its telemetry span."""
    graph = graph or ConnectionGraph.from_layout(layout)
    model = build_model(layout, graph)
    if not graph.side_contacts:
        return _no_link_verdict(model)

    centroids = _centroids(layout)
    index = {brick_id: i for i, brick_id in enumerate(model.brick_ids)}
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    for link_col, side in enumerate(graph.side_contacts):
        for brick_id, sign in ((side.a_id, 1.0), (side.b_id, -1.0)):
            base = ROWS_PER_BRICK * index[brick_id]
            cx, cy, _ = centroids[brick_id]
            rx = (side.centroid[0] - cx) * KNOB_PITCH_M
            ry = (side.centroid[1] - cy) * KNOB_PITCH_M
            for row, coeff in ((_FZ, 1.0), (_TX, ry), (_TY, -rx)):
                rows.append(base + row)
                cols.append(link_col)
                data.append(sign * coeff)
    a_link = coo_matrix(
        (data, (rows, cols)),
        shape=(model.a_matrix.shape[0], len(graph.side_contacts)),
    ).tocsr()

    forces = cp.Variable(model.var_count, nonneg=True)
    link_forces = cp.Variable(len(graph.side_contacts))
    constraints: list[cp.Constraint] = [
        model.a_matrix @ forces + a_link @ link_forces + model.b_vector == 0,
    ]
    if model.contact_points:
        constraints.append(forces[model.drag_cols] <= T_CAPACITY_N)
    problem = cp.Problem(cp.Minimize(cp.sum_squares(link_forces)), constraints)
    status = _solve(problem)
    if link_forces.value is None:
        return LinkReport(q=float("inf"), links=(), status=status)
    values = np.asarray(link_forces.value)
    links = tuple(
        sorted(
            (
                LinkForce(
                    a_id=side.a_id,
                    b_id=side.b_id,
                    magnitude=float(abs(value)),
                )
                for side, value in zip(graph.side_contacts, values, strict=True)
            ),
            key=lambda link: -link.magnitude,
        )
    )
    return LinkReport(q=float(problem.value), links=links, status=status)


def _no_link_verdict(model: StabilityModel) -> LinkReport:
    """Check plain feasibility: without lateral neighbours nothing patches."""
    forces = cp.Variable(model.var_count, nonneg=True)
    constraints: list[cp.Constraint] = [
        model.a_matrix @ forces + model.b_vector == 0,
    ]
    if model.contact_points:
        constraints.append(forces[model.drag_cols] <= T_CAPACITY_N)
    problem = cp.Problem(cp.Minimize(cp.sum_squares(forces) * 0.0), constraints)
    status = _solve(problem)
    if forces.value is None:
        return LinkReport(q=float("inf"), links=(), status=status)
    return LinkReport(q=0.0, links=(), status=status)


def _centroids(layout: Layout) -> dict[int, tuple[float, float, float]]:
    centroids: dict[int, tuple[float, float, float]] = {}
    for brick in layout:
        cells = layout.filled_cells_of(brick)
        centroids[brick.brick_id] = (
            sum(c[0] for c in cells) / len(cells),
            sum(c[1] for c in cells) / len(cells),
            sum(c[2] for c in cells) / len(cells) + 0.5,
        )
    return centroids


def _solve(problem: cp.Problem) -> str:
    with telemetry.span("stability.links.cvxpy"):
        return _solve_body(problem)


def _solve_body(problem: cp.Problem) -> str:
    last_status = "error"
    for solver in _QP_SOLVERS:
        try:
            problem.solve(solver=solver)
        except (cp.SolverError, ValueError):
            continue
        last_status = str(problem.status)
        if problem.status in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
            return "optimal"
        if problem.status in (cp.INFEASIBLE, cp.INFEASIBLE_INACCURATE):
            return "infeasible"
    return last_status
