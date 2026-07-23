"""Prefix-stable assembly sequencing with pull-forward and ROTSTEP hints.

The sequencer orders band-pure chunks (see :mod:`chunking`) so that every
emitted prefix is a physically stable, vertically insertable structure:

- a chunk is *ready* when its supports are placed, no already-placed brick
  blocks its vertical insertion, and pulling it forward cannot strand a
  still-unplaced brick under a new overhang;
- among the first ``beam_width`` ready chunks — evaluated nearest-previous-
  step first when ``spatial_tiebreak`` is on (Ma et al.'s continuity
  heuristic) — the earliest whose prefix the RBE calls stable is taken
  (one LP per step on the fast path);
- when the greedy pass degrades (readiness deadlock, or no ready chunk
  yields a stable prefix), the remainder is re-planned by
  assembly-by-disassembly (:mod:`search`), which walks backward from the
  complete structure along a maximal-stability path; steps that stay
  unstable get ``prefix_stable=False`` and a warning
  (``stability_policy="strict"`` raises instead) — genuinely unorderable
  prefixes exist, e.g. cantilevers whose counterweight sits on their tail.
  ``fallback="band"`` restores the legacy unchecked band-order escape.

Pure band order is always insertion-feasible (a placed brick above an
unplaced one's column would have to overlap it). Everything is
deterministic: no RNG, and every ordering key ends in a brick id or a
deterministic chunk position.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import combinations
from typing import TYPE_CHECKING, Literal

from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.instructions.blocking import chunk_ready, vertical_blockers
from legolization.instructions.bom import BillOfMaterials, bill_of_materials
from legolization.instructions.chunking import chunk_bands, chunk_centroid, mirror_pairs
from legolization.instructions.search import (
    ChunkVerdict,
    beam_order,
    disassembly_order,
)
from legolization.stability.prefix import PrefixSolver, RemovalSolver
from legolization.stability.solver import SolverConfig, StabilityResult, analyze

if TYPE_CHECKING:
    from collections.abc import Callable

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
    spatial_tiebreak: bool = True
    fallback: Literal["disassembly", "band"] = "disassembly"
    search: Literal["greedy", "beam"] = "greedy"
    beam_states: int = 3
    lp_budget: int | None = None  # beam mode; None = 8 x chunk count
    subassemblies: bool = True
    min_sub_bricks: int = 3
    max_subassemblies: int = 6
    insertion_check: bool = False
    """Prefer press-robust orderings: a statically stable candidate that
    collapses under the insertion press keeps scanning the ready window
    for a press-stable alternative; only-fragile windows are accepted
    with a warning and ``BuildStep.insertion_fragile`` (never rescue)."""
    insertion_mass_kg: float = 1.0

    def __post_init__(self) -> None:
        """Validate search widths before sequencing starts."""
        if self.beam_width <= 0:
            msg = "beam_width must be positive"
            raise ValueError(msg)
        if not math.isfinite(self.insertion_mass_kg) or self.insertion_mass_kg <= 0:
            msg = (
                f"insertion_mass_kg must be finite and positive, "
                f"got {self.insertion_mass_kg}"
            )
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RotStep:
    """A view rotation applying to the step it is attached to."""

    yaw: int
    mode: Literal["REL", "ABS", "END"] = "REL"


@dataclass(frozen=True, slots=True)
class BuildStep:
    """One instruction step: bricks in emission order plus its verdict.

    ``submodel`` names the subassembly this step builds on the table;
    ``attaches`` marks the step that seats a finished subassembly onto
    the main model (such steps place no individual bricks:
    ``brick_ids == ()``).
    """

    index: int
    brick_ids: tuple[int, ...]
    prefix_stable: bool
    prefix_max_score: float
    rotstep: RotStep | None = None
    submodel: str | None = None
    attaches: str | None = None
    insertion_fragile: bool = False
    """Statically stable but collapses under the insertion press
    (Liu et al. 2024 virtual brick); appended last for back-compat."""


@dataclass(frozen=True, slots=True)
class Subassembly:
    """A cluster built separately and attached to the main model as a unit."""

    name: str
    brick_ids: tuple[int, ...]
    anchor_layer: int  # world base layer; sub-local layer = world - anchor


@dataclass(frozen=True, slots=True)
class InstructionPlan:
    """A full build sequence with warnings and the bill of materials.

    ``steps`` is flat: each subassembly's build steps appear immediately
    before its attach step, so every index-zipped consumer (per-step BOM
    callouts, step images, booklet entries) stays aligned.
    """

    steps: tuple[BuildStep, ...]
    warnings: tuple[str, ...]
    bom: BillOfMaterials
    subassemblies: tuple[Subassembly, ...] = ()

    @property
    def order(self) -> tuple[int, ...]:
        """Every brick id in build order."""
        return tuple(brick_id for step in self.steps for brick_id in step.brick_ids)

    def main_steps(self) -> tuple[BuildStep, ...]:
        """Return the main-model steps (attach steps included)."""
        return tuple(step for step in self.steps if step.submodel is None)

    def sub_steps(self, name: str) -> tuple[BuildStep, ...]:
        """Return one subassembly's build steps, in order."""
        return tuple(step for step in self.steps if step.submodel == name)


