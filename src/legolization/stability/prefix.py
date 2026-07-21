"""Fast prefix stability for sequencing: warm LPs, shortcuts, components.

The sequencer solves a fresh LP per candidate prefix - profiled at ~99% of
large-model runtime. This module cuts that three ways:

- :class:`PrefixSolver` keeps ONE ``highspy.Highs`` model alive across the
  greedy loop's monotonically growing prefix: probing a candidate chunk
  appends its rows/columns and re-solves warm from the retained simplex
  basis (iterations proportional to the chunk, not the model); committing
  the probed chunk is free (lazy rollback); rejecting it deletes the
  trailing rows/columns and restores the base basis. The solver owns its
  own append-order (brick, contact) -> (row, column) numbering, decoupled
  from ``build_model``'s sorted-id ordering, because placement ids
  interleave across chunks. Coefficients come from the same
  :func:`legolization.stability.model.force_entries` the batch assembler
  uses.
- The **floating shortcut**: a prefix containing a brick with no stud path
  to ground can never be in equilibrium, so its verdict (unstable, the
  floater scored exactly 1.0) needs no LP at all. This turns the dominant
  unstable-prefix class into a graph reachability check.
- :class:`RemovalSolver` serves the disassembly rescue, whose states stay
  too connected for LP-deletion warm starts to pay (removing a chunk
  deletes many basic variables; HiGHS effectively cold-starts). Instead it
  splits each state into contact components - the RBE is block-diagonal
  across bricks with no knob/side coupling - and caches per-component
  verdicts, so consecutive rescue states only re-solve what changed.

Exactness: the LP polytope is identical across engines; verdicts agree
except through solver-tolerance-level alternative optima on degenerate
states (the same drift scipy shows across its own versions). A boundary
guard cold-solves any prefix whose verdict sits near the stability
threshold, and every non-optimal warm solve falls back to the legacy
``analyze`` chain. Presolve stays off on the persistent model (required
for basis reuse - and incidentally immune to the known degenerate-presolve
failure). Engine selection lives on ``SolverConfig.engine``; the scipy
engine preserves the legacy path bit-for-bit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from legolization import telemetry
from legolization.graph import (
    GROUND_ID,
    ConnectionGraph,
    KnobContact,
    SideContact,
)
from legolization.stability.constants import (
    ALPHA,
    BETA,
    FOUR_POINT_OFFSETS,
    GRAVITY,
    K_DIRECTIONS,
    KNOB_PITCH_M,
    PLATE_HEIGHT_M,
    T_CAPACITY_N,
)
from legolization.stability.model import (
    brick_centroid,
    cavity_pattern,
    footprint_columns,
    force_entries,
    knob_pattern,
    rotate_pattern,
    rows_per_brick,
)
from legolization.stability.solver import (
    BrickScore,
    SolverConfig,
    StabilityResult,
    _solve_lp_highspy,
    analyze,
    build_model_from_config,
)

if TYPE_CHECKING:
    from legolization.layout import Layout

import highspy

_FZ = 2
_INF = math.inf
_UP = (0.0, 0.0, 1.0)
_DOWN = (0.0, 0.0, -1.0)

_Load = tuple[int, tuple[float, float, float], tuple[float, float, float]]


def _floating_shortcut(
    subset: frozenset[int],
    floating: set[int],
) -> StabilityResult:
    """LP-free verdict for a subset with stud-unreachable bricks.

    A floating brick can never be in equilibrium (its residual cannot be
    zeroed), so ``analyze`` scores it exactly 1.0 and the subset is
    unstable — no LP needed. Grounded bricks get score 0.0 here, which
    the exact LP would refine; consumers of unstable states only rank by
    the dominating 1.0 floaters. highspy-engine paths only.
    """
    scores = {
        bid: BrickScore(
            brick_id=bid,
            score=1.0 if bid in floating else 0.0,
            drag_max=0.0,
            in_equilibrium=bid not in floating,
        )
        for bid in subset
    }
    return StabilityResult(
        stable=False,
        scores=scores,
        weakest_pair=None,
        min_capacity=T_CAPACITY_N,
        status="floating-shortcut",
        objective=0.0,
    )


def _stud_reach_floating(
    subset: frozenset[int] | set[int],
    grounded: frozenset[int],
    stud_adjacent: dict[int, set[int]],
) -> set[int]:
    """Bricks in ``subset`` with no stud path to ground within the subset."""
    reached = {bid for bid in grounded if bid in subset}
    stack = list(reached)
    while stack:
        current = stack.pop()
        for neighbour in stud_adjacent.get(current, ()):
            if neighbour in subset and neighbour not in reached:
                reached.add(neighbour)
                stack.append(neighbour)
    return set(subset) - reached


class PrefixSolverError(RuntimeError):
    """Internal warm-solve failure; callers fall back to the legacy engine."""


def _require_optimal(status: object) -> None:
    """Raise :class:`PrefixSolverError` unless HiGHS reports optimality."""
    if status != highspy.HighsModelStatus.kOptimal:
        msg = f"warm solve status {status}"
        raise PrefixSolverError(msg)


@dataclass(slots=True)
class _Appendage:
    """Bookkeeping to undo one appended chunk (lazy rollback)."""

    chunk: frozenset[int]
    rows_before: int
    cols_before: int
    contact_pairs_before: int
    new_dmax_bricks: list[int] = field(default_factory=list)
    grown_bottom_drag: dict[int, int] = field(default_factory=dict)


@dataclass(slots=True)
class _ColumnBatch:
    """Accumulates one ``addCols`` call in CSC layout."""

    first_col: int
    costs: list[float] = field(default_factory=list)
    starts: list[int] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    def add(self, cost: float, entries: list[tuple[int, float]]) -> int:
        """Append one column; returns its final column index."""
        col = self.first_col + len(self.costs)
        self.costs.append(cost)
        self.starts.append(len(self.indices))
        for row, value in entries:
            self.indices.append(row)
            self.values.append(value)
        return col

    def __len__(self) -> int:
        return len(self.costs)


class PrefixSolver:
    """Incremental warm-started LP over a growing prefix of one layout."""

    def __init__(self, layout: Layout, config: SolverConfig) -> None:
        self._layout = layout
        self._config = config
        self._rpb = rows_per_brick(torque_z=config.torque_z)
        self._block_rows = 2 * self._rpb  # upper + lower residual rows
        self._h = highspy.Highs()
        self._h.setOptionValue("output_flag", False)  # noqa: FBT003 - pybind
        self._h.setOptionValue("presolve", "off")
        self._h.setOptionValue("solver", "simplex")
        self._h.setOptionValue("threads", 1)
        # Direction-aware contacts are extracted once; `present` filters
        # them at append time. Sharing ConnectionGraph with the cold model
        # prevents vertical-only SNOT mistakes and phantom cladding faces.
        self._graph = ConnectionGraph.from_layout(layout)
        self._knobs_by_brick: dict[int, list[KnobContact]] = {
            bid: [] for bid in layout.bricks
        }
        for knob in self._graph.knob_contacts:
            if knob.below_id != GROUND_ID:
                self._knobs_by_brick[knob.below_id].append(knob)
            self._knobs_by_brick[knob.above_id].append(knob)
        self._centroid: dict[int, tuple[float, float, float]] = {}
        self._mass_kg: dict[int, float] = {}
        self._pattern: dict[int, tuple[tuple[float, float], ...]] = {}
        self._footprint: dict[int, tuple[frozenset[tuple[int, int]], int]] = {}
        self._yaw: dict[int, int] = {}
        for brick in layout:
            bid = brick.brick_id
            self._centroid[bid] = brick_centroid(layout, bid)
            self._yaw[bid] = brick.yaw
            self._mass_kg[bid] = layout.part_of(brick).mass_g / 1000.0
            self._pattern[bid] = cavity_pattern(layout, bid)
            if config.paper_knob_rule:
                self._footprint[bid] = footprint_columns(layout, bid)
        self._build_reachability()
        # Canonical append-order index state.
        self.present: set[int] = set()
        self._row_base: dict[int, int] = {}
        self._row_count = 0
        self._col_count = 0
        self._t_cols: dict[int, int] = {}  # first of each brick's 5 t columns
        self._dmax_col: dict[int, int] = {}
        self._bottom_drag: dict[int, list[int]] = {}
        self._contact_pairs: list[tuple[int, int, int]] = []  # (below, above, drag)
        self._base_basis: highspy.HighsBasis | None = None
        self._pending: _Appendage | None = None

    # -- public API --------------------------------------------------------

    @classmethod
    def create(
        cls,
        layout: Layout,
        config: SolverConfig | None,
    ) -> PrefixSolver | None:
        """Build a solver, or None when the warm engine is unavailable."""
        config = config or SolverConfig()
        if config.mode != "lp" or config.engine != "highspy":
            return None
        try:
            return cls(layout, config)
        except Exception:  # noqa: BLE001 - any init failure means "use legacy"
            return None

    def probe(self, chunk: tuple[int, ...]) -> StabilityResult:
        """Solve base | chunk warm; appended state stays pending."""
        size = len(self.present) + len(chunk)
        with telemetry.span("stability.prefix.probe", n=size):
            return self._probe_body(chunk)

    def press_probe(
        self,
        chunk: tuple[int, ...],
        extra_mass_kg: float,
    ) -> StabilityResult:
        """Solve base | chunk with a press load on every chunk brick.

        Liu et al. 2024's virtual-brick insertion model: the gravity RHS
        of each pressed brick grows by ``extra_mass_kg`` (the same
        semantics as ``analyze(extra_masses=dict.fromkeys(chunk, kg))``).
        Only row bounds change, so the warm basis re-converges in a few
        dual-simplex iterations instead of a cold LP. Appended state
        stays pending; the press bounds are ALWAYS restored before this
        returns, so a following ``commit`` can never bake a press into
        the base model.
        """
        size = len(self.present) + len(chunk)
        with telemetry.span("stability.prefix.press", n=size):
            return self._press_body(chunk, extra_mass_kg)

    def press_probe_selection(
        self,
        chunk: tuple[int, ...],
        press_ids: tuple[int, ...],
        extra_mass_kg: float,
    ) -> StabilityResult:
        """Append ``chunk`` while pressing only ``press_ids``.

        Used by press-aware chunk refinement to evaluate the second half
        of a prospective split without committing the first half.
        """
        if not set(press_ids) <= set(chunk):
            msg = "pressed ids must be a subset of the appended chunk"
            raise ValueError(msg)
        size = len(self.present) + len(chunk)
        with telemetry.span("stability.prefix.press", n=size):
            return self._press_body(
                chunk,
                extra_mass_kg,
                press_ids=frozenset(press_ids),
            )

    def commit(self, chunk: tuple[int, ...]) -> None:
        """Advance the base by ``chunk`` (zero LP when it was just probed)."""
        wanted = frozenset(chunk) - self.present
        if self._pending is not None and self._pending.chunk == wanted:
            self._base_basis = self._h.getBasis()
            self._pending = None
            self.present |= wanted
            return
        with telemetry.span("stability.prefix.commit"):
            try:
                self._rollback()
                if wanted:
                    self._append(wanted)
                    self._base_basis = self._extended_basis(self._base_basis)
                    self._pending = None
            except Exception:  # noqa: BLE001 - rebuild below covers all failures
                self.present |= wanted
                self._hard_reset()
                return
            self.present |= wanted

    # -- probe internals ---------------------------------------------------

    def _probe_body(self, chunk: tuple[int, ...]) -> StabilityResult:
        wanted = frozenset(chunk) - self.present
        subset = frozenset(self.present | wanted)
        floating = _stud_reach_floating(subset, self._grounded, self._stud_adjacent)
        # Cross-check mode promises every probe a cold comparison; the
        # shortcut's synthetic scores would dodge it (PR #17 review), so
        # it only fires when the mode is off.
        if floating and not self._config.engine_cross_check:
            with telemetry.span("stability.prefix.floating_shortcut"):
                self._rollback()
                return _floating_shortcut(subset, floating)
        try:
            self._rollback()
            self._append(wanted)
            self._h.setBasis(self._extended_basis(self._base_basis))
            self._h.run()
            _require_optimal(self._h.getModelStatus())
            result, near_boundary = self._extract(subset)
        except PrefixSolverError:
            return self._cold_fallback(subset, span="stability.prefix.warm_fail")
        except Exception:  # noqa: BLE001 - contain highspy surprises
            self._hard_reset()
            return self._cold_fallback(subset, span="stability.prefix.warm_fail")
        if near_boundary:
            return self._cold_fallback(
                subset, span="stability.prefix.boundary_fallback"
            )
        if self._config.engine_cross_check:
            return self._cross_check(subset, result)
        return result

    def _press_body(
        self,
        chunk: tuple[int, ...],
        extra_mass_kg: float,
        *,
        press_ids: frozenset[int] | None = None,
    ) -> StabilityResult:
        wanted = frozenset(chunk) - self.present
        subset = frozenset(self.present | wanted)
        press_ids = press_ids if press_ids is not None else frozenset(chunk)
        if not hasattr(self._h, "changeRowBounds"):
            # Net-new highspy API: older bindings take the cold path.
            return self._cold_press(
                subset, press_ids, extra_mass_kg, span="stability.prefix.press_cold"
            )
        floating = _stud_reach_floating(subset, self._grounded, self._stud_adjacent)
        if floating and not self._config.engine_cross_check:
            with telemetry.span("stability.prefix.floating_shortcut"):
                self._rollback()
                return _floating_shortcut(subset, floating)
        try:
            self._rollback()
            self._append(wanted)
            try:
                self._press_rows(press_ids, extra_mass_kg)
                self._h.setBasis(self._extended_basis(self._base_basis))
                self._h.run()
                _require_optimal(self._h.getModelStatus())
                result, near_boundary = self._extract(subset)
            finally:
                # Restore BEFORE anything can commit: the base model must
                # never carry press bounds forward.
                self._press_rows(press_ids, 0.0)
        except Exception as error:  # noqa: BLE001 - contain highspy surprises
            if not isinstance(error, PrefixSolverError):
                self._hard_reset()
            return self._cold_press(
                subset, press_ids, extra_mass_kg, span="stability.prefix.warm_fail"
            )
        if near_boundary:
            return self._cold_press(
                subset,
                press_ids,
                extra_mass_kg,
                span="stability.prefix.boundary_fallback",
            )
        if self._config.engine_cross_check:
            return self._press_cross_check(subset, press_ids, extra_mass_kg, result)
        return result

    def _press_rows(self, press_ids: frozenset[int], extra_mass_kg: float) -> None:
        """Set each pressed brick's FZ row bounds for its loaded mass."""
        for bid in sorted(press_ids):
            base = self._row_base[bid]
            load = (self._mass_kg[bid] + extra_mass_kg) * GRAVITY
            self._h.changeRowBounds(base + _FZ, -_INF, load)
            self._h.changeRowBounds(base + self._rpb + _FZ, -_INF, -load)

    def _cold_press(
        self,
        subset: frozenset[int],
        press_ids: frozenset[int],
        extra_mass_kg: float,
        *,
        span: str,
    ) -> StabilityResult:
        with telemetry.span(span):
            return analyze(
                self._layout.subset(subset),
                self._config,
                extra_masses=dict.fromkeys(press_ids, extra_mass_kg),
            )

    def _press_cross_check(
        self,
        subset: frozenset[int],
        press_ids: frozenset[int],
        extra_mass_kg: float,
        warm: StabilityResult,
    ) -> StabilityResult:
        """Debug mode: measure warm-vs-cold press drift, return the COLD."""
        cold = analyze(
            self._layout.subset(subset),
            self._config,
            extra_masses=dict.fromkeys(press_ids, extra_mass_kg),
        )
        drift = max(
            (abs(warm.scores[b].score - cold.scores[b].score) for b in cold.scores),
            default=0.0,
        )
        if cold.stable != warm.stable or drift > 1e-6:
            with telemetry.span("stability.prefix.cross_check_mismatch"):
                pass
        return cold

    def _cross_check(
        self,
        subset: frozenset[int],
        warm: StabilityResult,
    ) -> StabilityResult:
        """Debug mode: measure warm-vs-cold drift, return the COLD result."""
        cold = analyze(self._layout.subset(subset), self._config)
        drift = max(
            (abs(warm.scores[b].score - cold.scores[b].score) for b in cold.scores),
            default=0.0,
        )
        if cold.stable != warm.stable or drift > 1e-6:
            with telemetry.span("stability.prefix.cross_check_mismatch"):
                pass
        return cold

    def _cold_fallback(
        self,
        subset: frozenset[int],
        *,
        span: str,
    ) -> StabilityResult:
        with telemetry.span(span):
            return analyze(self._layout.subset(subset), self._config)

    def _hard_reset(self) -> None:
        """Rebuild the whole base model from scratch after a highspy failure."""
        with telemetry.span("stability.prefix.rebuild"):
            base = frozenset(self.present)
            fresh = PrefixSolver(self._layout, self._config)
            self.__dict__.update(fresh.__dict__)  # adopt the clean state
            if base:
                self._append(base)
                self._base_basis = self._extended_basis(None)
                self._pending = None
            self.present = set(base)

    # -- incremental assembly ----------------------------------------------

    def _rollback(self) -> None:
        pending = self._pending
        if pending is None:
            return
        rows_to_drop = self._row_count - pending.rows_before
        cols_to_drop = self._col_count - pending.cols_before
        if rows_to_drop:
            self._h.deleteRows(
                rows_to_drop, list(range(pending.rows_before, self._row_count))
            )
        if cols_to_drop:
            self._h.deleteCols(
                cols_to_drop, list(range(pending.cols_before, self._col_count))
            )
        for bid in pending.chunk:
            self._row_base.pop(bid, None)
            self._t_cols.pop(bid, None)
            self._dmax_col.pop(bid, None)
            self._bottom_drag.pop(bid, None)
        for bid in pending.new_dmax_bricks:
            self._dmax_col.pop(bid, None)
        for bid, grown in pending.grown_bottom_drag.items():
            drags = self._bottom_drag.get(bid)
            if drags is not None:
                del drags[len(drags) - grown :]
        del self._contact_pairs[pending.contact_pairs_before :]
        self._row_count = pending.rows_before
        self._col_count = pending.cols_before
        self._pending = None
        if self._base_basis is not None:
            self._h.setBasis(self._base_basis)

    def _append(self, chunk: frozenset[int]) -> None:
        appendage = _Appendage(
            chunk=chunk,
            rows_before=self._row_count,
            cols_before=self._col_count,
            contact_pairs_before=len(self._contact_pairs),
        )
        ordered = sorted(chunk)
        self._append_structural_rows(ordered)
        batch = _ColumnBatch(first_col=self._col_count)
        drag_links: list[tuple[int, int]] = []
        for bid in ordered:
            self._discover_knobs(bid, chunk, batch, drag_links, appendage)
        self._discover_sides(chunk, ordered, batch)
        for bid in ordered:
            base = self._row_base[bid]
            self._t_cols[bid] = batch.first_col + len(batch)
            for i in range(self._rpb):
                batch.add(1.0, [(base + i, -1.0), (base + self._rpb + i, -1.0)])
        for bid in ordered:
            if self._bottom_drag.get(bid) and bid not in self._dmax_col:
                self._dmax_col[bid] = batch.add(ALPHA, [])
        for bid in appendage.grown_bottom_drag:
            if bid not in self._dmax_col:
                self._dmax_col[bid] = batch.add(ALPHA, [])
                appendage.new_dmax_bricks.append(bid)
        if len(batch):
            self._h.addCols(
                len(batch),
                batch.costs,
                [0.0] * len(batch),
                [_INF] * len(batch),
                len(batch.indices),
                batch.starts,
                batch.indices,
                batch.values,
            )
            self._col_count += len(batch)
        self._append_drag_link_rows(drag_links)
        self._pending = appendage

    def _append_structural_rows(self, ordered: list[int]) -> None:
        row_lower: list[float] = []
        row_upper: list[float] = []
        for bid in ordered:
            self._row_base[bid] = self._row_count
            self._row_count += self._block_rows
            gravity_b = -self._mass_kg[bid] * GRAVITY
            for i in range(self._rpb):  # upper: A F - t <= -b
                row_lower.append(-_INF)
                row_upper.append(-gravity_b if i == _FZ else 0.0)
            for i in range(self._rpb):  # lower: -A F - t <= +b
                row_lower.append(-_INF)
                row_upper.append(gravity_b if i == _FZ else 0.0)
        if row_lower:
            self._h.addRows(len(row_lower), row_lower, row_upper, 0, [], [], [])

    def _append_drag_link_rows(self, drag_links: list[tuple[int, int]]) -> None:
        if not drag_links:
            return
        starts: list[int] = []
        indices: list[int] = []
        values: list[float] = []
        for drag_col, owner in drag_links:
            starts.append(len(indices))
            indices.extend((drag_col, self._dmax_col[owner]))
            values.extend((1.0, -1.0))
        self._h.addRows(
            len(drag_links),
            [-_INF] * len(drag_links),
            [0.0] * len(drag_links),
            len(indices),
            starts,
            indices,
            values,
        )
        self._row_count += len(drag_links)

    def _build_reachability(self) -> None:
        """Direction-aware stud reachability for the floating shortcut."""
        self._grounded = self._graph.grounded_ids
        self._stud_adjacent = {bid: set() for bid in self._layout.bricks}
        for knob in self._graph.knob_contacts:
            if knob.below_id == GROUND_ID:
                continue
            self._stud_adjacent[knob.below_id].add(knob.above_id)
            self._stud_adjacent[knob.above_id].add(knob.below_id)

    def _force_col(
        self,
        batch: _ColumnBatch,
        cost: float,
        loads: list[_Load],
    ) -> int:
        entries: list[tuple[int, float]] = []
        for bid, direction, position in loads:
            base = self._row_base[bid]
            for offset, coeff in force_entries(
                self._centroid[bid],
                position,
                direction,
                torque_z=self._config.torque_z,
            ):
                entries.append((base + offset, coeff))
                entries.append((base + self._rpb + offset, -coeff))
        return batch.add(cost, entries)

    def _discover_knobs(
        self,
        bid: int,
        chunk: frozenset[int],
        batch: _ColumnBatch,
        drag_links: list[tuple[int, int]],
        appendage: _Appendage,
    ) -> None:
        """Emit knob interfaces where ``bid`` is new and involved."""
        subset = self.present | set(chunk)
        for knob in self._knobs_by_brick[bid]:
            endpoints = {knob.above_id}
            if knob.below_id != GROUND_ID:
                endpoints.add(knob.below_id)
            if not endpoints <= subset:
                continue
            new_endpoints = endpoints & chunk
            if not new_endpoints or bid != min(new_endpoints):
                continue
            if knob.normal == (0, 0, 1):
                self._emit_knob(
                    knob.below_id,
                    knob.above_id,
                    knob.x,
                    knob.y,
                    float(knob.interface_layer),
                    chunk,
                    batch,
                    drag_links,
                    appendage,
                )
            else:
                self._emit_lateral_knob(
                    knob,
                    chunk,
                    batch,
                    drag_links,
                    appendage,
                )

    def _emit_lateral_knob(
        self,
        knob: KnobContact,
        chunk: frozenset[int],
        batch: _ColumnBatch,
        drag_links: list[tuple[int, int]],
        appendage: _Appendage,
    ) -> None:
        """Emit the cold model's sideways normal, drag, and shear columns."""
        nx, ny, _ = knob.normal
        tx, ty = float(-ny), float(nx)
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
            loads_normal: list[_Load] = [(knob.above_id, outward, position)]
            loads_drag: list[_Load] = [(knob.above_id, inward, position)]
            if knob.below_id != GROUND_ID:
                loads_normal.append((knob.below_id, inward, position))
                loads_drag.append((knob.below_id, outward, position))
            self._force_col(batch, 0.0, loads_normal)
            drag_col = self._force_col(batch, BETA, loads_drag)
            self._bottom_drag.setdefault(knob.above_id, []).append(drag_col)
            if knob.above_id not in chunk:
                appendage.grown_bottom_drag[knob.above_id] = (
                    appendage.grown_bottom_drag.get(knob.above_id, 0) + 1
                )
            drag_links.append((drag_col, knob.above_id))
            self._contact_pairs.append((knob.below_id, knob.above_id, drag_col))
        center = (float(x_pos), float(y_pos), float(z_center))
        directions = (
            (tx, ty, 0.0),
            (-tx, -ty, 0.0),
            (0.0, 0.0, 1.0),
            (0.0, 0.0, -1.0),
        )
        for direction in directions:
            loads: list[_Load] = [(knob.above_id, direction, center)]
            if knob.below_id != GROUND_ID:
                loads.append(
                    (
                        knob.below_id,
                        (-direction[0], -direction[1], -direction[2]),
                        center,
                    )
                )
            self._force_col(batch, 0.0, loads)

    def _emit_knob(  # noqa: PLR0913 - one scalar per knob attribute
        self,
        below: int,
        above: int,
        x: int,
        y: int,
        z_plane: float,
        chunk: frozenset[int],
        batch: _ColumnBatch,
        drag_links: list[tuple[int, int]],
        appendage: _Appendage,
    ) -> None:
        if self._config.paper_knob_rule:
            columns, min_dim = self._footprint[above]
            pattern = knob_pattern(columns, min_dim, (x, y))
        else:
            pattern = self._pattern[above]
        if self._config.rotate_contact_pattern:
            pattern = rotate_pattern(pattern, self._yaw[above])
        for ox, oy in pattern:
            position = (x + ox, y + oy, z_plane)
            loads_normal: list[_Load] = [(above, _UP, position)]
            loads_drag: list[_Load] = []
            # Mirrors the batch assembler's pull-free ground: the drag
            # column exists but carries no entries when the baseplate
            # cannot pull.
            if self._config.ground_pull or below != GROUND_ID:
                loads_drag.append((above, _DOWN, position))
            if below != GROUND_ID:
                loads_normal.append((below, _DOWN, position))
                loads_drag.append((below, _UP, position))
            self._force_col(batch, 0.0, loads_normal)
            drag_col = self._force_col(batch, BETA, loads_drag)
            self._bottom_drag.setdefault(above, []).append(drag_col)
            if above not in chunk:
                appendage.grown_bottom_drag[above] = (
                    appendage.grown_bottom_drag.get(above, 0) + 1
                )
            drag_links.append((drag_col, above))
            self._contact_pairs.append((below, above, drag_col))
        knob_center = (float(x), float(y), z_plane)
        for ux, uy in K_DIRECTIONS:
            loads: list[_Load] = [(above, (ux, uy, 0.0), knob_center)]
            if below != GROUND_ID:
                loads.append((below, (-ux, -uy, 0.0), knob_center))
            self._force_col(batch, 0.0, loads)

    def _discover_sides(
        self,
        chunk: frozenset[int],
        ordered: list[int],
        batch: _ColumnBatch,
    ) -> None:
        """Emit side interfaces with at least one new endpoint."""
        del ordered
        subset = self.present | set(chunk)
        for side in self._graph.side_contacts:
            endpoints = {side.a_id, side.b_id}
            if endpoints <= subset and endpoints & chunk:
                self._emit_side_presses(batch, side)

    def _emit_side_presses(
        self,
        batch: _ColumnBatch,
        side: SideContact,
    ) -> None:
        """Press generators for one shared-face pair.

        Mirrors model.py's side-press generators: two vertical extremes
        without yaw torque, four (transverse, vertical) corners with it
        (same spanning argument).
        """
        unit = (1.0, 0.0, 0.0) if side.axis == 0 else (0.0, 1.0, 0.0)
        away = (-unit[0] * side.direction, -unit[1] * side.direction, 0.0)
        toward = (unit[0] * side.direction, unit[1] * side.direction, 0.0)
        cx, cy, _ = side.centroid
        for z_edge in (float(side.z_lo), float(side.z_hi + 1)):
            if self._config.torque_z:
                spots = [side.t_lo, side.t_hi]
            else:
                spots = [cy if side.axis == 0 else cx]
            for t_coord in spots:
                position = (
                    (cx, t_coord, z_edge) if side.axis == 0 else (t_coord, cy, z_edge)
                )
                self._force_col(
                    batch,
                    0.0,
                    [
                        (side.a_id, away, position),
                        (side.b_id, toward, position),
                    ],
                )

    # -- basis + extraction ------------------------------------------------

    def _extended_basis(self, base: highspy.HighsBasis | None) -> highspy.HighsBasis:
        """Extend a basis with basic slacks for new rows, nonbasic new cols."""
        basis = highspy.HighsBasis()
        base_cols = list(base.col_status) if base is not None else []
        base_rows = list(base.row_status) if base is not None else []
        lower = highspy.HighsBasisStatus.kLower
        basic = highspy.HighsBasisStatus.kBasic
        basis.col_status = base_cols + [lower] * (self._col_count - len(base_cols))
        basis.row_status = base_rows + [basic] * (self._row_count - len(base_rows))
        basis.valid = True
        return basis

    def _extract(
        self,
        subset: frozenset[int],
    ) -> tuple[StabilityResult, bool]:
        """Build a StabilityResult from the current optimum.

        At an optimum the ``t`` columns equal the absolute residuals (their
        cost is strictly positive), so equilibrium checks read them
        directly — mirroring ``solver._score``'s |residual| tests.
        """
        value = self._h.getSolution().col_value
        config = self._config
        near = False
        scores: dict[int, BrickScore] = {}
        for bid in subset:
            t0 = self._t_cols[bid]
            t_vals = value[t0 : t0 + self._rpb]
            force_ok = all(v <= config.tol_force for v in t_vals[:3])
            torque_ok = all(v <= config.tol_torque for v in t_vals[3:])
            drags = self._bottom_drag.get(bid)
            drag_max = max((value[c] for c in drags), default=0.0) if drags else 0.0
            in_equilibrium = force_ok and torque_ok
            if not in_equilibrium or drag_max >= T_CAPACITY_N:
                score = 1.0
            else:
                score = drag_max / T_CAPACITY_N
            scores[bid] = BrickScore(
                brick_id=bid,
                score=score,
                drag_max=drag_max,
                in_equilibrium=in_equilibrium,
            )
            margin = config.boundary_margin
            low = (1.0 - margin) * T_CAPACITY_N
            high = (1.0 + margin) * T_CAPACITY_N
            if low <= drag_max <= high:
                near = True
            if any(
                tol / 10.0 < v < tol * 10.0
                for v, tol in zip(
                    t_vals,
                    # 3 force rows + (rpb - 3) torque rows: torque_z adds
                    # a sixth residual, and the hard-coded 5 made this
                    # strict zip raise — silently demoting every
                    # torque_z=True probe to the cold engine via the
                    # broad warm-fail handler (PR #20 review, severity 3).
                    (config.tol_force,) * 3 + (config.tol_torque,) * (self._rpb - 3),
                    strict=True,
                )
            ):
                near = True
        weakest_pair: tuple[int, int] | None = None
        min_capacity = T_CAPACITY_N
        for below, above, drag_col in self._contact_pairs:
            capacity = T_CAPACITY_N - float(value[drag_col])
            if capacity < min_capacity:
                min_capacity = capacity
                weakest_pair = (below, above)
        stable = all(s.score < 1.0 for s in scores.values())
        info = self._h.getInfo()
        result = StabilityResult(
            stable=stable,
            scores=scores,
            weakest_pair=weakest_pair,
            min_capacity=min_capacity,
            status="optimal",
            objective=float(info.objective_function_value),
        )
        return result, near


