"""Structure-preserving bridge synthesis for the connectivity pass.

`improve_connectivity` historically re-tiled the repair ring with a
random maximal merge — measured in docs/kollsker-drift-report.md as the
count-inflation hotspot (+155 bricks on mushroom's 112-brick minimum
tiling even under best-of-k). This module replaces the random rewrite
for layered strategies with a principled one: the ring's cells are
re-decomposed through the same absolute-3-plate slab policy the
strategies place with (`slab_problems`), and each slab component is
solved by a two-stage exact-cover MILP — stage 1 minimizes the part
count **subject to actually bridging** (at least one chosen rect must
touch two stud-graph components; a minimum cover that reproduces the
fragmenting seam is infeasible under that row), stage 2 pins the count
and maximizes extra component crossings plus Kollsker's stagger reward.

The known trap this dodges: a pure minimum-count cover of the ring can
reproduce the very straight seam that fragmented the layout. The
bridging row makes that cover infeasible, and stage 2 spends the
equal-count degrees of freedom on more crossings and better bond.

When the per-slab pass declines (measured v5 residual: interleaved
shells re-fragment because each slab is covered in isolation), the
synthesizer escalates to a single cross-slab MILP over every slab
problem jointly: a hub-aggregated single-commodity flow ties the exact
covers together so every chosen rect must be flow-connected — through
chosen rects, remaining components, or the ground — to a root
component. Slab problems talk through hub nodes keyed by the actual
MATING PLANE (a plate's top at plane z meets a brick's bottom at plane
z regardless of slab phase). Known approximation, guarded by the
strict `after < before` acceptance: flow may route through the ground
or a grounded non-root chain, so a specific terminal merge is not
guaranteed — the count never increases, and the best-of-k comparison
in `improve_connectivity` keeps whichever bridge is leaner.

Re-phasing-only bridges (plate columns joined by `compact_columns`'
mod-3 vote in the random path) are invisible to an absolute-slab
re-tiling; the synthesizer returns None for those rings and the random
fallback still covers them. No rng is consumed on the MILP path, so
runs stay deterministic and the fallback's draw sequence is unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import Bounds, LinearConstraint
from scipy.sparse import coo_matrix

from legolization import telemetry
from legolization.catalog import Catalog, default_catalog
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.placement.layered.engine import (
    LayerContext,
    LayerProblem,
    Rect2D,
    build_context,
    enumerate_layer_rects,
    realize,
    slab_problems,
)
from legolization.placement.layered.kollsker import (
    _RANK_EPS,
    _components,
    _cover_matrix,
    _guarded_milp,
    bond_reward,
)
from legolization.placement.merge import _MERGEABLE, _cell_code, compact_vertical

if TYPE_CHECKING:
    from collections.abc import Callable

    from legolization.catalog import Cell
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout

    BridgeFn = Callable[[Layout, set[int], VoxelGrid], Layout | None]

_MIN_SOLVE_S = 0.05

_Node = tuple[str, object]
"""Flow-graph node key: ("r", rect index), ("h", (column, plane)),
("c", component label), or ("g", 0) for the ground."""


@dataclass(slots=True)
class _FlowEntry:
    """One slab component's slice of the global candidate list."""

    problem: LayerProblem
    context: LayerContext
    component: list[tuple[int, int]]
    rects: list[Rect2D]
    offset: int


@dataclass(slots=True)
class _FlowGraph:
    """Hub-aggregated arc set with node bookkeeping."""

    arcs: list[tuple[int, int]]
    node_ids: dict[_Node, int]
    root: int
    terminals: list[int]


@dataclass(slots=True)
class _RowBuilder:
    """Shared COO accumulator for the joint constraint system."""

    rows: list[int] = field(default_factory=list)
    cols: list[int] = field(default_factory=list)
    data: list[float] = field(default_factory=list)
    lb: list[float] = field(default_factory=list)
    ub: list[float] = field(default_factory=list)

    def entry(self, row: int, col: int, value: float) -> None:
        self.rows.append(row)
        self.cols.append(col)
        self.data.append(value)

    def bound(self, low: float, high: float) -> None:
        self.lb.append(low)
        self.ub.append(high)