def plan_instructions(
    layout: Layout,
    *,
    config: InstructionsConfig | None = None,
) -> InstructionPlan:
    """Sequence the layout into digestible, never-unstable-mid-build steps."""
    config = config or InstructionsConfig()
    graph = ConnectionGraph.from_layout(layout)
    supports: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    neighbours: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    for below_id, above_id in graph.support_edges():
        if below_id != GROUND_ID:
            supports[above_id].add(below_id)
            neighbours[below_id].add(above_id)
            neighbours[above_id].add(below_id)
    for side in graph.side_contacts:
        neighbours[side.a_id].add(side.b_id)
        neighbours[side.b_id].add(side.a_id)
    blockers = vertical_blockers(layout)
    blocks: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
    for brick_id, blocked_by in blockers.items():
        for blocker in blocked_by:
            blocks[blocker].add(brick_id)

    chunks = chunk_bands(layout, config=config, pairs=mirror_pairs(layout))
    # With subassemblies on, strictness is judged AFTER the rewrite: the
    # extraction exists to stabilize persistently floating stretches, so
    # the pre-rewrite sequence must be allowed to carry warnings
    # (PR #17 review — strict+subassemblies used to raise before the
    # rewrite could make the plan stable).
    strict_after_rewrite = config.subassemblies and config.stability_policy == "strict"
    sequencing_config = (
        replace(config, stability_policy="warn") if strict_after_rewrite else config
    )
    ordered_steps, warnings = _sequence(
        layout,
        sequencing_config,
        chunks,
        supports,
        blockers,
        blocks,
        neighbours,
    )
    plan = InstructionPlan(
        steps=tuple(ordered_steps),
        warnings=tuple(warnings),
        bom=BillOfMaterials(total=(), per_step=()),
    )
    if config.subassemblies:
        from legolization.instructions.subassembly import (  # noqa: PLC0415 - cycle
            extract_subassemblies,
        )

        plan = extract_subassemblies(layout, plan, config=config)
        if strict_after_rewrite and any(not step.prefix_stable for step in plan.steps):
            unstable = [step.index for step in plan.steps if not step.prefix_stable]
            msg = (
                f"no stable ordering even with subassemblies "
                f"(steps {unstable} stay unstable)"
            )
            raise InstructionsError(msg)
    if config.rotstep:
        plan = replace(
            plan, steps=tuple(_assign_rotsteps_subaware(layout, list(plan.steps)))
        )
    return replace(plan, bom=bill_of_materials(layout, plan=plan))


def _assign_rotsteps_subaware(
    layout: Layout,
    steps: list[BuildStep],
) -> list[BuildStep]:
    """Assign ROTSTEP hints over MAIN steps only; sub steps stay unrotated."""
    main = [step for step in steps if step.submodel is None]
    rotated = iter(_assign_rotsteps(layout, main))
    return [step if step.submodel is not None else next(rotated) for step in steps]


