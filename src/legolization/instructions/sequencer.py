"""Prefix-stable assembly sequencing with pull-forward and ROTSTEP hints.

The sequencer orders band-pure chunks (see :mod:`chunking`) so that every
emitted prefix is a physically stable, vertically insertable structure:

- a chunk is *ready* when its supports are placed, no already-placed brick
  blocks its vertical insertion, and pulling it forward cannot strand a
  still-unplaced brick under a new overhang;
- among the first ``beam_width`` ready chunks, the earliest whose prefix
  the RBE calls stable is taken (one LP per step on the fast path);
- when no ready chunk yields a stable prefix, the best-scoring one is
  emitted with ``prefix_stable=False`` and a warning (``stability_policy=
  "strict"`` raises instead) — genuinely unorderable prefixes exist, e.g.
  cantilevers whose counterweight shares their band.

Pure band order is always insertion-feasible (a placed brick above an
unplaced one's column would have to overlap it), so the loop can always
fall back to it. Everything is deterministic: no RNG, and every ordering
key ends in a brick id.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.instructions.blocking import vertical_blockers
from legolization.instructions.bom import BillOfMaterials, bill_of_materials
from legolization.instructions.chunking import chunk_bands, mirror_pairs
from legolization.stability.solver import SolverConfig, analyze

if TYPE_CHECKING:
    from legolization.layout import Layout

_ROTATE_THRESHOLD_DEG = 120.0
_ROTATE_GAIN_DEG = 45.0
_CENTRE_DEAD_ZONE_STUDS = 2.0
_DEFAULT_VIEW_AZIMUTH_DEG = 45.0  # LDraw viewers default to a front-right view


class InstructionsError(ValueError):
    """Raised in strict mode when no never-unstable ordering exists."""


@dataclass(frozen=True, slots=True)
class InstructionsConfig:
    """Sequencing knobs."""

    mode: Literal["smart", "layer"] = "smart"
    target_step_size: int = 7
    max_step_size: int = 10
    min_step_size: int = 3
    rotstep: bool = True
    beam_width: int = 4
    stability_policy: Literal["warn", "strict"] = "warn"
    solver: SolverConfig | None = None


@dataclass(frozen=True, slots=True)
class RotStep:
    """A view rotation applying to the step it is attached to."""

    yaw: int
    mode: Literal["REL", "ABS", "END"] = "REL"


@dataclass(frozen=True, slots=True)
class BuildStep:
    """One instruction step: bricks in emission order plus its verdict."""

    index: int
    brick_ids: tuple[int, ...]
    prefix_stable: bool
    prefix_max_score: float
    rotstep: RotStep | None = None


@dataclass(frozen=True, slots=True)
class InstructionPlan:
    """A full build sequence with warnings and the bill of materials."""

    steps: tuple[BuildStep, ...]
    warnings: tuple[str, ...]
    bom: BillOfMaterials

    @property
    def order(self) -> tuple[int, ...]:
        """Every brick id in build order."""
        return tuple(brick_id for step in self.steps for brick_id in step.brick_ids)


def plan_instructions(
    layout: Layout,
    *,
    config: InstructionsConfig | None = None,
) -> InstructionPlan:
    """Sequence the layout into digestible, never-unstable-mid-build steps."""
    config = config or InstructionsConfig()
    graph = ConnectionGraph.from_layout(layout)
    supports: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    for below_id, above_id in graph.support_edges():
        if below_id != GROUND_ID:
            supports[above_id].add(below_id)
    blockers = vertical_blockers(layout)
    blocks: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    for brick_id, blocked_by in blockers.items():
        for blocker in blocked_by:
            blocks[blocker].add(brick_id)

    chunks = chunk_bands(layout, config=config, pairs=mirror_pairs(layout))
    ordered_steps, warnings = _sequence(
        layout, config, chunks, supports, blockers, blocks
    )
    if config.rotstep:
        ordered_steps = _assign_rotsteps(layout, ordered_steps)
    plan = InstructionPlan(
        steps=tuple(ordered_steps),
        warnings=tuple(warnings),
        bom=BillOfMaterials(total=(), per_step=()),
    )
    return InstructionPlan(
        steps=plan.steps,
        warnings=plan.warnings,
        bom=bill_of_materials(layout, plan=plan),
    )


def _sequence(  # noqa: PLR0913, C901 - the greedy loop owns all sequencing state
    layout: Layout,
    config: InstructionsConfig,
    chunks: list[tuple[int, tuple[int, ...]]],
    supports: dict[int, set[int]],
    blockers: dict[int, frozenset[int]],
    blocks: dict[int, set[int]],
) -> tuple[list[BuildStep], list[str]]:
    pending = list(range(len(chunks)))
    placed: set[int] = set()
    steps: list[BuildStep] = []
    warnings: list[str] = []

    def emit(chunk: tuple[int, ...], *, stable: bool, score: float) -> None:
        ordered = tuple(
            sorted(
                chunk,
                key=lambda bid: (
                    layout.bricks[bid].layer,
                    layout.bricks[bid].y,
                    layout.bricks[bid].x,
                    bid,
                ),
            )
        )
        steps.append(
            BuildStep(
                index=len(steps) + 1,
                brick_ids=ordered,
                prefix_stable=stable,
                prefix_max_score=score,
            )
        )
        placed.update(chunk)

    while pending:
        ready: list[int] = []
        for position in pending:
            _, chunk = chunks[position]
            if not _ready(chunk, placed, supports, blockers, blocks):
                continue
            ready.append(position)
            if len(ready) >= config.beam_width:
                break
        if not ready:
            # Belt-and-braces: fall back to pure band order, which is
            # always insertion-feasible.
            warnings.append("sequencer deadlocked; remaining steps follow band order")
            for position in pending:
                _, chunk = chunks[position]
                result = analyze(layout.subset(placed | set(chunk)), config.solver)
                emit(chunk, stable=result.stable, score=result.max_score)
            break

        chosen: int | None = None
        best: tuple[float, int] | None = None
        for position in ready:
            _, chunk = chunks[position]
            result = analyze(layout.subset(placed | set(chunk)), config.solver)
            if result.stable:
                chosen = position
                emit(chunk, stable=True, score=result.max_score)
                break
            if best is None or result.max_score < best[0]:
                best = (result.max_score, position)
        if chosen is None:
            assert best is not None  # noqa: S101 - ready was non-empty
            score, position = best
            if config.stability_policy == "strict":
                msg = (
                    f"no stable ordering at step {len(steps) + 1} "
                    f"(best prefix score {score:.3f})"
                )
                raise InstructionsError(msg)
            _, chunk = chunks[position]
            warnings.append(
                f"step {len(steps) + 1}: prefix unstable (score {score:.2f}); "
                "support the overhang by hand while building"
            )
            emit(chunk, stable=False, score=score)
            chosen = position
        pending.remove(chosen)
    return steps, warnings


def _ready(
    chunk: tuple[int, ...],
    placed: set[int],
    supports: dict[int, set[int]],
    blockers: dict[int, frozenset[int]],
    blocks: dict[int, set[int]],
) -> bool:
    chunk_set = set(chunk)
    settled = placed | chunk_set
    for brick_id in chunk:
        if not supports[brick_id] <= placed:
            return False
        if blockers[brick_id] & placed:
            return False
        # Pull-forward safety: placing this chunk must not strand any
        # still-unplaced brick under a new overhang.
        if not blocks[brick_id] <= settled:
            return False
    return True


def _assign_rotsteps(
    layout: Layout,
    steps: list[BuildStep],
) -> list[BuildStep]:
    """Rotate the view when a step's bricks face away from it (hysteresis)."""
    if not steps:
        return []
    columns = [(x, y) for brick in layout for x, y, _ in layout.cells_of(brick)]
    model_x = sum(x for x, _ in columns) / len(columns)
    model_y = sum(y for _, y in columns) / len(columns)
    view = 0
    rotated: list[BuildStep] = []
    for step in steps:
        step_columns = [
            (x, y)
            for brick_id in step.brick_ids
            for x, y, _ in layout.cells_of(layout.bricks[brick_id])
        ]
        cx = sum(x for x, _ in step_columns) / len(step_columns)
        cy = sum(y for _, y in step_columns) / len(step_columns)
        rotstep: RotStep | None = None
        if math.hypot(cx - model_x, cy - model_y) > _CENTRE_DEAD_ZONE_STUDS:
            azimuth = math.degrees(math.atan2(cy - model_y, cx - model_x))
            current = _angular_distance(azimuth, _facing(view))
            best_yaw = min(
                (0, 90, 180, 270),
                key=lambda yaw: (_angular_distance(azimuth, _facing(yaw)), yaw),
            )
            improved = _angular_distance(azimuth, _facing(best_yaw))
            if (
                current > _ROTATE_THRESHOLD_DEG
                and current - improved >= _ROTATE_GAIN_DEG
            ):
                rotstep = RotStep(yaw=(best_yaw - view) % 360, mode="REL")
                view = best_yaw
        rotated.append(
            BuildStep(
                index=step.index,
                brick_ids=step.brick_ids,
                prefix_stable=step.prefix_stable,
                prefix_max_score=step.prefix_max_score,
                rotstep=rotstep,
            )
        )
    return rotated


