"""Bounded, deterministic redesign search for infeasible imported layouts."""

from __future__ import annotations

import heapq
import math
import queue
import time
from dataclasses import asdict, dataclass
from multiprocessing import get_context
from typing import TYPE_CHECKING, Any, Literal, cast

import numpy as np
from scipy import ndimage

from legolization.analysis import PlacementSignature, placement_signature, signatures
from legolization.catalog import Category
from legolization.graph import ConnectionGraph
from legolization.grid import EMPTY, VoxelGrid
from legolization.layout import CollisionError, Layout
from legolization.pipeline import PipelineConfig
from legolization.placement.layered.engine import place_rect
from legolization.placement.registry import make_strategy
from legolization.placement.repair import RepairConfig, repair_stability
from legolization.stability.solver import (
    SolverConfig,
    analyze,
    build_model_from_config,
    solve_maximin,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from multiprocessing.process import BaseProcess
    from multiprocessing.queues import Queue
    from pathlib import Path

    from legolization.ldraw_in import LdrawSourceRef

RepairTier = Literal["envelope-retile", "interior-support", "exterior-support"]
_TIER_RANK: dict[RepairTier, int] = {
    "envelope-retile": 0,
    "interior-support": 1,
    "exterior-support": 2,
}

# Bound the exterior bridge enumeration; only the nearest same-level stud
# pairs across components are ever worth validating within the budget.
_MAX_BRIDGE_PAIRS = 256


@dataclass(frozen=True, order=True, slots=True)
class _BridgeStud:
    """One upward stud available for a cross-component exterior bridge."""

    brick_id: int
    component: int
    x: int
    y: int
    z: int


_BridgePair = tuple[int, _BridgeStud, _BridgeStud]


@dataclass(frozen=True, slots=True)
class RepairCandidate:
    """One candidate that passed every official final-model check."""

    layout: Layout
    tier: RepairTier
    strict_capacity_n: float
    parity_max_score: float
    strict_max_score: float
    added_signatures: tuple[PlacementSignature, ...]
    removed_signatures: tuple[PlacementSignature, ...]
    removed_brick_ids: tuple[int, ...]
    added_cells: int
    visible_added_cells: int

    @property
    def rank(
        self,
    ) -> tuple[int, int, int, int, int, float, tuple[PlacementSignature, ...]]:
        """Least-visible lexicographic ranking requested by the CLI contract."""
        return (
            _TIER_RANK[self.tier],
            self.visible_added_cells,
            self.added_cells,
            len(self.removed_signatures),
            len(self.added_signatures),
            -self.strict_capacity_n,
            signatures(self.layout),
        )


@dataclass(frozen=True, slots=True)
class RepairSearchResult:
    """Best validated candidate plus bounded-search accounting."""

    layout: Layout | None
    candidate: RepairCandidate | None
    attempts: int
    elapsed_seconds: float
    timed_out: bool
    complete: bool
    error: str | None = None

    def to_report(
        self,
        source_refs: dict[int, LdrawSourceRef],
    ) -> dict[str, object]:
        """Serialize the candidate diff without embedding its full layout."""
        if self.candidate is None:
            return {
                "status": "error" if self.error is not None else "not_found",
                "attempts": self.attempts,
                "elapsed_seconds": self.elapsed_seconds,
                "timed_out": self.timed_out,
                "complete": self.complete,
                "exhausted": self.complete and not self.timed_out,
                "error": self.error,
            }
        removed: list[dict[str, object]] = []
        for signature, brick_id in zip(
            self.candidate.removed_signatures,
            self.candidate.removed_brick_ids,
            strict=True,
        ):
            source = source_refs.get(brick_id)
            removed.append(
                {
                    "placement": list(signature),
                    "source": asdict(source) if source is not None else None,
                }
            )
        match self.candidate.tier:
            case "envelope-retile":
                support_visibility = "none"
            case "interior-support":
                support_visibility = "enclosed"
            case "exterior-support":
                support_visibility = "visible"
        return {
            "status": "found",
            "tier": self.candidate.tier,
            "support_visibility": support_visibility,
            "attempts": self.attempts,
            "elapsed_seconds": self.elapsed_seconds,
            "timed_out": self.timed_out,
            "complete": self.complete,
            "exhausted": False,
            "metrics": {
                "strict_capacity_n": self.candidate.strict_capacity_n,
                "parity_max_score": self.candidate.parity_max_score,
                "strict_max_score": self.candidate.strict_max_score,
                "added_cells": self.candidate.added_cells,
                "visible_added_cells": self.candidate.visible_added_cells,
            },
            "after_metrics": {
                "feasible": True,
                "component_count": 1,
                "floating_count": 0,
                "rbe_5dof_stable": True,
                "rbe_5dof_max_score": self.candidate.parity_max_score,
                "rbe_6dof_stable": True,
                "rbe_6dof_max_score": self.candidate.strict_max_score,
                "strict_capacity_n": self.candidate.strict_capacity_n,
            },
            "diff": {
                "removed": removed,
                "added": [list(item) for item in self.candidate.added_signatures],
            },
        }


@dataclass(slots=True)
class _WorkerState:
    """Latest repair-search state streamed from the worker process."""

    candidate: RepairCandidate | None = None
    attempts: int = 0
    complete: bool = False
    timed_out: bool = False
    error: str | None = None


def _apply_worker_update(
    state: _WorkerState,
    kind: object,
    payload: object,
) -> None:
    match kind:
        case "candidate":
            state.candidate = cast("RepairCandidate", payload)
        case "progress":
            state.attempts = int(cast("int | str", payload))
        case "complete":
            state.attempts = int(cast("int | str", payload))
            state.complete = True
        case "timeout":
            state.attempts = int(cast("int | str", payload))
            state.timed_out = True
        case "error":
            state.error = str(payload)
            state.complete = True


def _poll_worker_updates(
    process: BaseProcess,
    updates: Queue[Any],
    *,
    deadline: float,
) -> _WorkerState:
    """Collect worker messages until completion or the hard deadline."""
    state = _WorkerState()
    while process.is_alive() and (remaining := deadline - time.monotonic()) > 0:
        try:
            kind, payload = updates.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue
        _apply_worker_update(state, kind, payload)
    return state


def _drain_worker_updates(updates: Queue[Any], state: _WorkerState) -> None:
    """Consume messages flushed as the worker exits without losing a candidate."""
    while True:
        try:
            # A short timeout lets the queue's reader thread surface a final
            # message a just-exited worker flushed but the parent has not yet
            # deserialized; a worker killed mid-put can also leave a truncated
            # pickle behind, which must not discard an already-streamed
            # candidate.
            kind, payload = updates.get(timeout=0.05)
        except queue.Empty:
            return
        except Exception:  # noqa: BLE001 - corrupt tail after a kill
            return
        _apply_worker_update(state, kind, payload)


def search_repair(  # noqa: PLR0913 - public search configuration
    layout: Layout,
    *,
    physics_seed_ids: tuple[int, ...],
    parity_solver: SolverConfig,
    strict_solver: SolverConfig,
    time_budget_s: float,
    seed: int,
) -> RepairSearchResult:
    """Run redesign in a killable worker and retain its latest best candidate."""
    if not math.isfinite(time_budget_s) or time_budget_s <= 0:
        msg = "time_budget_s must be finite and positive"
        raise ValueError(msg)
    context = get_context("spawn")
    updates: Queue[Any] = context.Queue()
    process = context.Process(
        target=_search_worker,
        args=(
            layout,
            physics_seed_ids,
            parity_solver,
            strict_solver,
            time_budget_s,
            seed,
            updates,
        ),
    )
    started = time.monotonic()
    process.start()
    deadline = started + time_budget_s
    state = _poll_worker_updates(process, updates, deadline=deadline)
    timed_out = state.timed_out or process.is_alive()
    if timed_out:
        process.terminate()
    process.join(timeout=0.25)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.25)
    _drain_worker_updates(updates, state)
    timed_out = timed_out or state.timed_out
    if not timed_out and not state.complete:
        state.error = (
            f"repair worker exited with code {process.exitcode}"
            if process.exitcode not in (0, None)
            else "repair worker ended without a completion result"
        )
    updates.close()
    return RepairSearchResult(
        layout=state.candidate.layout if state.candidate is not None else None,
        candidate=state.candidate,
        attempts=state.attempts,
        elapsed_seconds=round(time.monotonic() - started, 6),
        timed_out=timed_out,
        complete=state.complete and not timed_out,
        error=state.error,
    )