def _sequence(  # noqa: PLR0912, PLR0913, PLR0915, C901 - sequencing state
    layout: Layout,
    config: InstructionsConfig,
    chunks: list[tuple[int, tuple[int, ...]]],
    supports: dict[int, set[int]],
    blockers: dict[int, frozenset[int]],
    blocks: dict[int, set[int]],
    neighbours: dict[int, set[int]],
) -> tuple[list[BuildStep], list[str]]:
    pending = list(range(len(chunks)))
    placed: set[int] = set()
    steps: list[BuildStep] = []
    warnings: list[str] = []
    cache: dict[frozenset[int], StabilityResult] = {}
    warm_keys: set[frozenset[int]] = set()
    prefix_solver: PrefixSolver | None = None
    centroids = [chunk_centroid(layout, chunk) for _, chunk in chunks]
    previous_centroid: tuple[float, float] | None = None
    band_rank = {
        layer: rank for rank, layer in enumerate(sorted({layer for layer, _ in chunks}))
    }
    brick_position = {
        brick_id: position
        for position, (_, chunk) in enumerate(chunks)
        for brick_id in chunk
    }

    def emit(
        chunk: tuple[int, ...],
        *,
        stable: bool,
        score: float,
        fragile: bool = False,
    ) -> None:
        if config.insertion_check:
            ordered = _insertion_order(
                layout,
                chunk,
                supports=supports,
                blockers=blockers,
            )
        else:
            # Preserve the byte-identical legacy plan when press checking
            # is disabled.
            ordered = _brick_order(layout, chunk)
        steps.append(
            BuildStep(
                index=len(steps) + 1,
                brick_ids=ordered,
                prefix_stable=stable,
                prefix_max_score=score,
                insertion_fragile=fragile,
            )
        )
        placed.update(chunk)

    def analyze_prefix(chunk: tuple[int, ...]) -> StabilityResult:
        key = frozenset(placed | set(chunk))
        if (hit := cache.get(key)) is None:
            if prefix_solver is not None:
                hit = prefix_solver.probe(chunk)
                warm_keys.add(key)
            else:
                hit = analyze(layout.subset(key), config.solver)
            cache[key] = hit
        return hit

    def accept(chunk: tuple[int, ...], score: float) -> None:
        emit(chunk, stable=True, score=score)
        if prefix_solver is not None:
            prefix_solver.commit(chunk)

    press_cache: dict[tuple[frozenset[int], frozenset[int]], StabilityResult] = {}

    def press_prefix(chunk: tuple[int, ...]) -> StabilityResult:
        # Keyed (prefix set, chunk set) — NOT analyze_prefix's
        # prefix-set-only cache: the same final set pressed via a
        # different chunk is a different load case.
        key = (frozenset(placed), frozenset(chunk))
        if (hit := press_cache.get(key)) is None:
            if prefix_solver is not None:
                hit = prefix_solver.press_probe(chunk, config.insertion_mass_kg)
            else:
                subset = frozenset(placed | set(chunk))
                hit = analyze(
                    layout.subset(subset),
                    config.solver,
                    extra_masses=dict.fromkeys(chunk, config.insertion_mass_kg),
                )
            press_cache[key] = hit
        return hit

    def press_selection(
        chunk: tuple[int, ...],
        press_ids: tuple[int, ...],
    ) -> StabilityResult:
        if prefix_solver is not None:
            return prefix_solver.press_probe_selection(
                chunk,
                press_ids,
                config.insertion_mass_kg,
            )
        subset = frozenset(placed | set(chunk))
        return analyze(
            layout.subset(subset),
            config.solver,
            extra_masses=dict.fromkeys(press_ids, config.insertion_mass_kg),
        )

    def disable_warm_engine() -> None:
        """Rescue/beam/band paths float-order scores: legacy numbers only."""
        nonlocal prefix_solver
        prefix_solver = None
        for key in warm_keys:
            cache.pop(key, None)
        warm_keys.clear()

    def emit_verdicts(  # noqa: C901 - fixed-tail coalescing state
        verdicts: list[ChunkVerdict],
    ) -> None:
        position = 0
        while position < len(verdicts):
            verdict = verdicts[position]
            if not verdict.stable:
                if config.stability_policy == "strict":
                    msg = (
                        f"no stable ordering at step {len(steps) + 1} "
                        f"(best prefix score {verdict.max_score:.3f})"
                    )
                    raise InstructionsError(msg)
                warnings.append(
                    f"step {len(steps) + 1}: prefix unstable "
                    f"(score {verdict.max_score:.2f}); "
                    "support the overhang by hand while building"
                )
            fragile = (
                config.insertion_check
                and verdict.stable
                and not press_prefix(verdict.chunk).stable
            )
            consumed = 1
            if fragile:
                # Rescue tails have a fixed order, so only coalesce a
                # consecutive tail prefix. Every verdict is recomputed
                # against the actual forward prefix before it is emitted.
                union = verdict.chunk
                fallback: tuple[tuple[int, ...], StabilityResult, int] | None = None
                for tail in range(position + 1, len(verdicts)):
                    candidate = union + verdicts[tail].chunk
                    if not _press_union_allowed(
                        layout,
                        candidate,
                        placed=placed,
                        supports=supports,
                        blockers=blockers,
                        blocks=blocks,
                        neighbours=neighbours,
                        band_rank=band_rank,
                        max_step_size=config.max_step_size,
                    ):
                        break
                    union = candidate
                    static = analyze_prefix(union)
                    press = press_prefix(union)
                    if static.stable and press.stable:
                        verdict = ChunkVerdict(
                            chunk=union,
                            stable=True,
                            max_score=static.max_score,
                        )
                        fragile = False
                        consumed = tail - position + 1
                        break
                    if static.stable and fallback is None:
                        fallback = (union, static, tail - position + 1)
                if fragile and fallback is not None:
                    union, static, consumed = fallback
                    verdict = ChunkVerdict(
                        chunk=union,
                        stable=True,
                        max_score=static.max_score,
                    )
            if fragile:
                warnings.append(
                    f"step {len(steps) + 1}: insertion-fragile; "
                    "press bricks home gently and support the joint"
                )
            emit(
                verdict.chunk,
                stable=verdict.stable,
                score=verdict.max_score,
                fragile=fragile,
            )
            position += consumed

    def rescue() -> None:
        """Re-plan the whole remainder by assembly-by-disassembly.

        The rescue goes warm through a :class:`RemovalSolver` when the
        highspy engine is on. Its scores can differ in degenerate float
        ties from the scipy engine's, so byte-identity across engines is
        guaranteed only for plans that never enter the rescue (which
        includes all shipped goldens); rescued plans are validated by
        ``verify_plan``/plan-quality equivalence instead.
        """
        nonlocal prefix_solver
        prefix_solver = None  # the forward base can no longer advance
        scope = frozenset(placed).union(
            *(set(chunks[position][1]) for position in pending), frozenset()
        )
        remover = RemovalSolver.create(layout, scope, config.solver)
        emit_verdicts(
            disassembly_order(
                layout,
                placed=frozenset(placed),
                chunks=[chunks[position] for position in pending],
                supports=supports,
                blockers=blockers,
                config=config,
                cache=cache,
                remover=remover,
            )
        )

    if config.search == "beam":
        emit_verdicts(
            beam_order(
                layout,
                chunks=chunks,
                supports=supports,
                blockers=blockers,
                blocks=blocks,
                config=config,
                cache=cache,
            )
        )
        return steps, warnings

    prefix_solver = PrefixSolver.create(layout, config.solver)

    while pending:
        ready = _gather_ready(
            pending,
            chunks,
            placed,
            supports,
            blockers,
            blocks,
            limit=config.beam_width,
        )
        if not ready:
            if config.fallback == "disassembly":
                rescue()
                break
            # Legacy escape hatch: pure band order is always
            # insertion-feasible, but its verdicts go unchecked.
            disable_warm_engine()
            warnings.append("sequencer deadlocked; remaining steps follow band order")
            for position in pending:
                _, chunk = chunks[position]
                result = analyze_prefix(chunk)
                emit_verdicts(
                    [
                        ChunkVerdict(
                            chunk=chunk,
                            stable=result.stable,
                            max_score=result.max_score,
                        )
                    ]
                )
            break
        if config.spatial_tiebreak and previous_centroid is not None:
            ready = _spatial_order(ready, chunks, centroids, previous_centroid)

        chosen, best, best_fragile = _scan_ready_window(
            ready,
            chunks,
            config,
            analyze_prefix=analyze_prefix,
            press_prefix=press_prefix,
            accept=accept,
        )
        consumed = {chosen} if chosen is not None else set()
        if chosen is None and best_fragile is not None:
            press_score, position, static_score = best_fragile
            _, chunk = chunks[position]
            subset = _best_press_subset(
                layout,
                chunk,
                placed=placed,
                supports=supports,
                blockers=blockers,
                blocks=blocks,
                max_step_size=config.max_step_size,
                analyze_prefix=analyze_prefix,
                press_prefix=press_prefix,
                press_selection=press_selection,
            )
            if subset is not None:
                subset_chunk, subset_score = subset
                emit(subset_chunk, stable=True, score=subset_score)
                if prefix_solver is not None:
                    prefix_solver.commit(subset_chunk)
                remainder = tuple(
                    brick_id for brick_id in chunk if brick_id not in subset_chunk
                )
                chunks[position] = (chunks[position][0], remainder)
                centroids[position] = chunk_centroid(layout, remainder)
                previous_centroid = chunk_centroid(layout, subset_chunk)
                continue
            composite = _best_press_union(
                layout,
                seed=position,
                pending=pending,
                chunks=chunks,
                placed=placed,
                supports=supports,
                blockers=blockers,
                blocks=blocks,
                neighbours=neighbours,
                band_rank=band_rank,
                brick_position=brick_position,
                max_step_size=config.max_step_size,
                analyze_prefix=analyze_prefix,
                press_prefix=press_prefix,
            )
            if composite is not None:
                positions, chunk, static_score, fragile, union_press_score = composite
                if fragile:
                    warnings.append(
                        f"step {len(steps) + 1}: insertion-fragile "
                        f"(press score {union_press_score:.2f}); "
                        "press bricks home gently and support the joint"
                    )
                emit(
                    chunk,
                    stable=True,
                    score=static_score,
                    fragile=fragile,
                )
                if prefix_solver is not None:
                    prefix_solver.commit(chunk)
                chosen = position
                consumed = set(positions)
            else:
                # No legal adjacent union improves the forced window.
                # Preserve the warning and least-fragile fallback.
                warnings.append(
                    f"step {len(steps) + 1}: insertion-fragile "
                    f"(press score {press_score:.2f}); "
                    "press bricks home gently and support the joint"
                )
                emit(chunk, stable=True, score=static_score, fragile=True)
                if prefix_solver is not None:
                    prefix_solver.commit(chunk)
                chosen = position
                consumed = {position}
        if chosen is None:
            assert best is not None  # noqa: S101 - ready was non-empty
            score, position = best
            if config.fallback == "disassembly":
                # No stable prefix in the window: re-plan the remainder
                # along the maximal-stability disassembly path instead of
                # committing to the least-bad forward pick.
                rescue()
                break
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
            if prefix_solver is not None:
                prefix_solver.commit(chunk)
            chosen = position
            consumed = {position}
        emitted_ids = steps[-1].brick_ids
        previous_centroid = chunk_centroid(layout, emitted_ids)
        for position in sorted(consumed):
            pending.remove(position)
    return steps, warnings