def _cover_rows(entries: list[_FlowEntry], builder: _RowBuilder) -> int:
    """Append the block-diagonal exact-cover rows; return the row count."""
    row = 0
    for entry in entries:
        cover = _cover_matrix(entry.component, entry.rects).tocoo()
        for r, c, v in zip(cover.row, cover.col, cover.data, strict=True):
            builder.entry(row + int(r), entry.offset + int(c), float(v))
        block_rows = cover.shape[0]
        for _ in range(block_rows):
            builder.bound(1.0, 1.0)
        row += block_rows
    return row


def _flow_constraints(
    entries: list[_FlowEntry],
    graph: _FlowGraph,
    *,
    n_rects: int,
    n_terminals: int,
    capacity: float,
) -> LinearConstraint:
    """Sparse joint system: block covers + conservation + gating.

    Variables are ``[x (rects)] [s (join slacks)] [o (orphan slacks)]
    [f (arc flows)]``. Every node carries conservation except the root —
    the root is the sole net emitter, so any chosen rect's required unit
    of inflow must trace back to it; circulations cannot fabricate
    supply. ``o_r`` waives rect r's inflow demand (``o_r <= x_r`` keeps
    unchosen rects from becoming fake sources): rings whose absolute-slab
    re-tiling CANNOT fully stud-connect (the mushroom cap ring's
    re-phasing limitation) would otherwise be infeasible outright —
    orphans let the solve return its best joins instead of nothing.
    """
    builder = _RowBuilder()
    o_col = n_rects + n_terminals
    f_col = o_col + n_rects
    row = _cover_rows(entries, builder)

    # Incidence per node.
    inflow: dict[int, list[int]] = {}
    outflow: dict[int, list[int]] = {}
    for arc_index, (tail, head) in enumerate(graph.arcs):
        outflow.setdefault(tail, []).append(arc_index)
        inflow.setdefault(head, []).append(arc_index)

    def net_flow(node: int, target_row: int) -> None:
        for arc_index in inflow.get(node, ()):
            builder.entry(target_row, f_col + arc_index, 1.0)
        for arc_index in outflow.get(node, ()):
            builder.entry(target_row, f_col + arc_index, -1.0)

    terminal_index = {node: t for t, node in enumerate(graph.terminals)}
    for node_key, node in graph.node_ids.items():
        if node == graph.root:
            continue
        match node_key:
            case ("r", int(rect_index)):
                # Conservation: inflow - outflow = x - o.
                net_flow(node, row)
                builder.entry(row, rect_index, -1.0)
                builder.entry(row, o_col + rect_index, 1.0)
                builder.bound(0.0, 0.0)
                row += 1
                # Gating: inflow <= capacity * x.
                for arc_index in inflow.get(node, ()):
                    builder.entry(row, f_col + arc_index, 1.0)
                builder.entry(row, rect_index, -capacity)
                builder.bound(-np.inf, 0.0)
                row += 1
                # Orphans only on chosen rects: o <= x.
                builder.entry(row, o_col + rect_index, 1.0)
                builder.entry(row, rect_index, -1.0)
                builder.bound(-np.inf, 0.0)
                row += 1
            case _ if node in terminal_index:
                # Join demand: inflow - outflow + s = 1.
                net_flow(node, row)
                builder.entry(row, n_rects + terminal_index[node], 1.0)
                builder.bound(1.0, 1.0)
                row += 1
            case _:  # hubs, the ground, non-terminal components: transship.
                net_flow(node, row)
                builder.bound(0.0, 0.0)
                row += 1

    n_vars = 2 * n_rects + n_terminals + len(graph.arcs)
    matrix = coo_matrix(
        (builder.data, (builder.rows, builder.cols)), shape=(row, n_vars)
    )
    return LinearConstraint(matrix, np.array(builder.lb), np.array(builder.ub))