class RemovalSolver:
    """Component-decomposed analysis for the shrinking disassembly walk.

    LP-deletion warm starts do not pay in HiGHS (removing a chunk deletes
    many basic variables; the repaired basis effectively cold-starts), but
    the rescue's mid-build states decompose into independent contact
    components — a stem plus scattered floating fragments — and the RBE is
    block-diagonal across components (no knob, side, or ground coupling
    between them). Each probe therefore solves only the components not
    already cached, which after the first state means the one or two
    components the removal touched. Per-component results merge exactly:
    the concatenation of block optima is an optimum of the joint LP.

    Kept under the ``RemovalSolver`` name/API used by the disassembly
    search; the per-component solves run through the legacy ``analyze``
    chain, so all its retry/telemetry behaviour applies.
    """

    def __init__(
        self,
        layout: Layout,
        scope: frozenset[int],
        config: SolverConfig,
    ) -> None:
        self._layout = layout
        self._config = config
        self.scope: set[int] = set(scope)
        graph = ConnectionGraph.from_layout(layout)
        self._adjacent = {bid: set() for bid in layout.bricks}
        self._stud_adjacent = {bid: set() for bid in layout.bricks}
        for knob in graph.knob_contacts:
            if knob.below_id == GROUND_ID:
                continue
            self._adjacent[knob.below_id].add(knob.above_id)
            self._adjacent[knob.above_id].add(knob.below_id)
            self._stud_adjacent[knob.below_id].add(knob.above_id)
            self._stud_adjacent[knob.above_id].add(knob.below_id)
        for side in graph.side_contacts:
            self._adjacent[side.a_id].add(side.b_id)
            self._adjacent[side.b_id].add(side.a_id)
        self._grounded = graph.grounded_ids
        self._component_cache: dict[frozenset[int], StabilityResult] = {}

    @classmethod
    def create(
        cls,
        layout: Layout,
        scope: frozenset[int],
        config: SolverConfig | None,
    ) -> RemovalSolver | None:
        """Build a removal solver, or None when the warm engine is off."""
        config = config or SolverConfig()
        if config.mode != "lp" or config.engine != "highspy":
            return None
        return cls(layout, scope, config)

    def probe_without(self, chunk: tuple[int, ...]) -> StabilityResult:
        """Analyze ``scope - chunk`` by merging per-component verdicts."""
        subset = frozenset(self.scope - set(chunk))
        with telemetry.span("stability.prefix.remove_probe", n=len(subset)):
            floating = _stud_reach_floating(subset, self._grounded, self._stud_adjacent)
            # Cross-check mode wants exact scores everywhere: skip the
            # synthetic-score shortcut and analyze cold (PR #17 review).
            if floating and not self._config.engine_cross_check:
                with telemetry.span("stability.prefix.floating_shortcut"):
                    return _floating_shortcut(subset, floating)
            return self._analyze_components(subset)

    def commit_without(self, chunk: tuple[int, ...]) -> None:
        """Permanently remove ``chunk`` from the scope."""
        self.scope -= set(chunk)

    def _analyze_components(self, subset: frozenset[int]) -> StabilityResult:
        if not subset:
            return StabilityResult(stable=True)
        components = self._components(subset)
        results = []
        for component in components:
            if (hit := self._component_cache.get(component)) is None:
                hit = self._cold_component(component)
                self._component_cache[component] = hit
            results.append(hit)
        if len(results) == 1:
            return results[0]
        scores: dict[int, BrickScore] = {}
        weakest_pair: tuple[int, int] | None = None
        min_capacity = T_CAPACITY_N
        stable = True
        objective = 0.0
        for result in results:
            scores.update(result.scores)
            stable = stable and result.stable
            objective += result.objective
            if result.min_capacity < min_capacity:
                min_capacity = result.min_capacity
                weakest_pair = result.weakest_pair
        return StabilityResult(
            stable=stable,
            scores=scores,
            weakest_pair=weakest_pair,
            min_capacity=min_capacity,
            status="optimal",
            objective=objective,
        )

    def _cold_component(self, component: frozenset[int]) -> StabilityResult:
        """Solve one uncached contact component from scratch.

        Components below ``rescue_direct_min_bricks`` keep the
        scipy-exact ``analyze`` path (pinned to 1e-6 by the equivalence
        tests); larger ones — the ~80 grounded-stable rescue solves that
        dominate spot@24 — go through highspy directly, with the scipy
        path as the fallback for near-boundary verdicts and any solver
        failure. Verdict-preserving by construction: only score drift
        within the accepted alternative-optima class can differ.
        """
        sub_layout = self._layout.subset(component)
        if len(component) < self._config.rescue_direct_min_bricks:
            return analyze(sub_layout, self._config)
        try:
            with telemetry.span("stability.rescue.cold_direct", n=len(component)):
                direct, near_boundary = _solve_lp_highspy(
                    build_model_from_config(sub_layout, self._config),
                    self._config,
                )
        except Exception:  # noqa: BLE001 - any failure means scipy-exact
            with telemetry.span("stability.rescue.cold_fallback", n=len(component)):
                return analyze(sub_layout, self._config)
        if near_boundary:
            with telemetry.span("stability.rescue.cold_fallback", n=len(component)):
                return analyze(sub_layout, self._config)
        if self._config.engine_cross_check:
            cold = analyze(sub_layout, self._config)
            drift = max(
                (
                    abs(direct.scores[k].score - cold.scores[k].score)
                    for k in cold.scores
                ),
                default=0.0,
            )
            if direct.stable != cold.stable or drift > 1e-6:
                with telemetry.span("stability.rescue.cross_check_mismatch"):
                    pass
            return cold
        return direct

    def _components(self, subset: frozenset[int]) -> list[frozenset[int]]:
        remaining = set(subset)
        components: list[frozenset[int]] = []
        while remaining:
            seed = next(iter(remaining))
            stack = [seed]
            component = {seed}
            remaining.discard(seed)
            while stack:
                current = stack.pop()
                for neighbour in self._adjacent[current]:
                    if neighbour in remaining:
                        remaining.discard(neighbour)
                        component.add(neighbour)
                        stack.append(neighbour)
            components.append(frozenset(component))
        return components