def _best_press_subset(  # noqa: PLR0913 - explicit refinement constraints
    layout: Layout,
    chunk: tuple[int, ...],
    *,
    placed: set[int],
    supports: dict[int, set[int]],
    blockers: dict[int, frozenset[int]],
    blocks: dict[int, set[int]],
    max_step_size: int,
    analyze_prefix: Callable[[tuple[int, ...]], StabilityResult],
    press_prefix: Callable[[tuple[int, ...]], StabilityResult],
    press_selection: Callable[
        [tuple[int, ...], tuple[int, ...]],
        StabilityResult,
    ],
) -> tuple[tuple[int, ...], float] | None:
    """Pre-refine a forced fragile base chunk using warm probes."""
    candidates: list[tuple[int, float, tuple[int, ...], float]] = []
    for size in range(min(len(chunk) - 1, max_step_size), 0, -1):
        for subset in combinations(chunk, size):
            if not chunk_ready(subset, placed, supports, blockers, blocks):
                continue
            if not _insertion_order(
                layout,
                subset,
                supports=supports,
                blockers=blockers,
            ):
                continue
            static = analyze_prefix(subset)
            if not static.stable:
                continue
            press = press_prefix(subset)
            if not press.stable:
                continue
            remainder = tuple(brick_id for brick_id in chunk if brick_id not in subset)
            remainder_press = press_selection(chunk, remainder)
            if remainder_press.stable:
                candidates.append(
                    (-size, press.max_score, tuple(sorted(subset)), static.max_score)
                )
        if candidates:
            break
    if not candidates:
        return None
    _size, _press, subset, static_score = min(candidates)
    return subset, static_score


