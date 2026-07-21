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
    for below_id, above_id in graph.support_edges():
        if below_id != GROUND_ID:
            supports[above_id].add(below_id)
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
        layout, sequencing_config, chunks, supports, blockers, blocks
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


def _sequence(  # noqa: PLR0913, PLR0915, C901 - the loop owns sequencing state
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
    cache: dict[frozenset[int], StabilityResult] = {}
    warm_keys: set[frozenset[int]] = set()
    prefix_solver: PrefixSolver | None = None
    centroids = [chunk_centroid(layout, chunk) for _, chunk in chunks]
    previous_centroid: tuple[float, float] | None = None

    def emit(
        chunk: tuple[int, ...],
        *,
        stable: bool,
        score: float,
        fragile: bool = False,
    ) -> None:
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

    def disable_warm_engine() -> None:
        """Rescue/beam/band paths float-order scores: legacy numbers only."""
        nonlocal prefix_solver
        prefix_solver = None
        for key in warm_keys:
            cache.pop(key, None)
        warm_keys.clear()

    def emit_verdicts(verdicts: list[ChunkVerdict]) -> None:
        for verdict in verdicts:
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
            # Rescue/band orderings are fixed by their own solvers, so
            # fragility here is flag-only (never avoidance): the mark
            # keeps the plan truthful where the ready loop never scanned.
            fragile = (
                config.insertion_check
                and verdict.stable
                and not press_prefix(verdict.chunk).stable
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
        if chosen is None and best_fragile is not None:
            # Fragility never triggers rescue and never blocks acceptance:
            # the whole window is press-fragile, so take the least-fragile
            # statically-stable candidate with a warning.
            press_score, position, static_score = best_fragile
            _, chunk = chunks[position]
            warnings.append(
                f"step {len(steps) + 1}: insertion-fragile "
                f"(press score {press_score:.2f}); "
                "press bricks home gently and support the joint"
            )
            emit(chunk, stable=True, score=static_score, fragile=True)
            if prefix_solver is not None:
                prefix_solver.commit(chunk)
            chosen = position
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
        previous_centroid = centroids[chosen]
        pending.remove(chosen)
    return steps, warnings


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