def _search_worker(  # noqa: PLR0913, PLR0917 - process target mirrors search inputs
    layout: Layout,
    physics_seed_ids: tuple[int, ...],
    parity_solver: SolverConfig,
    strict_solver: SolverConfig,
    time_budget_s: float,
    seed: int,
    updates: Queue[Any],
) -> None:
    try:
        _search_body(
            layout,
            physics_seed_ids=physics_seed_ids,
            parity_solver=parity_solver,
            strict_solver=strict_solver,
            deadline=time.monotonic() + time_budget_s,
            seed=seed,
            updates=updates,
        )
    except Exception as error:  # noqa: BLE001 - child must report, never vanish
        updates.put(("error", f"{type(error).__name__}: {error}"))


def _search_body(  # noqa: PLR0913
    original: Layout,
    *,
    physics_seed_ids: tuple[int, ...],
    parity_solver: SolverConfig,
    strict_solver: SolverConfig,
    deadline: float,
    seed: int,
    updates: Queue[Any],
) -> None:
    attempts = 0
    for tier, layouts in (
        (
            "envelope-retile",
            _envelope_candidates(
                original,
                seed=seed,
                deadline=deadline,
                strict_solver=strict_solver,
            ),
        ),
        (
            "interior-support",
            _support_candidates(
                original,
                physics_seed_ids,
                enclosed_only=True,
            ),
        ),
        (
            "exterior-support",
            _exterior_candidates(
                original,
                physics_seed_ids,
                deadline=deadline,
            ),
        ),
    ):
        best: RepairCandidate | None = None
        for candidate_layout in layouts:
            if time.monotonic() >= deadline:
                updates.put(("timeout", attempts))
                return
            attempts += 1
            updates.put(("progress", attempts))
            validated = _validate_candidate(
                original,
                candidate_layout,
                tier=tier,
                parity_solver=parity_solver,
                strict_solver=strict_solver,
            )
            if validated is not None and (best is None or validated.rank < best.rank):
                best = validated
                updates.put(("candidate", best))
        if best is not None:
            updates.put(("complete", attempts))
            return
    updates.put(("complete", attempts))