def _brick_order(layout: Layout, chunk: tuple[int, ...]) -> tuple[int, ...]:
    """Legacy deterministic order inside a same-band chunk."""
    return tuple(
        sorted(
            chunk,
            key=lambda brick_id: (
                layout.bricks[brick_id].layer,
                layout.bricks[brick_id].y,
                layout.bricks[brick_id].x,
                brick_id,
            ),
        )
    )


def _insertion_order(
    layout: Layout,
    chunk: tuple[int, ...],
    *,
    supports: dict[int, set[int]],
    blockers: dict[int, frozenset[int]],
) -> tuple[int, ...]:
    """Topologically order a cross-band step for support and sweep safety."""
    chunk_set = set(chunk)
    successors = {brick_id: set() for brick_id in chunk}
    incoming = dict.fromkeys(chunk, 0)
    for brick_id in chunk:
        dependencies = supports[brick_id] & chunk_set
        # A brick must be inserted before any in-step brick that blocks
        # its sweep. This turns ``blockers[b]`` into b -> blocker edges.
        for dependency in dependencies:
            successors[dependency].add(brick_id)
        for blocker in blockers[brick_id] & chunk_set:
            successors[brick_id].add(blocker)
    for targets in successors.values():
        for target in targets:
            incoming[target] += 1

    def key(brick_id: int) -> tuple[int, int, int, int]:
        return (
            layout.bricks[brick_id].layer,
            layout.bricks[brick_id].y,
            layout.bricks[brick_id].x,
            brick_id,
        )

    ready = sorted(
        (brick_id for brick_id, degree in incoming.items() if degree == 0),
        key=key,
    )
    ordered: list[int] = []
    while ready:
        brick_id = ready.pop(0)
        ordered.append(brick_id)
        for target in sorted(successors[brick_id], key=key):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
                ready.sort(key=key)
    return tuple(ordered) if len(ordered) == len(chunk) else ()


def _press_union_allowed(  # noqa: PLR0913 - explicit union constraints
    layout: Layout,
    chunk: tuple[int, ...],
    *,
    placed: set[int],
    supports: dict[int, set[int]],
    blockers: dict[int, frozenset[int]],
    blocks: dict[int, set[int]],
    neighbours: dict[int, set[int]],
    band_rank: dict[int, int],
    max_step_size: int,
) -> bool:
    """Check all structural and insertion constraints for a press union."""
    if len(chunk) > max_step_size or len(set(chunk)) != len(chunk):
        return False
    ranks = {band_rank[layout.bricks[brick_id].layer] for brick_id in chunk}
    if len(ranks) > 2 or (ranks and max(ranks) - min(ranks) > 1):
        return False
    chunk_set = set(chunk)
    if len(chunk_set) > 1 and not any(
        neighbours[brick_id] & (chunk_set - {brick_id}) for brick_id in chunk
    ):
        return False
    return chunk_ready(chunk, placed, supports, blockers, blocks) and bool(
        _insertion_order(
            layout,
            chunk,
            supports=supports,
            blockers=blockers,
        )
    )


