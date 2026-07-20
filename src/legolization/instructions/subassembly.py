"""Subassembly extraction: rewrite floating clusters as table-built units.

``docs/unstable-prefix-report.md`` showed that every warned unstable step
in the corpus belongs to one class — a chunk whose only stud route to
ground arrives in a LATER band — and that no reordering fixes it. This
post-pass detects those persistent floating clusters in a finished plan
and rewrites them as SUBASSEMBLIES: their bricks are built as a separate
grounded-on-table sequence (all stable by construction) and seated onto
the main model in a single attach step.

Honest improvement model: the RBE has no rigid-body notion, so the
post-attach prefix is analyzed by the same LP as before — an attach onto
a weak seat still warns ("support while attaching"). What disappears are
the per-chunk floating warnings during the sub's construction: mushroom's
seventeen warned steps become two or three attach warnings.

The pass is a pure rewrite: any cluster that fails validation (no seat,
vertically blocked unit insertion, too small, over the cap) is simply
left as today's warned steps.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.instructions.blocking import vertical_blockers
from legolization.instructions.sequencer import (
    BuildStep,
    InstructionPlan,
    InstructionsConfig,
    Subassembly,
    plan_instructions,
)
from legolization.stability.prefix import PrefixSolver
from legolization.stability.solver import analyze

if TYPE_CHECKING:
    from collections.abc import Callable

    from legolization.layout import Layout

_UNSTABLE_WARNING_MARK = ": prefix unstable"
_FRAGILE_WARNING_MARK = ": insertion-fragile"


def extract_subassemblies(
    layout: Layout,
    plan: InstructionPlan,
    *,
    config: InstructionsConfig,
) -> InstructionPlan:
    """Rewrite persistent floating clusters as subassemblies (or no-op)."""
    graph = ConnectionGraph.from_layout(layout)
    stud_adjacent, grounded = _stud_graph(graph, layout)
    blockers = vertical_blockers(layout)
    floating_per_step = _floating_walk(plan, stud_adjacent, grounded)
    clusters = _find_clusters(plan, floating_per_step, stud_adjacent, config=config)
    clusters = _validate_clusters(plan, clusters, blockers, stud_adjacent, grounded)
    clusters = _cap_clusters(clusters, floating_per_step, config=config)
    if not clusters:
        return plan
    return _rewrite(layout, plan, clusters, config=config)


class _Cluster:
    """One accepted subassembly candidate."""

    __slots__ = ("attach_step", "bricks", "first_float_step", "name")

    def __init__(
        self,
        bricks: frozenset[int],
        attach_step: int,
        first_float_step: int,
    ) -> None:
        self.bricks = bricks
        self.attach_step = attach_step  # 0-based index into plan.steps
        self.first_float_step = first_float_step
        self.name = ""


def _stud_graph(
    graph: ConnectionGraph,
    layout: Layout,
) -> tuple[dict[int, set[int]], frozenset[int]]:
    adjacent: dict[int, set[int]] = {bid: set() for bid in layout.bricks}
    for below, above in graph.support_edges():
        if below != GROUND_ID:
            adjacent[below].add(above)
            adjacent[above].add(below)
    return adjacent, frozenset(graph.grounded_ids)


def _reach_floating(
    subset: set[int],
    grounded: frozenset[int],
    adjacent: dict[int, set[int]],
) -> set[int]:
    reached = {bid for bid in grounded if bid in subset}
    stack = list(reached)
    while stack:
        current = stack.pop()
        for neighbour in adjacent[current]:
            if neighbour in subset and neighbour not in reached:
                reached.add(neighbour)
                stack.append(neighbour)
    return subset - reached


def _floating_walk(
    plan: InstructionPlan,
    adjacent: dict[int, set[int]],
    grounded: frozenset[int],
) -> list[set[int]]:
    """Floating brick set after each step (graph-only; no LP)."""
    placed: set[int] = set()
    result: list[set[int]] = []
    for step in plan.steps:
        placed |= set(step.brick_ids)
        result.append(_reach_floating(set(placed), grounded, adjacent))
    return result


def _find_clusters(
    plan: InstructionPlan,
    floating_per_step: list[set[int]],
    adjacent: dict[int, set[int]],
    *,
    config: InstructionsConfig,
) -> list[_Cluster]:
    """Find persistent floating runs and their placement-window components."""
    clusters: list[_Cluster] = []
    i = 0
    n = len(plan.steps)
    while i < n:
        if not floating_per_step[i]:
            i += 1
            continue
        start = i
        while i < n and floating_per_step[i]:
            i += 1
        if i >= n:
            break  # final model itself floats: unbuildable, leave alone
        ground_step = i  # first step whose prefix has no floaters
        window: set[int] = set()
        for step in plan.steps[start : ground_step + 1]:
            window |= set(step.brick_ids)
        ever_floating: set[int] = set()
        for floats in floating_per_step[start:ground_step]:
            ever_floating |= floats
        for component in _components(window, adjacent):
            if not component & ever_floating:
                continue
            if len(component) < config.min_sub_bricks:
                continue
            first_float = next(
                idx
                for idx in range(start, ground_step)
                if component & floating_per_step[idx]
            )
            clusters.append(_Cluster(frozenset(component), ground_step, first_float))
        i = ground_step + 1
    return clusters


def _components(
    subset: set[int],
    adjacent: dict[int, set[int]],
) -> list[set[int]]:
    remaining = set(subset)
    out: list[set[int]] = []
    while remaining:
        seed = min(remaining)  # deterministic
        stack = [seed]
        component = {seed}
        remaining.discard(seed)
        while stack:
            current = stack.pop()
            for neighbour in adjacent[current]:
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    component.add(neighbour)
                    stack.append(neighbour)
        out.append(component)
    return out


def _validate_clusters(
    plan: InstructionPlan,
    clusters: list[_Cluster],
    blockers: dict[int, frozenset[int]],
    stud_adjacent: dict[int, set[int]],
    grounded: frozenset[int],
) -> list[_Cluster]:
    accepted: list[_Cluster] = []
    for cluster in clusters:
        prior: set[int] = set()
        for step in plan.steps[: cluster.attach_step + 1]:
            prior |= set(step.brick_ids)
        prior -= cluster.bricks
        combined = prior | cluster.bricks
        still_floating = _reach_floating(set(combined), grounded, stud_adjacent)
        if still_floating & cluster.bricks:
            continue  # no seat: the unit would not ground at attach time
        if any(blockers[bid] & prior for bid in cluster.bricks):
            continue  # something already placed blocks lowering the unit in
        accepted.append(cluster)
    return accepted


def _cap_clusters(
    clusters: list[_Cluster],
    floating_per_step: list[set[int]],
    *,
    config: InstructionsConfig,
) -> list[_Cluster]:
    ever_floating: set[int] = set()
    for floats in floating_per_step:
        ever_floating |= floats
    ranked = sorted(
        clusters,
        key=lambda c: (
            -len(c.bricks & ever_floating),
            c.first_float_step,
            min(c.bricks),
        ),
    )
    kept = ranked[: config.max_subassemblies]
    kept.sort(key=lambda c: c.attach_step)
    for i, cluster in enumerate(kept, start=1):
        cluster.name = f"sub-{i}"
    return kept


def _rewrite(
    layout: Layout,
    plan: InstructionPlan,
    clusters: list[_Cluster],
    *,
    config: InstructionsConfig,
) -> InstructionPlan:
    sub_bricks: set[int] = set()
    for cluster in clusters:
        sub_bricks |= cluster.bricks
    attach_at: dict[int, list[_Cluster]] = {}
    for cluster in clusters:
        attach_at.setdefault(cluster.attach_step, []).append(cluster)

    original_verdicts = _original_verdicts(plan)
    new_steps: list[BuildStep] = []
    warnings: list[str] = []
    subassemblies: list[Subassembly] = []
    placed_world: set[int] = set()
    # One warm walker over the final world order keeps the per-step
    # press re-derivation at bound-change cost instead of a cold LP per
    # kept step (suzanne-scale rewrites pay dozens of presses).
    world_press, world_commit = _press_walker(layout, config, placed_world)

    for index, step in enumerate(plan.steps):
        kept = tuple(bid for bid in step.brick_ids if bid not in sub_bricks)
        if kept:
            placed_world |= set(kept)
            key = frozenset(placed_world)
            verdict = original_verdicts.get(key)
            if verdict is None:
                result = analyze(layout.subset(key), config.solver)
                verdict = (result.stable, result.max_score)
            # The pre-rewrite press mark is stale: extraction changes
            # every kept step's prefix, so re-derive the press verdict
            # against the prefix this plan actually builds.
            fragile = config.insertion_check and verdict[0] and world_press(kept)
            world_commit(kept)
            new_steps.append(
                replace(
                    step,
                    brick_ids=kept,
                    prefix_stable=verdict[0],
                    prefix_max_score=verdict[1],
                    rotstep=None,
                    insertion_fragile=fragile,
                )
            )
        for cluster in attach_at.get(index, ()):
            _emit_subassembly(
                layout,
                cluster,
                new_steps,
                warnings,
                subassemblies,
                placed_world,
                config=config,
                world_press=world_press,
            )
            world_commit(tuple(sorted(cluster.bricks)))
            placed_world |= cluster.bricks

    renumbered = tuple(
        replace(step, index=i) for i, step in enumerate(new_steps, start=1)
    )
    warnings = _regenerate_warnings(plan, renumbered, warnings)
    return InstructionPlan(
        steps=renumbered,
        warnings=tuple(warnings),
        bom=plan.bom,
        subassemblies=tuple(subassemblies),
    )


def _press_walker(
    layout: Layout,
    config: InstructionsConfig,
    placed_world: set[int],
) -> tuple[Callable[[tuple[int, ...]], bool], Callable[[tuple[int, ...]], None]]:
    """Build (press, commit) callbacks over the final world order.

    One warm walker keeps the per-step press re-derivation at
    bound-change cost instead of a cold LP per kept step (suzanne-scale
    rewrites pay dozens of presses); the scipy engine falls back to the
    cold path against the live ``placed_world`` set.
    """
    walker = (
        PrefixSolver.create(layout, config.solver) if config.insertion_check else None
    )

    def world_press(bricks: tuple[int, ...]) -> bool:
        if walker is not None:
            return not walker.press_probe(bricks, config.insertion_mass_kg).stable
        return not analyze(
            layout.subset(placed_world | set(bricks)),
            config.solver,
            extra_masses=dict.fromkeys(bricks, config.insertion_mass_kg),
        ).stable

    def world_commit(bricks: tuple[int, ...]) -> None:
        if walker is not None:
            walker.commit(bricks)

    return world_press, world_commit


def _emit_subassembly(  # noqa: PLR0913 - the rewrite hands over all its state
    layout: Layout,
    cluster: _Cluster,
    new_steps: list[BuildStep],
    warnings: list[str],
    subassemblies: list[Subassembly],
    placed_world: set[int],
    *,
    config: InstructionsConfig,
    world_press: Callable[[tuple[int, ...]], bool] | None = None,
) -> None:
    anchor = min(layout.bricks[bid].layer for bid in cluster.bricks)
    sub_layout = layout.subset(cluster.bricks).translated(dz=anchor)
    sub_config = replace(
        config, subassemblies=False, rotstep=False, fallback="disassembly"
    )
    sub_plan = plan_instructions(sub_layout, config=sub_config)
    new_steps.extend(
        replace(sub_step, submodel=cluster.name, rotstep=None)
        for sub_step in sub_plan.steps
    )
    warnings.extend(
        f"subassembly {cluster.name}: {warning}"
        for warning in sub_plan.warnings
        if _UNSTABLE_WARNING_MARK in warning
    )
    combined = placed_world | cluster.bricks
    attach_result = analyze(layout.subset(combined), config.solver)
    # Whole-unit press: seating a finished subassembly presses its full
    # footprint at once — a check the step-by-step audit cannot see.
    attach_fragile = (
        config.insertion_check
        and attach_result.stable
        and world_press is not None
        and world_press(tuple(sorted(cluster.bricks)))
    )
    new_steps.append(
        BuildStep(
            index=0,  # renumbered later
            brick_ids=(),
            prefix_stable=attach_result.stable,
            prefix_max_score=attach_result.max_score,
            attaches=cluster.name,
            insertion_fragile=attach_fragile,
        )
    )
    subassemblies.append(
        Subassembly(
            name=cluster.name,
            brick_ids=tuple(bid for step in sub_plan.steps for bid in step.brick_ids),
            anchor_layer=anchor,
        )
    )


def _original_verdicts(
    plan: InstructionPlan,
) -> dict[frozenset[int], tuple[bool, float]]:
    """Original per-prefix verdicts keyed by cumulative brick set."""
    placed: set[int] = set()
    verdicts: dict[frozenset[int], tuple[bool, float]] = {}
    for step in plan.steps:
        placed |= set(step.brick_ids)
        verdicts[frozenset(placed)] = (step.prefix_stable, step.prefix_max_score)
    return verdicts


def _regenerate_warnings(
    original: InstructionPlan,
    steps: tuple[BuildStep, ...],
    sub_warnings: list[str],
) -> list[str]:
    """Rebuild step-numbered warnings from final verdicts; keep the rest."""
    kept = [
        warning
        for warning in original.warnings
        if _UNSTABLE_WARNING_MARK not in warning
        and _FRAGILE_WARNING_MARK not in warning
    ]
    regenerated: list[str] = []
    for step in steps:
        if step.insertion_fragile and step.submodel is None:
            what = (
                f"seating subassembly {step.attaches} "
                if step.attaches is not None
                else ""
            )
            regenerated.append(
                f"step {step.index}: insertion-fragile ({what}under press); "
                "press bricks home gently and support the joint"
            )
        if step.prefix_stable:
            continue
        if step.attaches is not None:
            regenerated.append(
                f"step {step.index}: attaching subassembly {step.attaches} "
                f"leaves the seat unstable "
                f"(score {step.prefix_max_score:.2f}); support while attaching"
            )
        elif step.submodel is None:
            regenerated.append(
                f"step {step.index}: prefix unstable "
                f"(score {step.prefix_max_score:.2f}); "
                "support the overhang by hand while building"
            )
    return kept + sub_warnings + regenerated