def _envelope_candidates(
    layout: Layout,
    *,
    seed: int,
    deadline: float,
    strict_solver: SolverConfig,
) -> Iterable[Layout]:
    replaceable_ids = {
        brick.brick_id
        for brick in layout
        if (part := layout.part_of(brick)).category in (Category.BRICK, Category.PLATE)
        or part.replaceable_geometry
    }
    if not replaceable_ids:
        return
    replaceable = layout.subset(replaceable_ids)
    frozen = layout.subset(set(layout.bricks) - replaceable_ids)
    grid, normalized, offset = _grid_and_normalized_layout(replaceable)
    for round_seed in range(seed, seed + 3):
        if time.monotonic() >= deadline:
            return
        candidate = normalized.copy()
        initial = analyze(layout=candidate, config=strict_solver)
        repair_stability(
            candidate,
            grid,
            catalog=candidate.catalog,
            solver_config=strict_solver,
            rng=np.random.default_rng(round_seed),
            config=RepairConfig(max_rounds=8),
            deadline=deadline,
            initial_stability=initial,
        )
        yield _restore_frozen(candidate, offset=offset, frozen=frozen)
    for strategy_name in ("bond", "fast", "beauty"):
        if time.monotonic() >= deadline:
            return
        config = PipelineConfig(
            strategy=strategy_name,
            hollow=False,
            refine=False,
            repair=False,
            seed=seed,
            time_budget_s=max(deadline - time.monotonic(), 0.001),
        )
        strategy = make_strategy(
            strategy_name,
            catalog=normalized.catalog,
            config=config,
        )
        candidate = strategy.place(
            grid,
            rng=np.random.default_rng(seed),
            deadline=deadline,
        )
        yield _restore_frozen(candidate, offset=offset, frozen=frozen)


def _support_candidates(
    layout: Layout,
    seed_ids: tuple[int, ...],
    *,
    enclosed_only: bool,
) -> Iterable[Layout]:
    holes = _enclosed_cells(layout) if enclosed_only else None
    for brick_id in seed_ids:
        if brick_id not in layout.bricks:
            continue
        brick = layout.bricks[brick_id]
        colour = brick.colour_code
        for connector in sorted(
            layout.connectors_of(brick, top=False),
            key=lambda item: item.cell,
        ):
            if connector.direction != (0, 0, -1):
                continue
            x, y, bottom = connector.cell
            additions = {
                (x, y, z) for z in range(bottom) if (x, y, z) not in layout.occupancy
            }
            if not additions or (holes is not None and not additions <= holes):
                continue
            candidate = layout.copy()
            if _add_column(candidate, additions, colour=colour):
                yield candidate