def _best_press_union(  # noqa: C901, PLR0912, PLR0913 - candidate state
    layout: Layout,
    *,
    seed: int,
    pending: list[int],
    chunks: list[tuple[int, tuple[int, ...]]],
    placed: set[int],
    supports: dict[int, set[int]],
    blockers: dict[int, frozenset[int]],
    blocks: dict[int, set[int]],
    neighbours: dict[int, set[int]],
    band_rank: dict[int, int],
    brick_position: dict[int, int],
    max_step_size: int,
    analyze_prefix: Callable[[tuple[int, ...]], StabilityResult],
    press_prefix: Callable[[tuple[int, ...]], StabilityResult],
) -> tuple[tuple[int, ...], tuple[int, ...], float, bool, float] | None:
    """Find the smallest deterministic adjacent union that survives pressing."""
    pending_set = set(pending)
    centroids = {
        position: chunk_centroid(layout, chunks[position][1]) for position in pending
    }
    queue: list[frozenset[int]] = [frozenset({seed})]
    seen = {queue[0]}
    ranked: list[
        tuple[
            int,
            float,
            float,
            tuple[int, ...],
            tuple[int, ...],
            tuple[int, ...],
            float,
        ]
    ] = []
    fragile_ranked: list[
        tuple[
            int,
            float,
            float,
            tuple[int, ...],
            tuple[int, ...],
            tuple[int, ...],
            float,
        ]
    ] = []
    while queue and len(seen) <= 128:
        positions = queue.pop(0)
        adjacent = _adjacent_chunk_positions(
            positions=positions,
            pending=pending,
            chunks=chunks,
            neighbours=neighbours,
            centroids=centroids,
        )
        for candidate_position in adjacent:
            expanded = set(positions)
            expanded.add(candidate_position)
            # Pull in the complete base chunk for every unplaced support.
            changed = True
            while changed:
                changed = False
                expanded_ids = {
                    brick_id
                    for position in expanded
                    for brick_id in chunks[position][1]
                }
                missing = {
                    dependency
                    for brick_id in expanded_ids
                    for dependency in supports[brick_id]
                    if dependency not in placed and dependency not in expanded_ids
                }
                for dependency in sorted(missing):
                    support_position = brick_position.get(dependency)
                    if support_position is None or support_position not in pending_set:
                        expanded = set()
                        break
                    if support_position not in expanded:
                        expanded.add(support_position)
                        changed = True
                if not expanded:
                    break
            if not expanded:
                continue
            state = frozenset(expanded)
            if state in seen:
                continue
            seen.add(state)
            union = tuple(
                brick_id
                for position in sorted(state)
                for brick_id in chunks[position][1]
            )
            if not _press_union_allowed(
                layout,
                union,
                placed=placed,
                supports=supports,
                blockers=blockers,
                blocks=blocks,
                neighbours=neighbours,
                band_rank=band_rank,
                max_step_size=max_step_size,
            ):
                continue
            static = analyze_prefix(union)
            press = press_prefix(union)
            if static.stable and press.stable:
                seed_x, seed_y = centroids[seed]
                distance = sum(
                    (centroids[position][0] - seed_x) ** 2
                    + (centroids[position][1] - seed_y) ** 2
                    for position in state
                    if position != seed
                )
                ranked.append(
                    (
                        len(union),
                        press.max_score,
                        distance,
                        tuple(sorted(union)),
                        tuple(sorted(state)),
                        union,
                        static.max_score,
                    )
                )
            elif static.stable:
                seed_x, seed_y = centroids[seed]
                distance = sum(
                    (centroids[position][0] - seed_x) ** 2
                    + (centroids[position][1] - seed_y) ** 2
                    for position in state
                    if position != seed
                )
                fragile_ranked.append(
                    (
                        len(union),
                        press.max_score,
                        distance,
                        tuple(sorted(union)),
                        tuple(sorted(state)),
                        union,
                        static.max_score,
                    )
                )
            queue.append(state)
    if not ranked and not fragile_ranked:
        return None
    fragile = not ranked
    chosen = min(fragile_ranked if fragile else ranked)
    _, press_score, _, _, positions, union, static_score = chosen
    return positions, union, static_score, fragile, press_score


def _adjacent_chunk_positions(
    *,
    positions: frozenset[int],
    pending: list[int],
    chunks: list[tuple[int, tuple[int, ...]]],
    neighbours: dict[int, set[int]],
    centroids: dict[int, tuple[float, float]],
) -> list[int]:
    """Return deterministic contact/spatial neighbours of a chunk union."""
    union_ids = {brick_id for position in positions for brick_id in chunks[position][1]}
    contact = {
        position
        for position in pending
        if position not in positions
        and any(neighbours[brick_id] & union_ids for brick_id in chunks[position][1])
    }
    union_x = sum(centroids[position][0] for position in positions) / len(positions)
    union_y = sum(centroids[position][1] for position in positions) / len(positions)
    spatial = sorted(
        (position for position in pending if position not in positions),
        key=lambda position: (
            (centroids[position][0] - union_x) ** 2
            + (centroids[position][1] - union_y) ** 2,
            chunks[position][0],
            position,
        ),
    )[:4]
    return sorted(contact | set(spatial))


def _scan_ready_window(  # noqa: PLR0913 - the scan reads the loop's shared state
    window: list[int],
    chunks: list[tuple[int, tuple[int, ...]]],
    config: InstructionsConfig,
    *,
    analyze_prefix: Callable[[tuple[int, ...]], StabilityResult],
    press_prefix: Callable[[tuple[int, ...]], StabilityResult],
    accept: Callable[[tuple[int, ...], float], None],
) -> tuple[int | None, tuple[float, int] | None, tuple[float, int, float] | None]:
    """Scan one ready window for the first acceptable chunk.

    Returns (accepted position or None, best-unstable ``(score, pos)``,
    best-fragile ``(press score, pos, static score)``). With
    ``insertion_check`` on, a statically stable candidate that collapses
    under the press is skipped (tracked as best-fragile) and the scan
    continues — the caller decides what to do with an all-fragile window.
    """
    best: tuple[float, int] | None = None
    best_fragile: tuple[float, int, float] | None = None
    for position in window:
        _, chunk = chunks[position]
        result = analyze_prefix(chunk)
        if result.stable:
            if config.insertion_check and not (press := press_prefix(chunk)).stable:
                if best_fragile is None or press.max_score < best_fragile[0]:
                    best_fragile = (press.max_score, position, result.max_score)
                continue
            accept(chunk, result.max_score)
            return position, best, best_fragile
        if best is None or result.max_score < best[0]:
            best = (result.max_score, position)
    return None, best, best_fragile