@dataclass(slots=True)
class BridgeSynthesizer:
    """Exact-cover MILP re-tiling of a connectivity-repair ring.

    Callable as ``synthesizer(layout, region, grid) -> Layout | None``:
    a re-tiled copy whose stud-graph component count strictly dropped,
    or None (ring not carvable, candidates blown up, solver failure or
    timeout, or no bridge achievable) — the caller falls back to the
    random rewrite. Deterministic: no rng, rank-epsilon tiebreaks.
    """

    catalog: Catalog = field(default_factory=default_catalog)
    slab_time_s: float = 2.0
    total_time_s: float = 10.0
    candidate_limit: int = 20_000
    bond_weight: float = 1.0
    bridge_bonus: float = 10.0
    flow_time_s: float = 8.0
    """Per-stage cap for the cross-slab flow MILP (within total_time_s)."""
    flow_candidate_limit: int = 600
    flow_arc_limit: int = 8_000
    """Measured tractability envelope, not throughput guards: the joint
    lexicographic MILP proves optimality in under a second at ladder
    scale (54 rects / 648 arcs) but cannot close the gap within any
    reasonable budget at shell scale (thin-shell's ring: 1_364 rects /
    15_752 arcs — 60 s was not enough, and flat weight rescaling did
    not help). Oversized rings decline in milliseconds instead of
    burning flow_time_s on a doomed solve."""
    flow_escalate: bool = True
    """Escalate to the cross-slab flow MILP when the per-slab pass
    declines; False restores the pure per-slab synthesizer."""
    placement_deadline: float | None = None
    """Absolute outer placement deadline shared by every bridge attempt."""
    rephase: bool = False
    """Try phase 0 only when false; phases 0, 1, and 2 when true."""

    def __call__(
        self,
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
    ) -> Layout | None:
        """Re-tile ``region``'s carvable bricks; None on any failure."""
        now = time.monotonic()
        deadline = now + self.total_time_s
        if self.placement_deadline is not None:
            deadline = min(deadline, self.placement_deadline)
        if now >= deadline:
            return None
        before = ConnectionGraph.from_layout(layout).component_count()
        phases = tuple(range(3) if self.rephase else range(1))
        phase_candidates, phase_keys = self._initial_phase_candidates(
            layout,
            region,
            grid,
            deadline,
            before,
            phases,
        )
        self._add_flow_candidates(
            layout,
            region,
            grid,
            deadline,
            before,
            phase_candidates,
            phase_keys,
        )
        return self._choose_phase_candidate(phase_candidates, phases)

    def _initial_phase_candidates(  # noqa: PLR0913 - shared phase state
        self,
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
        deadline: float,
        before: int,
        phases: tuple[int, ...],
    ) -> tuple[dict[int, list[Layout]], dict[int, tuple[int, int, int]]]:
        """Run cheap per-slab covers for every phase before any flow MILP."""
        phase_candidates: dict[int, list[Layout]] = {}
        phase_keys: dict[int, tuple[int, int, int]] = {}
        for phase in phases:
            if time.monotonic() >= deadline:
                break
            telemetry.value("connectivity.bridge.phase_attempted", float(phase))
            candidate = self._per_slab_candidate(
                layout,
                region,
                grid,
                deadline,
                before,
                phase=phase,
            )
            phase_candidates[phase] = [candidate] if candidate is not None else []
            components = (
                ConnectionGraph.from_layout(candidate).component_count()
                if candidate is not None
                else before
            )
            phase_keys[phase] = (
                components,
                len(candidate) if candidate is not None else 1 << 60,
                phase,
            )
        return phase_candidates, phase_keys

    def _add_flow_candidates(  # noqa: PLR0913 - shared phase state
        self,
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
        deadline: float,
        before: int,
        phase_candidates: dict[int, list[Layout]],
        phase_keys: dict[int, tuple[int, int, int]],
    ) -> None:
        """Escalate promising partial phases first under one deadline."""
        # Spend the joint-flow budget on the most promising re-phasing
        # first. Phase 0's large shell model otherwise consumes the shared
        # deadline before the 3-component phase-1 cover gets a chance.
        for phase in sorted(phase_keys, key=phase_keys.__getitem__):
            if (
                not self.flow_escalate
                or phase_keys[phase][0] <= 1
                or time.monotonic() >= deadline
            ):
                continue
            with telemetry.span("connectivity.bridge_flow"):
                flow_candidate = self._flow_candidate(
                    layout,
                    region,
                    grid,
                    deadline,
                    before,
                    phase=phase,
                )
            if flow_candidate is not None:
                phase_candidates[phase].append(flow_candidate)

    def _choose_phase_candidate(
        self,
        phase_candidates: dict[int, list[Layout]],
        phases: tuple[int, ...],
    ) -> Layout | None:
        """Choose components, then count, then phase; emit phase evidence."""
        best: Layout | None = None
        best_key: tuple[int, int, int] | None = None
        for phase in phases:
            candidates = phase_candidates.get(phase, [])
            candidate = min(
                candidates,
                key=lambda item: (
                    ConnectionGraph.from_layout(item).component_count(),
                    len(item),
                ),
                default=None,
            )
            if phase in phase_candidates:
                telemetry.value(
                    "connectivity.bridge.phase_solved",
                    float(candidate is not None),
                )
            if candidate is None:
                continue
            components = ConnectionGraph.from_layout(candidate).component_count()
            key = (components, len(candidate), phase)
            telemetry.value("connectivity.bridge.phase_components", float(components))
            telemetry.value("connectivity.bridge.phase_bricks", float(len(candidate)))
            if best_key is None or key < best_key:
                best, best_key = candidate, key
        if best_key is not None:
            telemetry.value("connectivity.bridge.phase_accepted", float(best_key[2]))
        return best

    def _carve(
        self,
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
    ) -> tuple[Layout, dict[Cell, int]] | None:
        """Copy the layout with the ring's mergeable bricks removed."""
        candidate = layout.copy()
        cells: dict[Cell, int] = {}
        for brick_id in sorted(region):
            brick = candidate.bricks.get(brick_id)
            if brick is None or candidate.part_of(brick).category not in _MERGEABLE:
                continue
            for cell in candidate.cells_of(brick):
                cells[cell] = _cell_code(grid, cell, brick.colour_code)
            candidate.remove(brick_id)
        if not cells:
            return None
        return candidate, cells

    def _per_slab_candidate(  # noqa: PLR0913 - one phase attempt is six facts
        self,
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
        deadline: float,
        before: int,
        *,
        phase: int = 0,
    ) -> Layout | None:
        """Run the original slab-at-a-time pass (optimal on clean seams)."""
        if (carved := self._carve(layout, region, grid)) is None:
            return None
        candidate, cells = carved
        for problem in slab_problems(cells, phase=phase):
            context = build_context(candidate, problem)
            labels = ConnectionGraph.from_layout(candidate).brick_components()
            chosen: list[Rect2D] = []
            for component in _components(problem.columns):
                rects = self._solve_component(
                    candidate, problem, context, labels, component, deadline
                )
                if rects is None:
                    return None
                chosen.extend(rects)
            realize(candidate, problem, chosen)
        compact_vertical(candidate)
        after = ConnectionGraph.from_layout(candidate).component_count()
        return candidate if after < before else None

    def _mates_of(
        self,
        candidate: Layout,
        context: LayerContext,
        labels: dict[int, int],
        column: tuple[int, int],
        top: int,
    ) -> tuple[int | None, int | None]:
        """(below, above) mates of a slab column, as component labels.

        The two verified guards, shared by the per-slab bridging row and
        the flow graph's component arcs: below contacts come through
        ``support_of`` (the slab's cells were free above their supports,
        so a stud mate is real) with ``GROUND_ID`` passed through; above
        contacts require the neighbour's *bottom* face exactly on this
        slab's top plane — that guard is what lets the mushroom
        stem-below/cap-above sandwich count as a bridge.
        """
        below: int | None = None
        support = context.support_of.get(column)
        if support == GROUND_ID:
            below = GROUND_ID
        elif support is not None:
            below = labels.get(support)
        x, y = column
        above_brick = candidate.brick_at((x, y, top))
        above: int | None = None
        if above_brick is not None and above_brick.layer == top:
            above = labels.get(above_brick.brick_id)
        return below, above

    def _touch_count(
        self,
        candidate: Layout,
        problem: LayerProblem,
        context: LayerContext,
        labels: dict[int, int],
        rect: Rect2D,
    ) -> int:
        """Distinct stud-graph components this rect would mate with."""
        top = problem.layer + problem.height_plates
        touched: set[int] = set()
        for column in rect.columns():
            below, above = self._mates_of(candidate, context, labels, column, top)
            if below is not None and below != GROUND_ID:
                touched.add(below)
            if above is not None:
                touched.add(above)
        return len(touched)

    def _budget(self, deadline: float, *, spent: float = 0.0) -> float | None:
        """Remaining per-solve budget, or None when exhausted."""
        budget = min(self.slab_time_s - spent, deadline - time.monotonic())
        if budget < _MIN_SOLVE_S:
            return None
        return budget

    def _solve_component(  # noqa: PLR0913 - one slab component is six facts
        self,
        candidate: Layout,
        problem: LayerProblem,
        context: LayerContext,
        labels: dict[int, int],
        component: list[tuple[int, int]],
        deadline: float,
    ) -> list[Rect2D] | None:
        """Two-stage lexicographic MILP with a hard bridging floor."""
        if self._budget(deadline) is None:
            return None  # budget already spent: don't pay for enumeration
        try:
            rects = enumerate_layer_rects(
                problem,
                component,
                self.catalog,
                candidate_limit=self.candidate_limit,
                deadline=deadline,
            )
        except TimeoutError:
            return None
        if not rects or len(rects) > self.candidate_limit:
            return None
        # Recheck: enumeration itself may have consumed the remainder
        # (PR #18 review — same flaw as kollsker's, fixed the same way).
        if (stage1_limit := self._budget(deadline)) is None:
            return None
        started = time.monotonic()
        cover = _cover_matrix(component, rects)
        ones = np.ones(len(rects))
        touches = np.array(
            [
                self._touch_count(candidate, problem, context, labels, rect)
                for rect in rects
            ]
        )
        constraints = [LinearConstraint(cover, lb=1.0, ub=1.0)]
        bridging = touches >= 2  # two components make a bridge
        if bridging.any():
            constraints.append(
                LinearConstraint(
                    bridging.astype(float).reshape(1, -1),
                    lb=1.0,
                    ub=float(bridging.sum()),
                )
            )
        stage1 = _guarded_milp(
            c=ones,
            constraints=constraints,
            integrality=ones,
            bounds=Bounds(0, 1),
            options={"time_limit": stage1_limit},
        )
        if stage1 is None or not stage1.success or stage1.x is None:
            return None
        n_star = float(np.round(stage1.fun))
        rewards = self.bridge_bonus * np.maximum(touches - 1, 0) + np.array(
            [self.bond_weight * bond_reward(rect, context) for rect in rects]
        )
        rank = _RANK_EPS * np.arange(len(rects))
        stage2_limit = self._budget(deadline, spent=time.monotonic() - started)
        stage2 = (
            None
            if stage2_limit is None
            else _guarded_milp(
                c=-rewards + rank,
                constraints=[
                    *constraints,
                    LinearConstraint(np.ones((1, len(rects))), n_star, n_star),
                ],
                integrality=ones,
                bounds=Bounds(0, 1),
                options={"time_limit": stage2_limit},
            )
        )
        chosen = (
            stage2
            if stage2 is not None and stage2.success and stage2.x is not None
            else stage1
        )
        return [
            rect for value, rect in zip(chosen.x, rects, strict=True) if value > 0.5
        ]

    # --- cross-slab flow escalation ------------------------------------

    def _flow_candidate(  # noqa: PLR0913 - one phase attempt is six facts
        self,
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
        deadline: float,
        before: int,
        *,
        phase: int = 0,
    ) -> Layout | None:
        """One MILP over every slab problem jointly; None on any breach."""
        if (carved := self._carve(layout, region, grid)) is None:
            return None
        candidate, cells = carved
        labels = ConnectionGraph.from_layout(candidate).brick_components()
        entries = self._gather_entries(candidate, cells, deadline, phase=phase)
        if entries is None:
            return None
        graph = self._flow_graph(candidate, entries, labels)
        if graph is None:
            return None
        chosen = self._solve_flow(candidate, entries, labels, graph, deadline)
        if chosen is None:
            return None
        for problem, rects in chosen:
            realize(candidate, problem, rects)
        compact_vertical(candidate)
        after = ConnectionGraph.from_layout(candidate).component_count()
        return candidate if after < before else None

    def _gather_entries(
        self,
        candidate: Layout,
        cells: dict[Cell, int],
        deadline: float,
        *,
        phase: int = 0,
    ) -> list[_FlowEntry] | None:
        """Build the deterministic global candidate list.

        One entry per slab component; None past the candidate limit or
        the deadline.
        """
        entries: list[_FlowEntry] = []
        offset = 0
        for problem in slab_problems(cells, phase=phase):
            context = build_context(candidate, problem)
            for component in _components(problem.columns):
                if self._flow_budget(deadline) is None:
                    return None
                try:
                    rects = enumerate_layer_rects(
                        problem,
                        component,
                        self.catalog,
                        candidate_limit=self.flow_candidate_limit - offset,
                        deadline=deadline,
                    )
                except TimeoutError:
                    return None
                if not rects:
                    return None
                entries.append(_FlowEntry(problem, context, component, rects, offset))
                offset += len(rects)
                if offset > self.flow_candidate_limit:
                    telemetry.value(
                        "connectivity.bridge.candidates",
                        float(offset),
                    )
                    return None
        telemetry.value("connectivity.bridge.candidates", float(offset))
        return entries

    def _flow_links(
        self,
        candidate: Layout,
        entries: list[_FlowEntry],
        labels: dict[int, int],
    ) -> list[tuple[_Node, _Node]]:
        """Collect every structural adjacency as a bidirectional arc pair."""
        links: list[tuple[_Node, _Node]] = []

        def link(a: _Node, b: _Node) -> None:
            links.append((a, b))
            links.append((b, a))

        for entry in entries:
            bottom = entry.problem.layer
            top = bottom + entry.problem.height_plates
            for index, rect in enumerate(entry.rects, start=entry.offset):
                for column in rect.columns():
                    link(("r", index), ("h", (column, bottom)))
                    link(("r", index), ("h", (column, top)))
            for column in entry.component:
                below, above = self._mates_of(
                    candidate, entry.context, labels, column, top
                )
                if below == GROUND_ID:
                    link(("g", 0), ("h", (column, bottom)))
                elif below is not None:
                    link(("c", below), ("h", (column, bottom)))
                if above is not None:
                    link(("c", above), ("h", (column, top)))
        return links

    def _flow_graph(
        self,
        candidate: Layout,
        entries: list[_FlowEntry],
        labels: dict[int, int],
    ) -> _FlowGraph | None:
        """Hub-aggregated arcs; None past the arc limit.

        Hubs are keyed by (column, mating plane) so slab problems talk
        through the plane where faces actually meet — plate-brick
        interleaving falls out of the keying, never "z±3" guesses.
        """
        links = self._flow_links(candidate, entries, labels)
        telemetry.value("connectivity.bridge.arcs", float(len(links)))
        if len(links) > self.flow_arc_limit:
            return None
        counts: dict[int, int] = {}
        for label in labels.values():
            counts[label] = counts.get(label, 0) + 1
        if counts:
            # Largest remaining component, smallest label on ties.
            _count, neg_label = max((count, -label) for label, count in counts.items())
            root: _Node = ("c", -neg_label)
        else:
            root = ("g", 0)
        node_ids: dict[_Node, int] = {}
        for a, b in links:
            for node in (a, b):
                if node not in node_ids:
                    node_ids[node] = len(node_ids)
        if root not in node_ids:
            node_ids[root] = len(node_ids)
        arcs = [(node_ids[a], node_ids[b]) for a, b in links]
        terminals = sorted(
            node_ids[("c", label)]
            for label in counts
            if ("c", label) != root and ("c", label) in node_ids
        )
        return _FlowGraph(
            arcs=arcs,
            node_ids=node_ids,
            root=node_ids[root],
            terminals=terminals,
        )

    def _flow_budget(self, deadline: float) -> float | None:
        """Remaining flow-stage budget, or None when exhausted."""
        budget = min(self.flow_time_s, deadline - time.monotonic())
        if budget < _MIN_SOLVE_S:
            return None
        return budget

    def _solve_flow(
        self,
        candidate: Layout,
        entries: list[_FlowEntry],
        labels: dict[int, int],
        graph: _FlowGraph,
        deadline: float,
    ) -> list[tuple[LayerProblem, list[Rect2D]]] | None:
        """Two-stage joint MILP; None on failure, timeout, or no joins."""
        n_rects = entries[-1].offset + len(entries[-1].rects)
        n_terminals = len(graph.terminals)
        n_arcs = len(graph.arcs)
        n_binary = 2 * n_rects + n_terminals  # x, s, o
        capacity = float(n_rects + n_terminals + 1)
        constraints = _flow_constraints(
            entries,
            graph,
            n_rects=n_rects,
            n_terminals=n_terminals,
            capacity=capacity,
        )
        integrality = np.concatenate([np.ones(n_binary), np.zeros(n_arcs)])
        bounds = Bounds(
            np.zeros(n_binary + n_arcs),
            np.concatenate([np.ones(n_binary), np.full(n_arcs, capacity)]),
        )
        if (stage1_limit := self._flow_budget(deadline)) is None:
            return None
        # Lexicographic: joins dominate orphans dominate brick count.
        orphan_weight = float(n_rects + 1)
        join_weight = orphan_weight * float(n_rects + 2)
        stage1_cost = np.concatenate(
            [
                np.ones(n_rects),
                np.full(n_terminals, join_weight),
                np.full(n_rects, orphan_weight),
                np.zeros(n_arcs),
            ]
        )
        stage1 = _guarded_milp(
            c=stage1_cost,
            constraints=constraints,
            integrality=integrality,
            bounds=bounds,
            options={"time_limit": stage1_limit},
        )
        if stage1 is None or not stage1.success or stage1.x is None:
            return None
        n_star = float(np.round(stage1.x[:n_rects].sum()))
        s_star = float(np.round(stage1.x[n_rects : n_rects + n_terminals].sum()))
        o_star = float(np.round(stage1.x[n_rects + n_terminals : n_binary].sum()))
        stage2_x = self._flow_stage2(
            candidate,
            entries,
            labels,
            deadline,
            constraints=constraints,
            integrality=integrality,
            bounds=bounds,
            pins=(n_star, s_star, o_star),
            sizes=(n_rects, n_terminals, n_arcs),
        )
        chosen_x = stage2_x if stage2_x is not None else stage1.x
        grouped: dict[int, tuple[LayerProblem, list[Rect2D]]] = {}
        for order, entry in enumerate(entries):
            problem_key = grouped.setdefault(order, (entry.problem, []))
            for index, rect in enumerate(entry.rects, start=entry.offset):
                if chosen_x[index] > 0.5:
                    problem_key[1].append(rect)
        merged: dict[int, tuple[LayerProblem, list[Rect2D]]] = {}
        for _order, (problem, rects) in sorted(grouped.items()):
            key = id(problem)
            if key in merged:
                merged[key][1].extend(rects)
            else:
                merged[key] = (problem, rects)
        return list(merged.values())

    def _flow_stage2(  # noqa: PLR0913 - one pinned re-solve is nine facts
        self,
        candidate: Layout,
        entries: list[_FlowEntry],
        labels: dict[int, int],
        deadline: float,
        *,
        constraints: LinearConstraint,
        integrality: np.ndarray,
        bounds: Bounds,
        pins: tuple[float, float, float],
        sizes: tuple[int, int, int],
    ) -> np.ndarray | None:
        """Stage 2: pin the lexicographic optima, maximize rect rewards."""
        if (stage2_limit := self._flow_budget(deadline)) is None:
            return None
        n_rects, n_terminals, n_arcs = sizes
        n_binary = 2 * n_rects + n_terminals
        n_star, s_star, o_star = pins
        rewards = np.concatenate(
            [
                self._flow_rewards(candidate, entries, labels),
                np.zeros(n_rects + n_terminals + n_arcs),
            ]
        )
        rank = np.concatenate(
            [
                _RANK_EPS * np.arange(n_rects),
                np.zeros(n_rects + n_terminals + n_arcs),
            ]
        )

        def pin(lo: int, hi: int, value: float) -> LinearConstraint:
            selector = np.zeros((1, n_binary + n_arcs))
            selector[0, lo:hi] = 1.0
            return LinearConstraint(selector, value, value)

        stage2 = _guarded_milp(
            c=-rewards + rank,
            constraints=[
                constraints,
                pin(0, n_rects, n_star),
                pin(n_rects, n_rects + n_terminals, s_star),
                pin(n_rects + n_terminals, n_binary, o_star),
            ],
            integrality=integrality,
            bounds=bounds,
            options={"time_limit": stage2_limit},
        )
        if stage2 is not None and stage2.success and stage2.x is not None:
            return stage2.x
        return None

    def _flow_rewards(
        self,
        candidate: Layout,
        entries: list[_FlowEntry],
        labels: dict[int, int],
    ) -> np.ndarray:
        """Stage-2 per-rect rewards: extra crossings + bond quality."""
        rewards: list[float] = []
        for entry in entries:
            for rect in entry.rects:
                touches = self._touch_count(
                    candidate, entry.problem, entry.context, labels, rect
                )
                rewards.append(
                    self.bridge_bonus * max(touches - 1, 0)
                    + self.bond_weight * bond_reward(rect, entry.context)
                )
        return np.array(rewards)
