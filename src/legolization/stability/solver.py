"""Solve the RBE system and score per-brick stability.

Follows StableLego's equilibrium-in-the-objective trick: equilibrium
residuals are minimized, not constrained, so *any* structure — including
floating or collapsing ones — yields a solution whose residuals localize
the failure.

Two modes:

- ``lp`` (default): the convex program with the bilinear non-coexistence
  constraint (a point cannot both press and pull) dropped. The relaxation
  is provably **exact**, not optimistic: each contact point's normal and
  drag columns are exact negatives of each other, so any solution with
  both positive can subtract the common minimum from both, leaving every
  equilibrium residual unchanged while strictly reducing the ``BETA``
  term — every LP optimum therefore already satisfies non-coexistence.
- ``milp``: enforces non-coexistence explicitly with big-M complementarity
  (``normal·drag = 0`` per contact point) and boolean switches. Redundant
  given the exactness above; kept as a debug cross-check of the LP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import cvxpy as cp
import highspy
import numpy as np
from scipy import sparse
from scipy.optimize import linprog

from legolization import telemetry
from legolization.stability.constants import ALPHA, BETA, T_CAPACITY_N
from legolization.stability.model import ROWS_PER_BRICK, StabilityModel, build_model

if TYPE_CHECKING:
    from legolization.graph import ConnectionGraph
    from legolization.layout import Layout

_MILP_SOLVERS = ("HIGHS",)

# HiGHS presolve occasionally errors on degenerate instances; retry
# without it, then with the interior-point method.
_LP_ATTEMPTS: tuple[tuple[str, dict[str, bool] | None], ...] = (
    ("highs", None),
    ("highs", {"presolve": False}),
    ("highs-ipm", None),
)


@dataclass(frozen=True, slots=True)
class SolverConfig:
    """Tunables for the stability solve.

    ``solver`` names a cvxpy backend for MILP mode (e.g. ``"SCIP"`` if
    pyscipopt is installed); LP mode always uses scipy's HiGHS interface
    directly.

    ``drag_big_m``/``normal_big_m`` are artificial force ceilings for the
    MILP's big-M complementarity switches only — they have no counterpart
    in the papers and never constrain the (exact) LP mode.

    ``engine`` selects the sequencer/verify prefix-solve engine only:
    ``"highspy"`` uses the warm-started incremental
    :class:`legolization.stability.prefix.PrefixSolver`; ``"scipy"`` keeps
    every solve on this module's cold path. ``engine_cross_check`` makes
    every warm probe also cold-solve and return the cold result while
    recording drift (debug/CI). ``boundary_margin`` is the relative band
    around the stability threshold within which warm verdicts are
    discarded in favour of a cold solve.
    """

    mode: Literal["lp", "milp"] = "lp"
    solver: str | None = None
    tol_force: float = 1e-6
    tol_torque: float = 1e-7
    drag_big_m: float = 10.0 * T_CAPACITY_N
    normal_big_m: float = 100.0
    engine: Literal["scipy", "highspy"] = "highspy"
    engine_cross_check: bool = False
    boundary_margin: float = 0.02

    # Appended fields only (positional compatibility).
    rescue_direct_min_bricks: int = 200
    """Rescue components at or above this size cold-solve through
    highspy directly (drops the scipy wrapper overhead); smaller ones
    keep the scipy-exact path that the 1e-6 equivalence tests pin."""

    rescue_warm: bool = False
    """Experimental: warm-start the disassembly rescue's shrinking walk
    by BOUND DEACTIVATION on one persistent HiGHS model (fix removed
    bricks' columns to zero, relax their rows) instead of per-state cold
    solves. Basis dimensions never change, so dual simplex hot-starts.
    Off until the kill criteria in docs/performance-testing.md pass."""


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
    with telemetry.span("stability.analyze", n=len(layout)):
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
    with telemetry.span("stability.lp", n=model.brick_count):
        return _solve_lp_body(model, config)


def _lp_arrays(
    model: StabilityModel,
) -> tuple[np.ndarray, sparse.csr_matrix, np.ndarray, int]:
    """Assemble the L1 system's ``(cost, A_ub, b_ub, force_count)``.

    Shared by the scipy path and the direct-highspy rescue path so both
    engines solve the byte-identical polytope.
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
    return cost, a_ub, b_ub, force_count


def _solve_lp_body(model: StabilityModel, config: SolverConfig) -> StabilityResult:
    """Run the body of :func:`_solve_lp` without its telemetry span."""
    cost, a_ub, b_ub, force_count = _lp_arrays(model)
    result = None
    with telemetry.span("stability.lp.linprog", n=model.brick_count):
        for method, options in _LP_ATTEMPTS:
            result = linprog(
                c=cost,
                A_ub=a_ub,
                b_ub=b_ub,
                bounds=(0, None),
                method=method,
                options=options,
            )
            if result.success and result.x is not None:
                break
    if result is None or not result.success or result.x is None:
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


def _solve_lp_highspy(
    model: StabilityModel,
    config: SolverConfig,
) -> tuple[StabilityResult, bool]:
    """One-shot direct-HiGHS solve of the same arrays the scipy path uses.

    Kills scipy's per-call wrapper overhead on large cold solves. The
    attempt chain mirrors ``_LP_ATTEMPTS`` (default presolve → presolve
    off → IPM). Returns ``(result, near_boundary)`` — callers re-solve
    through the scipy-exact path when the verdict sits near the
    stability threshold. Raises ``RuntimeError`` when no attempt reaches
    optimality.
    """
    cost, a_ub, b_ub, force_count = _lp_arrays(model)
    rows, cols = a_ub.shape
    csc = a_ub.tocsc()
    lp = highspy.HighsLp()
    lp.num_col_ = cols
    lp.num_row_ = rows
    lp.col_cost_ = cost
    lp.col_lower_ = np.zeros(cols)
    lp.col_upper_ = np.full(cols, np.inf)
    lp.row_lower_ = np.full(rows, -np.inf)
    lp.row_upper_ = b_ub
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = csc.indptr
    lp.a_matrix_.index_ = csc.indices
    lp.a_matrix_.value_ = csc.data
    solver = highspy.Highs()
    solver.setOptionValue("output_flag", False)  # noqa: FBT003 - pybind API
    solver.setOptionValue("threads", 1)
    attempts = (("choose", "simplex"), ("off", "simplex"), ("choose", "ipm"))
    optimal = False
    for presolve, method in attempts:
        solver.setOptionValue("presolve", presolve)
        solver.setOptionValue("solver", method)
        solver.passModel(lp)
        solver.run()
        if solver.getModelStatus() == highspy.HighsModelStatus.kOptimal:
            optimal = True
            break
    if not optimal:
        msg = f"direct HiGHS solve failed: {solver.getModelStatus()}"
        raise RuntimeError(msg)
    values = np.asarray(solver.getSolution().col_value)
    result = _score(
        model,
        config,
        values[:force_count],
        "optimal",
        float(solver.getInfo().objective_function_value),
    )
    return result, _near_boundary(model, config, values, force_count)


def _near_boundary(
    model: StabilityModel,
    config: SolverConfig,
    values: np.ndarray,
    force_count: int,
) -> bool:
    """Whether any brick's verdict sits inside the cold-re-solve band.

    Same thresholds as ``PrefixSolver._extract``: drag within
    ``(1 ± boundary_margin)·T``, or any t-residual within a decade of
    its tolerance.
    """
    margin = config.boundary_margin
    low = (1.0 - margin) * T_CAPACITY_N
    high = (1.0 + margin) * T_CAPACITY_N
    tolerances = (config.tol_force,) * 3 + (config.tol_torque,) * 2
    for i, brick_id in enumerate(model.brick_ids):
        drag_cols = model.bottom_drag_cols[brick_id]
        drag_max = float(values[drag_cols].max()) if drag_cols.size else 0.0
        if low <= drag_max <= high:
            return True
        t_vals = values[
            force_count + ROWS_PER_BRICK * i : force_count + ROWS_PER_BRICK * (i + 1)
        ]
        if any(
            tol / 10.0 < v < tol * 10.0
            for v, tol in zip(t_vals, tolerances, strict=True)
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class MaximinResult:
    """Luo's maximin friction capacity ``C_M`` for one structure."""

    feasible: bool
    capacity: float = 0.0


def solve_maximin(model: StabilityModel) -> MaximinResult:
    """Maximize the worst contact's friction margin under exact equilibrium.

    Luo's eq. 6-8: maximize ``m`` subject to ``A F + b = 0``, ``F >= 0``,
    and ``drag_j + m <= T`` for every drag variable. Unlike the RBE score
    this yields a single strict ordering over layouts: positive capacity
    means stable with margin, negative means unstable but comparable, and
    an infeasible equilibrium means collapse. It carries no per-brick
    localization — pair it with :func:`analyze` for failure seeds.
    """
    with telemetry.span("stability.maximin", n=model.brick_count):
        return _solve_maximin_body(model)


def _solve_maximin_body(model: StabilityModel) -> MaximinResult:
    """Run the body of :func:`solve_maximin` without its telemetry span."""
    rows, force_count = model.a_matrix.shape
    var_count = force_count + 1
    cost = np.zeros(var_count)
    cost[-1] = -1.0  # maximize the margin
    a_eq = sparse.hstack(
        [model.a_matrix, sparse.csr_matrix((rows, 1))],
        format="csr",
    )
    a_ub = None
    b_ub = None
    if model.contact_points:
        drag_cols = model.drag_cols
        data = np.ones(2 * drag_cols.size)
        row_idx = np.repeat(np.arange(drag_cols.size), 2)
        col_idx = np.stack(
            [drag_cols, np.full(drag_cols.size, force_count, dtype=np.int64)],
            axis=1,
        ).ravel()
        a_ub = sparse.csr_matrix(
            (data, (row_idx, col_idx)),
            shape=(drag_cols.size, var_count),
        )
        b_ub = np.full(drag_cols.size, T_CAPACITY_N)
    bounds = [(0.0, None)] * force_count + [(None, T_CAPACITY_N)]
    result = None
    for method, options in _LP_ATTEMPTS:
        result = linprog(
            c=cost,
            A_ub=a_ub,
            b_ub=b_ub,
            A_eq=a_eq,
            b_eq=-model.b_vector,
            bounds=bounds,
            method=method,
            options=options,
        )
        if result.success and result.x is not None:
            return MaximinResult(feasible=True, capacity=float(result.x[-1]))
        if result.status == 2:  # provably no equilibrium: collapsing
            return MaximinResult(feasible=False)
    message = result.message if result is not None else "no result"
    msg = f"maximin LP failed: {message}"
    raise RuntimeError(msg)


def _solve_milp(model: StabilityModel, config: SolverConfig) -> StabilityResult:
    """Solve with big-M complementarity via cvxpy (exact but slower)."""
    with telemetry.span("stability.milp", n=model.brick_count):
        return _solve_milp_body(model, config)


def _solve_milp_body(model: StabilityModel, config: SolverConfig) -> StabilityResult:
    """Run the body of :func:`_solve_milp` without its telemetry span."""
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
    last_status: str | None = None
    for solver in solvers:
        try:
            problem.solve(solver=solver)
        except (cp.SolverError, ValueError) as error:
            last_error = error
        else:
            last_error = None
            last_status = str(problem.status)
            if problem.status in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
                return last_status
    msg = f"no solver in {solvers} found an optimal stability solution"
    if last_status is not None:
        msg = f"{msg}; last status: {last_status}"
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