def _gather_ready(  # noqa: PLR0913 - the readiness scan reads all sequencing state
    pending: list[int],
    chunks: list[tuple[int, tuple[int, ...]]],
    placed: set[int],
    supports: dict[int, set[int]],
    blockers: dict[int, frozenset[int]],
    blocks: dict[int, set[int]],
    *,
    limit: int,
) -> list[int]:
    """First ``limit`` pending chunk positions whose insertion is feasible."""
    ready: list[int] = []
    for position in pending:
        _, chunk = chunks[position]
        if not chunk_ready(chunk, placed, supports, blockers, blocks):
            continue
        ready.append(position)
        if len(ready) >= limit:
            break
    return ready


def _spatial_order(
    ready: list[int],
    chunks: list[tuple[int, tuple[int, ...]]],
    centroids: list[tuple[float, float]],
    previous: tuple[float, float],
) -> list[int]:
    """Evaluate spatially adjacent candidates first (Ma et al. continuity).

    Ordering only: with the first-stable early exit intact this costs no
    extra LP calls, it just prefers the ready chunk nearest the previous
    step when several prefixes are equally viable.
    """
    previous_x, previous_y = previous

    def key(position: int) -> tuple[float, int, int]:
        centroid_x, centroid_y = centroids[position]
        distance_sq = (centroid_x - previous_x) ** 2 + (centroid_y - previous_y) ** 2
        return (distance_sq, chunks[position][0], position)

    return sorted(ready, key=key)


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
        if not step_columns:  # attach steps place no individual bricks
            rotated.append(step)
            continue
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
    """Check plan invariants; returns human-readable violations (ideally []).

    Three step kinds: sub-build steps are checked in the subassembly's
    own grounded frame (the unit sits on the table); an attach step is
    checked as a UNIT insertion — every sub brick's world blockers
    against the placed world, unit grounding, and the post-attach
    analysis; main steps get the classic per-brick checks. Preservation
    argument for the rewrite: extracted bricks only ever move LATER, and
    any window brick stud-touching the cluster is inside it by component
    construction, so no surviving main step can lose a support.
    """
    config = config or InstructionsConfig()
    if sorted(plan.order) != sorted(layout.bricks):
        return ["plan does not cover every brick exactly once"]
    walker = _PlanVerifier.create(layout, plan, config)
    for step in plan.steps:
        if step.submodel is not None:
            walker.sub_step(step)
        elif step.attaches is not None:
            walker.attach_step(step)
        else:
            walker.main_step(step)
    walker.finish()
    return walker.violations