def _facing(view_yaw: int) -> float:
    """Azimuth presented to the viewer after rotating the model by yaw."""
    return _DEFAULT_VIEW_AZIMUTH_DEG - view_yaw


def _angular_distance(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def verify_plan(
    layout: Layout,
    plan: InstructionPlan,
    *,
    config: InstructionsConfig | None = None,
) -> list[str]:
    """Check plan invariants; returns human-readable violations (ideally [])."""
    config = config or InstructionsConfig()
    violations: list[str] = []
    order = plan.order
    if sorted(order) != sorted(layout.bricks):
        violations.append("plan does not cover every brick exactly once")
        return violations
    graph = ConnectionGraph.from_layout(layout)
    supports: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    for below_id, above_id in graph.support_edges():
        if below_id != GROUND_ID:
            supports[above_id].add(below_id)
    blockers = vertical_blockers(layout)
    placed: set[int] = set()
    for step in plan.steps:
        step_set = set(step.brick_ids)
        for brick_id in step.brick_ids:
            if not supports[brick_id] <= placed | step_set:
                violations.append(f"step {step.index}: support after dependent")
            if blockers[brick_id] & placed:
                violations.append(f"step {step.index}: vertically blocked insert")
        result = analyze(layout.subset(placed | step_set), config.solver)
        if result.stable != step.prefix_stable:
            violations.append(
                f"step {step.index}: prefix stability mismatch "
                f"(expected {step.prefix_stable}, analyzed {result.stable})"
            )
        placed |= step_set
    return violations
