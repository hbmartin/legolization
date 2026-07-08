"""Solve the RBE system and score per-brick stability.

Follows StableLego's equilibrium-in-the-objective trick: equilibrium
residuals are minimized, not constrained, so *any* structure — including
floating or collapsing ones — yields a solution whose residuals localize
the failure.

Two modes:

- ``lp`` (default): the BrickGPT-style convex relaxation. Shared interface
  variables satisfy Newton's third law by construction and the bilinear
  non-coexistence constraint (a point cannot both press and pull) is
  dropped. Fast, open solvers, slightly optimistic.
- ``milp``: adds big-M complementarity (``normal·drag = 0`` per contact
  point) with boolean switches — closer to the paper, slower.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import cvxpy as cp
import numpy as np
from scipy import sparse
from scipy.optimize import linprog

from legolization.stability.constants import ALPHA, BETA, T_CAPACITY_N
from legolization.stability.model import ROWS_PER_BRICK, StabilityModel, build_model

if TYPE_CHECKING:
    from legolization.graph import ConnectionGraph
    from legolization.layout import Layout

_MILP_SOLVERS = ("HIGHS", "SCIP")


@dataclass(frozen=True, slots=True)
class SolverConfig:
    """Tunables for the stability solve.

    ``solver`` names a cvxpy backend for MILP mode; LP mode always uses
    scipy's HiGHS interface directly.
    """

    mode: Literal["lp", "milp"] = "lp"
    solver: str | None = None
    tol_force: float = 1e-6
    tol_torque: float = 1e-7
    drag_big_m: float = 10.0 * T_CAPACITY_N
    normal_big_m: float = 100.0


@dataclass(frozen=True, slots=True)
class BrickScore:
    """Stability verdict for one brick."""

    brick_id: int
    score: float
    drag_max: float
    in_equilibrium: bool


@dataclass(frozen=True, slots=True)
class StabilityResult:
    """Structure-level stability analysis result."""

    stable: bool
    scores: dict[int, BrickScore] = field(default_factory=dict)
    weakest_pair: tuple[int, int] | None = None
    min_capacity: float = T_CAPACITY_N
    status: str = "optimal"
    objective: float = 0.0

    @property
    def unstable_ids(self) -> frozenset[int]:
        """Bricks that are collapsing (score of 1)."""
        return frozenset(b for b, s in self.scores.items() if s.score >= 1.0)

    @property
    def max_score(self) -> float:
        """The worst per-brick score (0 = relaxed, 1 = collapsing)."""
        return max((s.score for s in self.scores.values()), default=0.0)


def analyze(
    layout: Layout,
    config: SolverConfig | None = None,
    graph: ConnectionGraph | None = None,
) -> StabilityResult:
    """Build and solve the RBE for a layout."""
    if not len(layout):
        return StabilityResult(stable=True)
    return solve_model(build_model(layout, graph), config or SolverConfig())


def solve_model(
    model: StabilityModel,
    config: SolverConfig | None = None,
) -> StabilityResult:
    """Solve an assembled system and score every brick."""
    config = config or SolverConfig()
    if config.mode == "lp":
        return _solve_lp(model, config)
    return _solve_milp(model, config)


def _solve_lp(model: StabilityModel, config: SolverConfig) -> StabilityResult:
    """Solve the convex relaxation directly with HiGHS via scipy.

    The refinement loops call this dozens of times, and cvxpy's per-call
    problem construction dwarfs the actual solve — so the L1 objective is
    assembled by hand: minimize ``sum(t) + alpha*sum(dmax_i) + beta*sum(d)`` subject to
    ``-t <= A F + b <= t`` and ``d_j <= dmax_i``, all variables nonnegative.
    """
    rows, force_count = model.a_matrix.shape
    dmax_bricks = [bid for bid, cols in model.bottom_drag_cols.items() if cols.size]
    dmax_index = {bid: force_count + rows + i for i, bid in enumerate(dmax_bricks)}
    var_count = force_count + rows + len(dmax_bricks)

    cost = np.zeros(var_count)
    if model.contact_points:
        cost[model.drag_cols] = BETA
    cost[force_count : force_count + rows] = 1.0
    cost[force_count + rows :] = ALPHA

    identity = sparse.identity(rows, format="csr")
    zero_dmax = sparse.csr_matrix((rows, len(dmax_bricks)))
    upper = sparse.hstack([model.a_matrix, -identity, zero_dmax], format="csr")
    lower = sparse.hstack([-model.a_matrix, -identity, zero_dmax], format="csr")
    blocks = [upper, lower]
    rhs = [-model.b_vector, model.b_vector]
    drag_rows: list[tuple[int, int]] = [
        (int(col), dmax_index[bid])
        for bid in dmax_bricks
        for col in model.bottom_drag_cols[bid]
    ]
    if drag_rows:
        data = np.tile([1.0, -1.0], len(drag_rows))
        row_idx = np.repeat(np.arange(len(drag_rows)), 2)
        col_idx = np.asarray(drag_rows).ravel()
        blocks.append(
            sparse.csr_matrix(
                (data, (row_idx, col_idx)),
                shape=(len(drag_rows), var_count),
            )
        )
        rhs.append(np.zeros(len(drag_rows)))

    a_ub = sparse.vstack(blocks, format="csr")
    b_ub = np.concatenate(rhs)
    result = None
    # HiGHS presolve occasionally errors on degenerate instances; retry
    # without it, then with the interior-point method.
    for method, options in (
        ("highs", None),
        ("highs", {"presolve": False}),
        ("highs-ipm", None),
    ):
        result = linprog(
            c=cost,
            A_ub=a_ub,
            b_ub=b_ub,
            bounds=(0, None),
            method=method,
            options=options,
        )
        if result.x is not None:
            break
    if result is None or result.x is None:
        message = result.message if result is not None else "no result"
        msg = f"stability LP failed: {message}"
        raise RuntimeError(msg)
    return _score(
        model,
        config,
        np.asarray(result.x[:force_count]),
        "optimal" if result.status == 0 else str(result.message),
        float(result.fun),
    )


def _solve_milp(model: StabilityModel, config: SolverConfig) -> StabilityResult:
    """Solve with big-M complementarity via cvxpy (exact but slower)."""
    forces = cp.Variable(model.var_count, nonneg=True)
    residual = model.a_matrix @ forces + model.b_vector

    drag_terms: list[cp.Expression] = [
        cp.max(forces[cols]) for cols in model.bottom_drag_cols.values() if cols.size
    ]
    objective = cp.norm1(residual)
    if drag_terms:
        objective = objective + ALPHA * cp.sum(cp.hstack(drag_terms))
    if model.contact_points:
        objective = objective + BETA * cp.sum(forces[model.drag_cols])

    constraints: list[cp.Constraint] = []
    if model.contact_points:
        switch = cp.Variable(len(model.contact_points), boolean=True)
        constraints.append(
            forces[model.drag_cols] <= config.drag_big_m * switch,
        )
        constraints.append(
            forces[model.normal_cols] <= config.normal_big_m * (1 - switch),
        )

    problem = cp.Problem(cp.Minimize(objective), constraints)
    status = _solve_with_fallback(problem, config)
    if forces.value is None:
        msg = f"stability solve failed with status {status!r}"
        raise RuntimeError(msg)

    return _score(model, config, np.asarray(forces.value), status, problem.value)


def _solve_with_fallback(problem: cp.Problem, config: SolverConfig) -> str:
    solvers = (config.solver,) if config.solver else _MILP_SOLVERS
    last_error: Exception | None = None
    for solver in solvers:
        try:
            problem.solve(solver=solver)
        except (cp.SolverError, ValueError) as error:
            last_error = error
        else:
            return str(problem.status)
    msg = f"no solver in {solvers} could solve the stability program"
    raise RuntimeError(msg) from last_error


def _score(
    model: StabilityModel,
    config: SolverConfig,
    force_values: np.ndarray,
    status: str,
    objective: float | None,
) -> StabilityResult:
    residual = model.a_matrix @ force_values + model.b_vector
    scores: dict[int, BrickScore] = {}
    for i, brick_id in enumerate(model.brick_ids):
        rows = residual[ROWS_PER_BRICK * i : ROWS_PER_BRICK * (i + 1)]
        force_ok = bool(np.all(np.abs(rows[:3]) <= config.tol_force))
        torque_ok = bool(np.all(np.abs(rows[3:]) <= config.tol_torque))
        cols = model.bottom_drag_cols[brick_id]
        drag_max = float(force_values[cols].max()) if cols.size else 0.0
        in_equilibrium = force_ok and torque_ok
        if not in_equilibrium or drag_max >= T_CAPACITY_N:
            score = 1.0
        else:
            score = drag_max / T_CAPACITY_N
        scores[brick_id] = BrickScore(
            brick_id=brick_id,
            score=score,
            drag_max=drag_max,
            in_equilibrium=in_equilibrium,
        )

    weakest_pair: tuple[int, int] | None = None
    min_capacity = T_CAPACITY_N
    for point in model.contact_points:
        capacity = T_CAPACITY_N - float(force_values[point.drag_col])
        if capacity < min_capacity:
            min_capacity = capacity
            weakest_pair = (point.below_id, point.above_id)

    stable = all(s.score < 1.0 for s in scores.values())
    return StabilityResult(
        stable=stable,
        scores=scores,
        weakest_pair=weakest_pair,
        min_capacity=min_capacity,
        status=status,
        objective=float(objective) if objective is not None else 0.0,
    )