@dataclass(slots=True)
class _PlanVerifier:
    """One verification walk's shared state; one method per step kind."""

    layout: Layout
    config: InstructionsConfig
    supports: dict[int, set[int]]
    blockers: dict[int, frozenset[int]]
    subs: dict[str, Subassembly]
    sub_placed: dict[str, set[int]]
    placed: set[int]
    prefix_solver: PrefixSolver | None
    violations: list[str]
    attach_counts: dict[str, int]

    @classmethod
    def create(
        cls,
        layout: Layout,
        plan: InstructionPlan,
        config: InstructionsConfig,
    ) -> _PlanVerifier:
        """Build the walk state for one layout/plan pair."""
        graph = ConnectionGraph.from_layout(layout)
        supports: dict[int, set[int]] = {brick_id: set() for brick_id in layout.bricks}
        for below_id, above_id in graph.support_edges():
            if below_id != GROUND_ID:
                supports[above_id].add(below_id)
        subs = {sub.name: sub for sub in plan.subassemblies}
        return cls(
            layout=layout,
            config=config,
            supports=supports,
            blockers=vertical_blockers(layout),
            subs=subs,
            sub_placed={name: set() for name in subs},
            placed=set(),
            # Final-order main prefixes are strictly append-only: warm fit.
            prefix_solver=PrefixSolver.create(layout, config.solver),
            violations=[],
            attach_counts=dict.fromkeys(subs, 0),
        )

    def _analyzed(self, subset: set[int]) -> StabilityResult:
        return analyze(self.layout.subset(subset), self.config.solver)

    def sub_step(self, step: BuildStep) -> None:
        """Check one sub-build step in the subassembly's grounded frame."""
        sub = self.subs.get(step.submodel or "")
        if sub is None:
            self.violations.append(f"step {step.index}: unknown submodel")
            return
        step_set = set(step.brick_ids)
        seen = self.sub_placed[sub.name]
        sub_all = set(sub.brick_ids)
        for brick_id in step.brick_ids:
            if not (self.supports[brick_id] & sub_all) <= (seen | step_set):
                self.violations.append(
                    f"step {step.index}: sub support after dependent"
                )
            if self.blockers[brick_id] & seen:
                self.violations.append(
                    f"step {step.index}: sub vertically blocked insert"
                )
        seen |= step_set
        sub_layout = self.layout.subset(seen).translated(dz=sub.anchor_layer)
        result = analyze(sub_layout, self.config.solver)
        self._check_fragile_mark(
            step,
            press_ids=step.brick_ids,
            analysis_layout=sub_layout,
        )
        if result.stable != step.prefix_stable:
            self.violations.append(
                f"step {step.index}: sub prefix stability mismatch "
                f"(expected {step.prefix_stable}, analyzed {result.stable})"
            )

    def attach_step(self, step: BuildStep) -> None:
        """Check one attach step as a unit insertion onto the placed world."""
        sub = self.subs.get(step.attaches or "")
        if sub is None:
            self.violations.append(f"step {step.index}: unknown subassembly")
            return
        unit = set(sub.brick_ids)
        self.attach_counts[sub.name] += 1
        if self.sub_placed[sub.name] != unit:
            self.violations.append(
                f"step {step.index}: attach before subassembly complete"
            )
        if step.brick_ids:
            self.violations.append(
                f"step {step.index}: attach step must place no bricks"
            )
        if any(self.blockers[bid] & self.placed for bid in unit):
            self.violations.append(f"step {step.index}: attach unit vertically blocked")
        seated_layout = self.layout.subset(self.placed | unit)
        result = analyze(seated_layout, self.config.solver)
        self._check_fragile_mark(
            step,
            press_ids=tuple(sorted(unit)),
            analysis_layout=seated_layout,
        )
        floating = {
            bid
            for bid, score in result.scores.items()
            if bid in unit and not score.in_equilibrium
        }
        if result.stable != step.prefix_stable:
            self.violations.append(
                f"step {step.index}: attach stability mismatch "
                f"(expected {step.prefix_stable}, analyzed {result.stable})"
            )
        if not result.stable and floating == unit:
            self.violations.append(
                f"step {step.index}: attached subassembly has no seat"
            )
        self.placed |= unit
        if self.prefix_solver is not None:
            self.prefix_solver.commit(tuple(unit))

    def finish(self) -> None:
        """Whole-plan checks after the walk.

        ``plan.order`` counts sub-build bricks, so a plan missing its
        attach step still "covers" every brick while the emitted world
        holds only the main model (PR #17 review) — require each
        declared subassembly to attach exactly once and the final world
        to equal the layout.
        """
        for name, count in sorted(self.attach_counts.items()):
            if count != 1:
                self.violations.append(
                    f"subassembly {name} attached {count} time(s), expected 1"
                )
        if self.placed != set(self.layout.bricks):
            self.violations.append(
                "final assembly does not place every brick in the layout"
            )

    def main_step(self, step: BuildStep) -> None:
        """Check one ordinary step's supports, blockers, and verdict."""
        step_set = set(step.brick_ids)
        for brick_id in step.brick_ids:
            if not self.supports[brick_id] <= self.placed | step_set:
                self.violations.append(f"step {step.index}: support after dependent")
            if self.blockers[brick_id] & self.placed:
                self.violations.append(f"step {step.index}: vertically blocked insert")
        if self.prefix_solver is not None:
            result = self.prefix_solver.probe(step.brick_ids)
            self._check_fragile_mark(step, press_ids=step.brick_ids)
            self.prefix_solver.commit(step.brick_ids)
        else:
            result = self._analyzed(self.placed | step_set)
            self._check_fragile_mark(step, press_ids=step.brick_ids)
        if result.stable != step.prefix_stable:
            self.violations.append(
                f"step {step.index}: prefix stability mismatch "
                f"(expected {step.prefix_stable}, analyzed {result.stable})"
            )
        self.placed |= step_set

    def _check_fragile_mark(
        self,
        step: BuildStep,
        *,
        press_ids: tuple[int, ...],
        analysis_layout: Layout | None = None,
    ) -> None:
        """Re-derive the press verdict for steps the sequencer flagged.

        One-directional on purpose: a fragile mark must reproduce as
        press-unstable (the flag never lies), but unflagged steps are
        not press-verified — rescue and band orderings are never
        press-scanned during sequencing, so demanding their absence of
        the flag re-derive would fail valid plans. ``analysis_layout``
        supplies the exact non-main frame: table-grounded for a
        sub-build, or the placed world plus the seated attachment unit.
        When the check is off, no press LP runs at all (verify stays
        byte-identical).
        """
        if not (
            self.config.insertion_check
            and step.insertion_fragile
            and step.prefix_stable
            and press_ids
        ):
            return
        if analysis_layout is not None:
            press = analyze(
                analysis_layout,
                self.config.solver,
                extra_masses=dict.fromkeys(press_ids, self.config.insertion_mass_kg),
            )
        elif self.prefix_solver is not None:
            press = self.prefix_solver.press_probe(
                press_ids, self.config.insertion_mass_kg
            )
        else:
            press = analyze(
                self.layout.subset(self.placed | set(press_ids)),
                self.config.solver,
                extra_masses=dict.fromkeys(press_ids, self.config.insertion_mass_kg),
            )
        if press.stable:
            self.violations.append(
                f"step {step.index}: flagged insertion-fragile but the "
                f"press verdict is stable"
            )
