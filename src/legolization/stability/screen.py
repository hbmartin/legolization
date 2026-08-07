"""Reduced-QP candidate screen (BrickSim-derived, OSQP).

Screens a layout by solving the reduced model from
:mod:`legolization.stability.reduced` with one convex sparse QP whose
structure mirrors the exact certifier LP (equilibrium residuals in the
objective, per-brick worst-drag variables, ``drag <= dmax`` rows):

    minimize  w_r ||r||^2 + alpha ||dmax||^2 + ridge ||x||^2
    subject to  A x - r = -b,   E x >= 0,   drag_j <= dmax_i,   dmax >= 0

BrickSim (arXiv:2603.16853 §VI-B) solves a three-stage lexicographic
relaxation instead. That scheme was ported first and measured: its
stage-2 exact pinning of every equilibrium row (``A x = y*``) needs
tens of thousands of ADMM iterations on force-propagation chains, while
this single weighted QP — the certifier's own objective shape, squared —
solves the same fixtures in hundreds. Two further measured conditioning
requirements are baked in: torque rows are scaled to force units
(``1/KNOB_PITCH_M`` — unscaled, whole overhang torques sit below the
solver tolerance and go unbalanced), and equilibrium residuals enter
through explicit variables rather than the Gram matrix ``A^T A`` (whose
chain modes are numerically singular).

Scoring mirrors :func:`legolization.stability.solver._score`: a brick
out of equilibrium (residual beyond a weight-relative threshold) or at
capacity (``dmax >= T``) scores 1.0, everything else ``dmax / T`` — so
``ScreenReport.q`` lives on the exact ``max_score`` scale. The reduced
polytope is a restriction of the exact LP's, so with equilibrium held
exactly the screen could only overestimate drag needs; the soft
equilibrium term means that in practice conservatism is a strong
tendency rather than a theorem (measured: q typically 1-2x the exact
score, with occasional small undershoots from residual leakage).
Either direction is safe — accepted candidates are always
cold-certified.

Every result is advisory: :class:`ScreenReport` is deliberately a
different type from :class:`~legolization.stability.solver.StabilityResult`
and never enters a verdict-bearing artifact. Callers treat anything but
a confident "ok" as a cold-solve fallthrough.

OSQP notes: solved directly (no cvxpy — per-candidate canonicalization
would dwarf the solve, the same reason the exact LP is hand-assembled);
``polishing`` is off because OSQP 1.x prints polish diagnostics to
stdout even with ``verbose=False``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import numpy as np
import osqp
from scipy import sparse

from legolization import telemetry
from legolization.stability.constants import KNOB_PITCH_M, T_CAPACITY_N
from legolization.stability.reduced import ReducedModel, build_reduced_model

if TYPE_CHECKING:
    from legolization.graph import ConnectionGraph
    from legolization.layout import Layout
    from legolization.stability.solver import SolverConfig

ScreenStatus = Literal["ok", "declined", "nonconverged", "deadline", "error"]

_W_RESIDUAL = 1_000.0
"""Equilibrium-residual weight. Large enough that residual leakage
stays below the equilibrium thresholds (measured: 100 lets a 26-brick
tower trade ~1e-3 N of residual for drag and mis-flags bricks; 1000
localizes identically to the exact solver)."""

_ALPHA_Q = 1e-3
"""Per-brick worst-drag weight (the exact LP's ``ALPHA`` role)."""

_RIDGE = 1e-6
"""Coefficient regularizer: uniqueness for dead press-basis columns
and normal/drag co-inflation (the exact LP's ``BETA`` role)."""

_V_TOL = 1e-3
"""Relative overload below which a drag above capacity is treated as
solver noise rather than a genuine violation."""

_EQ_WEIGHT_SHARE = 0.05
"""A brick is out of equilibrium when the residual leaves more than
this share of its own weight unsupported."""

_EQ_NOISE_FLOOR = 20.0
"""Absolute per-row noise floor in multiples of ``screen_eps``."""


@dataclass(frozen=True, slots=True)
class ScreenReport:
    """Advisory reduced-QP verdict for one layout.

    ``q`` lives on the exact solver's ``max_score`` scale (0 = relaxed,
    >= 1 = collapsing) but is computed from the *restricted* polytope,
    so it tends to overestimate the exact score (not strictly — see
    the module docstring). ``confident`` is False whenever any brick
    sits inside the ``screen_margin`` band around capacity — callers
    must fall through to the cold solve then.
    """

    status: ScreenStatus
    stable: bool = False
    q: float = 1.0
    scores: dict[int, float] | None = None
    overloaded: frozenset[tuple[int, int]] = frozenset()
    unstable_ids: frozenset[int] = frozenset()
    confident: bool = False

    q_raw: float = 1.0
    """Unclamped worst ``dmax / T``. ``q`` saturates at 1.0 exactly like
    the exact score, which cannot rank candidates whose baselines are
    already collapsing (ALNS repair's normal regime) — ``q_raw`` keeps
    the ordering information past the clamp."""


@dataclass(slots=True)
class _ScreenArrays:
    """Precomputed sparse blocks for the screen QP.

    ``a_scaled``/``b_scaled`` carry the equilibrium system with every
    torque row divided by ``KNOB_PITCH_M`` (torque as equivalent force
    at a one-stud lever). Without that scaling a first-order solver
    "converges" with entire overhang torques unbalanced — their metric
    coefficients sit below its stopping tolerance.
    """

    reduced: ReducedModel
    a_scaled: sparse.csc_matrix
    b_scaled: np.ndarray
    e_matrix: sparse.csc_matrix
    d_matrix: sparse.csc_matrix
    drag_above: np.ndarray
    """Brick ordinal owning each drag row (its ``above_id`` — the same
    attribution ``bottom_drag_cols`` gives the exact scorer)."""

    @classmethod
    def create(cls, reduced: ReducedModel) -> _ScreenArrays:
        rpb = reduced.rows_per_brick
        brick_count = len(reduced.brick_ids)
        weights = np.ones(rpb * brick_count)
        for i in range(brick_count):
            weights[rpb * i + 3 : rpb * (i + 1)] = 1.0 / KNOB_PITCH_M
        scale = sparse.diags(weights, format="csc")
        a_scaled = cast("sparse.csc_matrix", (scale @ reduced.a_matrix).tocsc())
        b_scaled = weights * reduced.b_vector
        e_matrix = cast("sparse.csc_matrix", reduced.expansion.tocsc())
        drag_cols = reduced.exact.drag_cols
        d_matrix = sparse.csc_matrix(reduced.expansion[drag_cols, :])
        brick_index = {bid: i for i, bid in enumerate(reduced.brick_ids)}
        drag_above = np.asarray(
            [brick_index[point.above_id] for point in reduced.exact.contact_points],
            dtype=np.int64,
        )
        return cls(
            reduced=reduced,
            a_scaled=a_scaled,
            b_scaled=b_scaled,
            e_matrix=e_matrix,
            d_matrix=d_matrix,
            drag_above=drag_above,
        )


@dataclass(slots=True)
class _QpSolution:
    """The screen QP's solution blocks."""

    residual: np.ndarray
    dmax: np.ndarray
    drag_values: np.ndarray


def _solve_qp(
    arrays: _ScreenArrays,
    config: SolverConfig,
    *,
    deadline: float | None,
) -> _QpSolution | None:
    """Assemble and solve the screen QP; ``None`` on non-convergence."""
    reduced = arrays.reduced
    n = reduced.var_count
    m = arrays.a_scaled.shape[0]
    rows_e = arrays.e_matrix.shape[0]
    rows_d = arrays.d_matrix.shape[0]
    brick_count = len(reduced.brick_ids)
    dmax_map = sparse.csc_matrix(
        (np.ones(rows_d), (np.arange(rows_d), arrays.drag_above)),
        shape=(rows_d, brick_count),
    )
    p_matrix = sparse.block_diag(
        (
            2.0 * _RIDGE * sparse.identity(n, format="csc"),
            2.0 * _W_RESIDUAL * sparse.identity(m, format="csc"),
            2.0 * _ALPHA_Q * sparse.identity(brick_count, format="csc"),
        ),
        format="csc",
    )
    constraints = sparse.vstack(
        [
            sparse.hstack(
                [
                    arrays.a_scaled,
                    -sparse.identity(m),
                    sparse.csc_matrix((m, brick_count)),
                ]
            ),
            sparse.hstack(
                [
                    arrays.e_matrix,
                    sparse.csc_matrix((rows_e, m + brick_count)),
                ]
            ),
            sparse.hstack(
                [
                    arrays.d_matrix,
                    sparse.csc_matrix((rows_d, m)),
                    -dmax_map,
                ]
            ),
            sparse.hstack(
                [
                    sparse.csc_matrix((brick_count, n + m)),
                    sparse.identity(brick_count),
                ]
            ),
        ],
        format="csc",
    )
    lower = np.concatenate(
        [
            -arrays.b_scaled,
            np.zeros(rows_e),
            np.full(rows_d, -np.inf),
            np.zeros(brick_count),
        ]
    )
    upper = np.concatenate(
        [
            -arrays.b_scaled,
            np.full(rows_e, np.inf),
            np.zeros(rows_d),
            np.full(brick_count, np.inf),
        ]
    )
    settings: dict[str, bool | int | float] = {
        "verbose": False,
        "polishing": False,
        "eps_abs": config.screen_eps,
        "eps_rel": config.screen_eps,
        "max_iter": config.screen_max_iter,
    }
    if deadline is not None:
        if (remaining := deadline - time.monotonic()) <= 0:
            return None
        settings["time_limit"] = remaining
    problem = osqp.OSQP()
    problem.setup(
        P=sparse.triu(p_matrix, format="csc"),
        q=np.zeros(n + m + brick_count),
        A=cast("sparse.csc_matrix", constraints),
        l=lower,
        u=upper,
        **settings,
    )
    result = problem.solve(raise_error=False)
    if "solved" not in str(result.info.status) or result.x is None:
        return None
    solution = np.asarray(result.x)
    x = solution[:n]
    return _QpSolution(
        residual=solution[n : n + m],
        dmax=solution[n + m :],
        drag_values=np.asarray(arrays.d_matrix @ x),
    )


def _score_report(
    arrays: _ScreenArrays,
    config: SolverConfig,
    solution: _QpSolution,
) -> ScreenReport:
    """Mirror the exact solver's scoring on the reduced solution."""
    reduced = arrays.reduced
    rpb = reduced.rows_per_brick
    scores: dict[int, float] = {}
    near_boundary = False
    low = (1.0 - config.screen_margin) * T_CAPACITY_N
    high = (1.0 + config.screen_margin) * T_CAPACITY_N
    for i, brick_id in enumerate(reduced.brick_ids):
        rows = solution.residual[rpb * i : rpb * (i + 1)]
        weight = abs(float(reduced.b_vector[rpb * i + 2]))
        tolerance = max(
            _EQ_WEIGHT_SHARE * weight,
            _EQ_NOISE_FLOOR * config.screen_eps,
        )
        in_equilibrium = bool(np.all(np.abs(rows) <= tolerance))
        drag_max = float(solution.dmax[i])
        if low <= drag_max <= high:
            near_boundary = True
        if not in_equilibrium or drag_max >= T_CAPACITY_N:
            scores[brick_id] = 1.0
        else:
            scores[brick_id] = drag_max / T_CAPACITY_N
    threshold = T_CAPACITY_N * (1.0 + _V_TOL)
    overloaded = frozenset(
        reduced.connection_pairs[ordinal]
        for ordinal in np.unique(
            reduced.drag_connection[solution.drag_values > threshold]
        )
    )
    unstable = frozenset(b for b, s in scores.items() if s >= 1.0)
    return ScreenReport(
        status="ok",
        stable=not unstable,
        q=max(scores.values(), default=0.0),
        scores=scores,
        overloaded=overloaded,
        unstable_ids=unstable,
        confident=not near_boundary,
        q_raw=float(solution.dmax.max()) / T_CAPACITY_N if solution.dmax.size else 0.0,
    )


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def screen_layout(
    layout: Layout,
    config: SolverConfig,
    graph: ConnectionGraph | None = None,
    *,
    deadline: float | None = None,
) -> ScreenReport:
    """Screen one layout with the reduced QP.

    Never raises for solver reasons: unsupported geometry returns
    ``status="declined"``, a failed solve ``"nonconverged"``, an
    expired deadline ``"deadline"`` — callers fall through to the cold
    path on anything but "ok".
    """
    with telemetry.span("stability.screen", n=len(layout)):
        return _screen_body(layout, config, graph, deadline=deadline)


def _screen_body(
    layout: Layout,
    config: SolverConfig,
    graph: ConnectionGraph | None,
    *,
    deadline: float | None,
) -> ScreenReport:
    """Run the body of :func:`screen_layout` without its outer span."""
    if _expired(deadline):
        telemetry.value("stability.screen.deadline_skip", 1.0)
        return ScreenReport(status="deadline")
    try:
        reduced = build_reduced_model(layout, config, graph)
    except Exception:  # noqa: BLE001 - screen must never propagate build bugs
        telemetry.value("stability.screen.error", 1.0)
        return ScreenReport(status="error")
    if reduced is None:
        telemetry.value("stability.screen.decline", 1.0)
        return ScreenReport(status="declined")
    return solve_screen(reduced, config, deadline=deadline)


def solve_screen(
    reduced: ReducedModel,
    config: SolverConfig,
    *,
    deadline: float | None = None,
) -> ScreenReport:
    """Solve the screen QP on an already-built reduced model."""
    try:
        arrays = _ScreenArrays.create(reduced)
        if _expired(deadline):
            telemetry.value("stability.screen.deadline_skip", 1.0)
            return ScreenReport(status="deadline")
        with telemetry.span("stability.screen.qp", n=reduced.var_count):
            solution = _solve_qp(arrays, config, deadline=deadline)
        if solution is None:
            if _expired(deadline):
                telemetry.value("stability.screen.deadline_skip", 1.0)
                return ScreenReport(status="deadline")
            telemetry.value("stability.screen.nonconverged", 1.0)
            return ScreenReport(status="nonconverged")
        return _score_report(arrays, config, solution)
    except Exception:  # noqa: BLE001 - screen must never propagate solver bugs
        telemetry.value("stability.screen.error", 1.0)
        return ScreenReport(status="error")


@dataclass(slots=True)
class ReducedScreen:
    """Baseline-holding screen for candidate accept/reject loops.

    Holds the current accepted layout's reduced verdict so candidate
    reports can be ranked like-for-like (screen-q against screen-q,
    never screen-q against a cold score).
    """

    config: SolverConfig
    baseline: ScreenReport

    @classmethod
    def create(cls, layout: Layout, config: SolverConfig) -> ReducedScreen | None:
        """Build and evaluate the baseline; ``None`` when declined."""
        report = screen_layout(layout, config)
        if report.status != "ok":
            return None
        return cls(config=config, baseline=report)

    @property
    def baseline_q(self) -> float:
        """The accepted layout's reduced score."""
        return self.baseline.q

    def evaluate(
        self,
        candidate: Layout,
        graph: ConnectionGraph | None = None,
        *,
        deadline: float | None = None,
    ) -> ScreenReport:
        """Screen one candidate layout."""
        return screen_layout(candidate, self.config, graph, deadline=deadline)

    def rebase(self, accepted_report: ScreenReport) -> None:
        """Advance the baseline to an accepted candidate's report."""
        if accepted_report.status == "ok":
            self.baseline = accepted_report

    def should_reject(self, report: ScreenReport, margin: float) -> bool:
        """Whether a candidate is confidently worse than the baseline.

        Worse means strictly more unstable bricks, or an equal count
        with the unclamped stress both a relative ``margin`` above the
        baseline and one absolute ``margin`` of capacity beyond it. The
        absolute headroom is load-bearing: deep in the relaxed regime
        (stress a few percent of capacity) the screen's restriction
        noise dwarfs real candidate differences, and a purely relative
        gate confidently rejects improvements (measured: 30% false
        rejects on shell removal candidates; 0% with the floor).
        Anything not solved confidently is never rejected — those
        candidates fall through to the cold path.
        """
        if report.status != "ok" or not report.confident:
            return False
        base = self.baseline
        if len(report.unstable_ids) > len(base.unstable_ids):
            return True
        return (
            len(report.unstable_ids) == len(base.unstable_ids)
            and report.q_raw > base.q_raw * (1.0 + margin) + margin
        )


__all__ = [
    "ReducedScreen",
    "ScreenReport",
    "ScreenStatus",
    "screen_layout",
    "solve_screen",
]
