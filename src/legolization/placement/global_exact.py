"""Bounded whole-model exact placement with rooted connectivity flow."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, OptimizeResult, milp
from scipy.sparse import coo_matrix

from legolization.catalog import Category, rotate_offset
from legolization.errors import ExactPlacementLimitError, PlacementInfeasibleError
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.grid import IGNORE, merge_colour
from legolization.layout import Layout
from legolization.physical import LduBox, LduConnector, boxes_intersect
from legolization.stability.solver import analyze

if TYPE_CHECKING:
    from collections.abc import Iterator

    from legolization.catalog import Catalog, Cell, Part
    from legolization.grid import VoxelGrid
    from legolization.pipeline import PipelineConfig
    from legolization.stability.solver import SolverConfig, StabilityResult

type ExactStatus = Literal["optimal", "fallback", "limit", "infeasible"]

_OBJECTIVE_SCALE = 1_000_000.0
_BOND_REWARD = 1.0
_RANK_EPSILON = 1.0
_MILP_TOLERANCE = 0.5
_EXACT_CATEGORIES = frozenset(
    (
        Category.BRICK,
        Category.PLATE,
        Category.TILE,
        Category.SLOPE,
        Category.SNOT,
        Category.SPECIAL_SNOT,
    )
)


@dataclass(frozen=True, slots=True)
class ExactOutcome:
    """Exactness evidence emitted by a global strategy run."""

    status: ExactStatus
    candidate_count: int
    unstable_cuts: int
    message: str = ""


@dataclass(frozen=True, slots=True)
class _Candidate:
    part_key: str
    x: int
    y: int
    layer: int
    yaw: int
    colour_code: int
    filled: frozenset[Cell]
    occupied: frozenset[Cell]
    collision_boxes: tuple[LduBox, ...]
    top: tuple[LduConnector, ...]
    bottom: tuple[LduConnector, ...]
    mass_g: float

    @property
    def signature(self) -> tuple[str, int, int, int, int, int]:
        return (
            self.part_key,
            self.x,
            self.y,
            self.layer,
            self.yaw,
            self.colour_code,
        )


@dataclass(slots=True)
class _RowBuilder:
    rows: list[int] = field(default_factory=list)
    cols: list[int] = field(default_factory=list)
    data: list[float] = field(default_factory=list)
    lower: list[float] = field(default_factory=list)
    upper: list[float] = field(default_factory=list)

    def add(self, entries: dict[int, float], *, lower: float, upper: float) -> None:
        row = len(self.lower)
        for column, value in entries.items():
            if value:
                self.rows.append(row)
                self.cols.append(column)
                self.data.append(value)
        self.lower.append(lower)
        self.upper.append(upper)

    def constraint(self, variable_count: int) -> LinearConstraint:
        matrix = coo_matrix(
            (self.data, (self.rows, self.cols)),
            shape=(len(self.lower), variable_count),
        ).tocsc()
        return LinearConstraint(matrix, np.asarray(self.lower), np.asarray(self.upper))


@dataclass(frozen=True, slots=True)
class _ProblemShape:
    """Variable offsets and graph data for one exact-cover MILP."""

    candidate_count: int
    pair_items: tuple[tuple[tuple[int, int], int], ...]
    directed: tuple[tuple[int, int, int], ...]
    grounded: tuple[int, ...]

    @property
    def pair_offset(self) -> int:
        return self.candidate_count

    @property
    def flow_offset(self) -> int:
        return self.pair_offset + len(self.pair_items)

    @property
    def root_offset(self) -> int:
        return self.flow_offset + len(self.directed)

    @property
    def variable_count(self) -> int:
        return self.root_offset + len(self.grounded)


@dataclass(slots=True)
class GlobalExactStrategy:
    """Solve a bounded voxel model exactly, then cold-certify stability."""

    catalog: Catalog
    solver_config: SolverConfig
    config: PipelineConfig
    outcome: ExactOutcome = field(
        default_factory=lambda: ExactOutcome(
            status="limit",
            candidate_count=0,
            unstable_cuts=0,
            message="not run",
        ),
        init=False,
    )

    def place(
        self,
        grid: VoxelGrid,
        *,
        rng: np.random.Generator,
        deadline: float | None = None,
    ) -> Layout:
        """Return a globally optimal stable layout within configured limits."""
        del rng  # exact enumeration and MILP ordering are deterministic
        exact_deadline = _exact_deadline(self.config, deadline)
        if (limited := self._preflight_limit(grid, deadline=deadline)) is not None:
            return limited
        candidates = _enumerate_candidates(
            grid,
            self.catalog,
            limit=(
                None
                if self.config.exact_limit_policy == "continue"
                else self.config.exact_max_candidates
            ),
            deadline=exact_deadline,
        )
        if candidates is None:
            return self._on_limit(
                grid,
                deadline=deadline,
                preflight=False,
                message=(
                    "exact candidate enumeration exceeded "
                    f"{self.config.exact_max_candidates} rows"
                ),
            )
        if not candidates:
            msg = "no candidate covers the target"
            raise PlacementInfeasibleError(msg)
        no_goods: list[frozenset[int]] = []
        while True:
            result = _solve(
                grid,
                candidates,
                no_goods=no_goods,
                objective=self.config.cost_objective,
                deadline=exact_deadline,
            )
            if result.status == 2:
                self.outcome = ExactOutcome(
                    status="infeasible",
                    candidate_count=len(candidates),
                    unstable_cuts=len(no_goods),
                    message="no connected stable exact cover exists",
                )
                raise PlacementInfeasibleError(self.outcome.message)
            if result.x is None or result.status != 0:
                return self._on_limit(
                    grid,
                    deadline=deadline,
                    preflight=False,
                    message="global exact solve reached its time or solver limit",
                    candidate_count=len(candidates),
                    unstable_cuts=len(no_goods),
                )
            selected = frozenset(
                index
                for index, value in enumerate(result.x[: len(candidates)])
                if value >= _MILP_TOLERANCE
            )
            layout = _layout(self.catalog, candidates, selected)
            stability = analyze(layout, self.solver_config)
            if stability.stable:
                self.outcome = ExactOutcome(
                    status="optimal",
                    candidate_count=len(candidates),
                    unstable_cuts=len(no_goods),
                )
                return layout
            no_goods.append(
                _weak_region_cut(
                    layout,
                    candidates=candidates,
                    selected=selected,
                    stability=stability,
                )
            )
            if len(no_goods) >= self.config.exact_max_stability_cuts:
                return self._on_limit(
                    grid,
                    deadline=deadline,
                    preflight=False,
                    message=(
                        "stability refinement reached its cut cap "
                        f"({self.config.exact_max_stability_cuts})"
                    ),
                    candidate_count=len(candidates),
                    unstable_cuts=len(no_goods),
                )
            if exact_deadline is not None and time.monotonic() >= exact_deadline:
                return self._on_limit(
                    grid,
                    deadline=deadline,
                    preflight=False,
                    message="stability certification exhausted the exact deadline",
                    candidate_count=len(candidates),
                    unstable_cuts=len(no_goods),
                )

    def _preflight_limit(
        self,
        grid: VoxelGrid,
        *,
        deadline: float | None,
    ) -> Layout | None:
        if (
            grid.filled_count > self.config.exact_max_cells
            and self.config.exact_limit_policy != "continue"
        ):
            return self._on_limit(
                grid,
                deadline=deadline,
                preflight=True,
                message=(
                    f"target has {grid.filled_count} cells; exact cap is "
                    f"{self.config.exact_max_cells}"
                ),
            )
        return None

    def _on_limit(  # noqa: PLR0913 - outcome evidence accompanies policy inputs
        self,
        grid: VoxelGrid,
        *,
        deadline: float | None,
        preflight: bool,
        message: str,
        candidate_count: int = 0,
        unstable_cuts: int = 0,
    ) -> Layout:
        policy = self.config.exact_limit_policy
        if self.config.exact_auto_preflight_fallback and preflight:
            policy = "fallback"
        if policy == "fallback":
            from legolization.placement.registry import make_strategy  # noqa: PLC0415

            fallback = make_strategy(
                self.config.exact_fallback_strategy,
                catalog=self.catalog,
                config=self.config,
            )
            self.outcome = ExactOutcome(
                status="fallback",
                candidate_count=candidate_count,
                unstable_cuts=unstable_cuts,
                message=message,
            )
            return fallback.place(
                grid=grid,
                rng=np.random.default_rng(self.config.seed),
                deadline=deadline,
            )
        self.outcome = ExactOutcome(
            status="limit",
            candidate_count=candidate_count,
            unstable_cuts=unstable_cuts,
            message=message,
        )
        raise ExactPlacementLimitError(message)


def _enumerate_candidates(
    grid: VoxelGrid,
    catalog: Catalog,
    *,
    limit: int | None,
    deadline: float | None,
) -> list[_Candidate] | None:
    target = _target_cells(grid)
    signatures: dict[tuple[str, int, int, int, int, int], _Candidate] = {}
    for part in _eligible_parts(catalog):
        for candidate in _part_candidates(grid, target, part):
            if deadline is not None and time.monotonic() >= deadline:
                return None
            signatures.setdefault(candidate.signature, candidate)
            if limit is not None and len(signatures) > limit:
                return None
    return sorted(signatures.values(), key=lambda item: item.signature)


def _target_cells(grid: VoxelGrid) -> frozenset[Cell]:
    return frozenset(
        cast("Cell", tuple(int(value) for value in row))
        for row in np.argwhere(grid.filled_mask)
    )


def _eligible_parts(catalog: Catalog) -> tuple[Part, ...]:
    return tuple(
        sorted(
            (
                part
                for part in catalog.parts.values()
                if part.category in _EXACT_CATEGORIES and part.mount_normal is None
            ),
            key=lambda item: item.key,
        )
    )


def _part_candidates(
    grid: VoxelGrid,
    target: frozenset[Cell],
    part: Part,
) -> Iterator[_Candidate]:
    for yaw in part.orientations:
        rotated = tuple(rotate_offset(cell, yaw) for cell in part.filled_cells)
        seen_anchors: set[Cell] = set()
        for target_cell in sorted(target):
            for local in rotated:
                anchor = cast(
                    "Cell",
                    tuple(target_cell[axis] - local[axis] for axis in range(3)),
                )
                if anchor in seen_anchors:
                    continue
                seen_anchors.add(anchor)
                candidate = _candidate(grid, target, part, anchor=anchor, yaw=yaw)
                if candidate is not None and _candidate_allowed(grid, part, candidate):
                    yield candidate


def _candidate_allowed(
    grid: VoxelGrid,
    part: Part,
    candidate: _Candidate,
) -> bool:
    return part.category is not Category.SPECIAL_SNOT or _is_detail_candidate(
        grid, candidate
    )


def _is_detail_candidate(grid: VoxelGrid, candidate: _Candidate) -> bool:
    """Reserve compound side-detail carriers for annotated mesh regions."""
    features = grid.mesh_features
    return features is not None and bool(
        candidate.filled & frozenset(features.detail_candidates)
    )


def _candidate(
    grid: VoxelGrid,
    target: frozenset[Cell],
    part: Part,
    *,
    anchor: Cell,
    yaw: int,
) -> _Candidate | None:
    x, y, layer = anchor
    filled = frozenset(part.filled_at(x, y, layer, yaw))
    occupied = frozenset(part.cells_at(x, y, layer, yaw))
    if not filled <= target or any(cell[2] < 0 for cell in occupied):
        return None
    colour = merge_colour(*(grid.code_at(*cell) for cell in filled))
    if colour is None:
        return None
    if colour == IGNORE:
        colour = 7
    return _Candidate(
        part_key=part.key,
        x=x,
        y=y,
        layer=layer,
        yaw=yaw,
        colour_code=colour,
        filled=filled,
        occupied=occupied,
        collision_boxes=part.collision_boxes_at(x, y, layer, yaw),
        top=part.physical_connectors_at(anchor, yaw, top=True),
        bottom=part.physical_connectors_at(anchor, yaw, top=False),
        mass_g=part.mass_g,
    )


def _solve(
    grid: VoxelGrid,
    candidates: list[_Candidate],
    *,
    no_goods: list[frozenset[int]],
    objective: Literal["bricks", "mass"],
    deadline: float | None,
) -> OptimizeResult:
    target = tuple(sorted(_target_cells(grid)))
    shape = _problem_shape(candidates)
    builder = _RowBuilder()
    _add_cover_constraints(builder, target=target, candidates=candidates)
    _add_connectivity_constraints(builder, shape=shape)
    for selected in no_goods:
        builder.add(
            dict.fromkeys(selected, 1.0),
            lower=-np.inf,
            upper=float(len(selected) - 1),
        )
    return _run_milp(
        candidates,
        shape=shape,
        builder=builder,
        objective=objective,
        deadline=deadline,
    )


def _problem_shape(candidates: list[_Candidate]) -> _ProblemShape:
    pair_items = tuple(sorted(_connection_pairs(candidates).items()))
    directed = tuple(
        (left, right, pair_index)
        for pair_index, ((left, right), _) in enumerate(pair_items)
        for left, right in ((left, right), (right, left))
    )
    grounded = tuple(
        index
        for index, candidate in enumerate(candidates)
        if any(
            connector.direction == (0, 0, -1) and connector.point[2] == 0
            for connector in candidate.bottom
        )
    )
    return _ProblemShape(
        candidate_count=len(candidates),
        pair_items=pair_items,
        directed=directed,
        grounded=grounded,
    )


def _add_cover_constraints(
    builder: _RowBuilder,
    *,
    target: tuple[Cell, ...],
    candidates: list[_Candidate],
) -> None:
    by_filled = _cell_index(candidates)
    for cell in target:
        builder.add(
            dict.fromkeys(by_filled.get(cell, ()), 1.0),
            lower=1.0,
            upper=1.0,
        )
    for left, right in _collision_pairs(candidates):
        builder.add({left: 1.0, right: 1.0}, lower=-np.inf, upper=1.0)


def _add_connectivity_constraints(
    builder: _RowBuilder,
    *,
    shape: _ProblemShape,
) -> None:
    for pair_index, ((left, right), _) in enumerate(shape.pair_items):
        edge = shape.pair_offset + pair_index
        builder.add({edge: 1.0, left: -1.0}, lower=-np.inf, upper=0.0)
        builder.add({edge: 1.0, right: -1.0}, lower=-np.inf, upper=0.0)
        builder.add({left: 1.0, right: 1.0, edge: -1.0}, lower=-np.inf, upper=1.0)
    capacity = float(shape.candidate_count)
    incoming: dict[int, dict[int, float]] = {
        index: {} for index in range(shape.candidate_count)
    }
    outgoing: dict[int, dict[int, float]] = {
        index: {} for index in range(shape.candidate_count)
    }
    for flow_index, (left, right, pair_index) in enumerate(shape.directed):
        variable = shape.flow_offset + flow_index
        edge = shape.pair_offset + pair_index
        builder.add(
            {variable: 1.0, edge: -capacity},
            lower=-np.inf,
            upper=0.0,
        )
        outgoing[left][variable] = 1.0
        incoming[right][variable] = 1.0
    root_by_candidate = {
        candidate: shape.root_offset + index
        for index, candidate in enumerate(shape.grounded)
    }
    for candidate, variable in root_by_candidate.items():
        builder.add(
            {variable: 1.0, candidate: -capacity},
            lower=-np.inf,
            upper=0.0,
        )
        incoming[candidate][variable] = 1.0
    for candidate in range(shape.candidate_count):
        entries = {candidate: -1.0}
        entries.update(incoming[candidate])
        for variable in outgoing[candidate]:
            entries[variable] = -1.0
        builder.add(entries, lower=0.0, upper=0.0)


def _run_milp(
    candidates: list[_Candidate],
    *,
    shape: _ProblemShape,
    builder: _RowBuilder,
    objective: Literal["bricks", "mass"],
    deadline: float | None,
) -> OptimizeResult:
    costs = np.zeros(shape.variable_count, dtype=np.float64)
    contact_total = sum(count for _, count in shape.pair_items)
    rank_span = _RANK_EPSILON * shape.candidate_count**2 / 2.0
    primary_scale = max(_OBJECTIVE_SCALE, contact_total + rank_span + 1.0)
    for index, candidate in enumerate(candidates):
        primary = 1.0 if objective == "bricks" else candidate.mass_g
        costs[index] = primary_scale * primary + _RANK_EPSILON * index
    for pair_index, (_, contact_count) in enumerate(shape.pair_items):
        costs[shape.pair_offset + pair_index] = -_BOND_REWARD * contact_count
    integrality = np.zeros(shape.variable_count, dtype=np.uint8)
    integrality[: shape.flow_offset] = 1
    upper = np.full(
        shape.variable_count,
        float(shape.candidate_count),
        dtype=np.float64,
    )
    upper[: shape.flow_offset] = 1.0
    options: dict[str, float | bool] = {"presolve": True}
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return OptimizeResult(status=1, message="deadline exhausted")
        options["time_limit"] = remaining
    try:
        return milp(
            c=costs,
            integrality=integrality,
            bounds=Bounds(np.zeros(shape.variable_count), upper),
            constraints=builder.constraint(shape.variable_count),
            options=options,
        )
    except (ArithmeticError, RuntimeError, ValueError) as error:
        return OptimizeResult(status=4, message=str(error))


def _connection_pairs(candidates: list[_Candidate]) -> dict[tuple[int, int], int]:
    sockets: dict[tuple[Cell, tuple[int, int, int]], list[int]] = {}
    for index, candidate in enumerate(candidates):
        for connector in candidate.bottom:
            sockets.setdefault((connector.point, connector.direction), []).append(index)
    pairs: dict[tuple[int, int], int] = {}
    for below, candidate in enumerate(candidates):
        for connector in candidate.top:
            dx, dy, dz = connector.direction
            opposite = (-dx, -dy, -dz)
            for above in sockets.get((connector.point, opposite), ()):
                if below == above or boxes_intersect(
                    candidate.collision_boxes,
                    candidates[above].collision_boxes,
                ):
                    continue
                pair = (min(below, above), max(below, above))
                pairs[pair] = pairs.get(pair, 0) + 1
    return pairs


def _collision_pairs(candidates: list[_Candidate]) -> tuple[tuple[int, int], ...]:
    """Find exact collision conflicts after deterministic coarse bucketing."""
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for index, candidate in enumerate(candidates):
        candidate_buckets: set[tuple[int, int, int]] = set()
        for box in candidate.collision_boxes:
            x_range = range(box.minimum[0] // 20, (box.maximum[0] - 1) // 20 + 1)
            y_range = range(box.minimum[1] // 20, (box.maximum[1] - 1) // 20 + 1)
            z_range = range(box.minimum[2] // 8, (box.maximum[2] - 1) // 8 + 1)
            candidate_buckets.update(
                (x, y, z) for x in x_range for y in y_range for z in z_range
            )
        for bucket in candidate_buckets:
            buckets.setdefault(bucket, []).append(index)
    possible = {
        (left, right)
        for indexes in buckets.values()
        for position, left in enumerate(indexes)
        for right in indexes[position + 1 :]
        if left != right
    }
    return tuple(
        pair
        for pair in sorted(possible)
        if boxes_intersect(
            candidates[pair[0]].collision_boxes,
            candidates[pair[1]].collision_boxes,
        )
    )


def _cell_index(
    candidates: list[_Candidate],
) -> dict[Cell, list[int]]:
    result: dict[Cell, list[int]] = {}
    for index, candidate in enumerate(candidates):
        for cell in candidate.filled:
            result.setdefault(cell, []).append(index)
    return result


def _layout(
    catalog: Catalog,
    candidates: list[_Candidate],
    selected: frozenset[int],
) -> Layout:
    layout = Layout(catalog=catalog)
    for index in sorted(selected, key=lambda item: candidates[item].signature):
        candidate = candidates[index]
        layout.add(
            candidate.part_key,
            candidate.x,
            candidate.y,
            candidate.layer,
            candidate.yaw,
            candidate.colour_code,
        )
    return layout


def _weak_region_cut(
    layout: Layout,
    *,
    candidates: list[_Candidate],
    selected: frozenset[int],
    stability: StabilityResult,
) -> frozenset[int]:
    """Cut the unstable contact-graph neighbourhood, not the whole layout."""
    ordered = sorted(selected, key=lambda item: candidates[item].signature)
    candidate_by_brick = dict(enumerate(ordered))
    seeds = set(stability.unstable_ids)
    if stability.weakest_pair is not None:
        seeds.update(
            brick_id for brick_id in stability.weakest_pair if brick_id != GROUND_ID
        )
    graph = ConnectionGraph.from_layout(layout)
    adjacency: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    for left, right in graph.support_edges():
        if left != GROUND_ID:
            adjacency[left].add(right)
            adjacency[right].add(left)
    region = seeds | {
        neighbour for brick_id in seeds for neighbour in adjacency.get(brick_id, ())
    }
    cut = frozenset(
        candidate_by_brick[brick_id]
        for brick_id in sorted(region)
        if brick_id in candidate_by_brick
    )
    return cut or selected


def _exact_deadline(
    config: PipelineConfig, pipeline_deadline: float | None
) -> float | None:
    if config.exact_limit_policy == "continue":
        return pipeline_deadline or time.monotonic() + config.exact_time_limit_s
    own = time.monotonic() + config.exact_time_limit_s
    return min(own, pipeline_deadline) if pipeline_deadline is not None else own


def preflight_reason(grid: VoxelGrid, *, max_cells: int) -> str | None:
    """Why the whole-model exact strategy is ineligible, or ``None``.

    The shared preflight for the auto strategy selector and the bundle
    candidate planner: no preflight-ineligible exact run may start, and
    the reason is recorded wherever the skip happens.
    """
    if grid.filled_count > max_cells:
        return f"target has {grid.filled_count} cells; exact cap is {max_cells}"
    return None
