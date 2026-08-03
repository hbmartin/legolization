"""Cold final certification for expanded assembly instruction plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from legolization.instructions.sequencer import verify_plan
from legolization.stability.solver import analyze

if TYPE_CHECKING:
    from legolization.instructions.sequencer import (
        BuildStep,
        InstructionPlan,
        InstructionsConfig,
        Subassembly,
    )
    from legolization.layout import Layout


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionFailureTrace:
    """Earliest cold-physics failure in an expanded action order."""

    step_index: int
    action: str
    occurrence_ids: tuple[int, ...]
    unstable_ids: tuple[int, ...]
    max_score: float


@dataclass(frozen=True, slots=True, kw_only=True)
class InstructionCertification:
    """Final structural and cold-physics verdict for an instruction plan."""

    violations: tuple[str, ...]
    cold_prefix_count: int
    earliest_failure: ActionFailureTrace | None

    @property
    def valid(self) -> bool:
        """Return whether the plan's declared verdicts match every check."""
        return not self.violations


def certify_instructions(
    layout: Layout,
    plan: InstructionPlan,
    *,
    config: InstructionsConfig,
) -> InstructionCertification:
    """Verify structure, insertion, and every expanded prefix with cold LPs."""
    violations = verify_plan(layout, plan, config=config)
    subassemblies = {item.name: item for item in plan.subassemblies}
    sub_placed = {name: set() for name in subassemblies}
    placed: set[int] = set()
    earliest: ActionFailureTrace | None = None
    cold_count = 0
    for step in plan.steps:
        analysis_layout, action, occurrence_ids = _step_layout(
            layout,
            step,
            placed=placed,
            subassemblies=subassemblies,
            sub_placed=sub_placed,
        )
        result = analyze(analysis_layout, config.solver)
        cold_count += 1
        if result.stable != step.prefix_stable:
            violations.append(
                f"step {step.index}: cold prefix stability mismatch "
                f"(declared {step.prefix_stable}, analyzed {result.stable})"
            )
        if earliest is None and not result.stable:
            earliest = ActionFailureTrace(
                step_index=step.index,
                action=action,
                occurrence_ids=occurrence_ids,
                unstable_ids=tuple(sorted(result.unstable_ids)),
                max_score=result.max_score,
            )
    return InstructionCertification(
        violations=tuple(dict.fromkeys(violations)),
        cold_prefix_count=cold_count,
        earliest_failure=earliest,
    )


def _step_layout(
    layout: Layout,
    step: BuildStep,
    *,
    placed: set[int],
    subassemblies: dict[str, Subassembly],
    sub_placed: dict[str, set[int]],
) -> tuple[Layout, str, tuple[int, ...]]:
    if step.submodel is not None:
        sub = subassemblies[step.submodel]
        seen = sub_placed[step.submodel]
        seen.update(step.brick_ids)
        return (
            layout.subset(seen).translated(dz=sub.anchor_layer),
            "subassembly-build",
            step.brick_ids,
        )
    if step.attaches is not None:
        sub = subassemblies[step.attaches]
        occurrence_ids = sub.brick_ids
        placed.update(occurrence_ids)
        return layout.subset(placed), "subassembly-attach", occurrence_ids
    placed.update(step.brick_ids)
    return layout.subset(placed), "place", step.brick_ids


__all__ = [
    "ActionFailureTrace",
    "InstructionCertification",
    "certify_instructions",
]