def _eligible_bridge_pairs(
    studs: list[_BridgeStud],
    *,
    deadline: float,
) -> Iterable[_BridgePair]:
    by_x: dict[tuple[int, int], list[_BridgeStud]] = {}
    by_y: dict[tuple[int, int], list[_BridgeStud]] = {}
    for stud in studs:
        if time.monotonic() >= deadline:
            return
        by_x.setdefault((stud.z, stud.x), []).append(stud)
        by_y.setdefault((stud.z, stud.y), []).append(stud)

    # X buckets cover every shared-x pair. Y buckets skip shared-x pairs so a
    # coincident coordinate cannot enter the heap twice.
    for buckets, skip_shared_x in ((by_x.values(), False), (by_y.values(), True)):
        for bucket in buckets:
            for index, a in enumerate(bucket):
                for other_index in range(index + 1, len(bucket)):
                    if time.monotonic() >= deadline:
                        return
                    b = bucket[other_index]
                    if a.component != b.component and (not skip_shared_x or a.x != b.x):
                        distance = abs(a.x - b.x) + abs(a.y - b.y)
                        yield distance, a, b


def _exterior_candidates(
    layout: Layout,
    seed_ids: tuple[int, ...],
    *,
    deadline: float,
) -> Iterable[Layout]:
    yield from _support_candidates(layout, seed_ids, enclosed_only=False)
    if time.monotonic() >= deadline:
        return
    graph = ConnectionGraph.from_layout(layout)
    if graph.component_count() <= 1:
        return
    labels = graph.brick_components()
    studs: list[_BridgeStud] = []
    for brick in layout:
        if time.monotonic() >= deadline:
            return
        for connector in layout.connectors_of(brick, top=True):
            if connector.direction == (0, 0, 1):
                x, y, z = connector.cell
                studs.append(
                    _BridgeStud(
                        brick_id=brick.brick_id,
                        component=labels[brick.brick_id],
                        x=x,
                        y=y,
                        z=z,
                    )
                )

    # Keep only the nearest bridgeable pairs. The source checks the deadline
    # for every examined pair so nsmallest cannot overrun the repair budget on
    # a large disconnected model.
    pairs = heapq.nsmallest(
        _MAX_BRIDGE_PAIRS,
        _eligible_bridge_pairs(studs, deadline=deadline),
    )
    for _, a, b in pairs:
        if time.monotonic() >= deadline:
            return
        candidate = layout.copy()
        x0, x1 = sorted((a.x, b.x))
        y0, y1 = sorted((a.y, b.y))
        try:
            place_rect(
                candidate,
                x0,
                y0,
                x1,
                y1,
                a.z + 1,
                1,
                layout.bricks[a.brick_id].colour_code,
            )
        except (CollisionError, ValueError):
            continue
        yield candidate


def _validate_candidate(  # noqa: PLR0911 - fail-fast validation gates
    original: Layout,
    candidate: Layout,
    *,
    tier: RepairTier,
    parity_solver: SolverConfig,
    strict_solver: SolverConfig,
) -> RepairCandidate | None:
    # Fail-fast acceptance gates are one cold-certification transaction.
    # lizard forgives(cyclomatic_complexity)
    original_placements = set(signatures(original))
    candidate_placements = set(signatures(candidate))
    if tier == "envelope-retile":
        if set(candidate.occupancy) != set(original.occupancy):
            return None
    elif not original_placements <= candidate_placements:
        return None
    for cell, original_id in original.occupancy.items():
        candidate_brick = candidate.brick_at(cell)
        if (
            candidate_brick is None
            or candidate_brick.colour_code != original.bricks[original_id].colour_code
        ):
            return None
    graph = ConnectionGraph.from_layout(candidate)
    if graph.component_count() != 1 or graph.floating_ids():
        return None
    parity = analyze(layout=candidate, config=parity_solver, graph=graph)
    if not parity.stable:
        return None
    strict = analyze(layout=candidate, config=strict_solver, graph=graph)
    if not strict.stable:
        return None
    maximin = solve_maximin(
        build_model_from_config(
            layout=candidate,
            config=strict_solver,
            graph=graph,
        )
    )
    if not maximin.feasible or maximin.capacity <= 0.0:
        return None
    removed = tuple(sorted(original_placements - candidate_placements))
    added = tuple(sorted(candidate_placements - original_placements))
    original_ids = {
        placement_signature(original, brick_id): brick_id
        for brick_id in original.bricks
    }
    added_cells = set(candidate.occupancy) - set(original.occupancy)
    return RepairCandidate(
        layout=candidate,
        tier=tier,
        strict_capacity_n=maximin.capacity,
        parity_max_score=parity.max_score,
        strict_max_score=strict.max_score,
        added_signatures=added,
        removed_signatures=removed,
        removed_brick_ids=tuple(original_ids[item] for item in removed),
        added_cells=len(added_cells),
        visible_added_cells=(0 if tier == "interior-support" else len(added_cells)),
    )


def _grid_and_normalized_layout(
    layout: Layout,
) -> tuple[VoxelGrid, Layout, tuple[int, int, int]]:
    xs, ys, zs = zip(*layout.occupancy, strict=True)
    offset = (min(xs), min(ys), min(zs))
    shape = (
        max(xs) - offset[0] + 1,
        max(ys) - offset[1] + 1,
        max(zs) - offset[2] + 1,
    )
    codes = np.full(shape, EMPTY, dtype=np.int16)
    normalized = Layout(catalog=layout.catalog)
    for brick in layout:
        normalized.add(
            part_key=brick.part_key,
            x=brick.x - offset[0],
            y=brick.y - offset[1],
            layer=brick.layer - offset[2],
            yaw=brick.yaw,
            colour_code=brick.colour_code,
        )
        for x, y, z in layout.cells_of(brick):
            codes[x - offset[0], y - offset[1], z - offset[2]] = brick.colour_code
    return VoxelGrid(codes=codes), normalized, offset


def _denormalize_layout(layout: Layout, offset: tuple[int, int, int]) -> Layout:
    restored = Layout(catalog=layout.catalog)
    for brick in layout:
        restored.add(
            part_key=brick.part_key,
            x=brick.x + offset[0],
            y=brick.y + offset[1],
            layer=brick.layer + offset[2],
            yaw=brick.yaw,
            colour_code=brick.colour_code,
        )
    return restored


def _restore_frozen(
    layout: Layout,
    *,
    offset: tuple[int, int, int],
    frozen: Layout,
) -> Layout:
    """Denormalize replaceable cells and restore exact special placements."""
    restored = _denormalize_layout(layout, offset)
    for brick in frozen:
        restored.add(
            part_key=brick.part_key,
            x=brick.x,
            y=brick.y,
            layer=brick.layer,
            yaw=brick.yaw,
            colour_code=brick.colour_code,
        )
    return restored


def _enclosed_cells(layout: Layout) -> set[tuple[int, int, int]]:
    if not layout.occupancy:
        return set()
    xs, ys, zs = zip(*layout.occupancy, strict=True)
    minimum = (min(xs) - 1, min(ys) - 1, 0)
    shape = (
        max(xs) - minimum[0] + 2,
        max(ys) - minimum[1] + 2,
        max(zs) + 2,
    )
    occupied = np.zeros(shape, dtype=bool)
    for x, y, z in layout.occupancy:
        occupied[x - minimum[0], y - minimum[1], z] = True
    holes = ndimage.binary_fill_holes(occupied) & ~occupied
    return {
        (x + minimum[0], y + minimum[1], z)
        for x, y, z in zip(*np.nonzero(holes), strict=True)
    }


def _add_column(
    layout: Layout,
    additions: set[tuple[int, int, int]],
    *,
    colour: int,
) -> bool:
    by_column: dict[tuple[int, int], set[int]] = {}
    for x, y, z in additions:
        by_column.setdefault((x, y), set()).add(z)
    try:
        for (x, y), layers in sorted(by_column.items()):
            z = min(layers)
            top = max(layers)
            while z <= top:
                if z not in layers:
                    z += 1
                    continue
                if {z, z + 1, z + 2} <= layers:
                    layout.add(
                        part_key="brick_1x1",
                        x=x,
                        y=y,
                        layer=z,
                        yaw=0,
                        colour_code=colour,
                    )
                    z += 3
                else:
                    layout.add(
                        part_key="plate_1x1",
                        x=x,
                        y=y,
                        layer=z,
                        yaw=0,
                        colour_code=colour,
                    )
                    z += 1
    except CollisionError:
        return False
    return True


def write_repair_model(layout: Layout, path: Path) -> None:
    """Write a flattened repair candidate through the normal LDraw emitter."""
    from legolization.ldraw_out import write_model  # noqa: PLC0415 - cycle guard

    write_model(layout, path, steps=False)
