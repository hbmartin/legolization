"""Sequence-quality metrics from the graph-transformer ASP paper (Ma et al.).

Two families: similarity of a build order to a reference order (Kendall's τ
over precedence pairs, plus the paper's Regularized Location Square
Deviation), and an LP-free quality report over a plan's stored per-step
stability verdicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from legolization.instructions.sequencer import InstructionPlan


@dataclass(frozen=True, slots=True)
class SequenceSimilarity:
    """How closely a candidate build order tracks a reference order."""

    kendall_tau: float
    rlsd: float


@dataclass(frozen=True, slots=True)
class PlanQuality:
    """Aggregate stability picture of a plan, from stored step verdicts."""

    step_count: int
    unstable_steps: int
    max_prefix_score: float
    mean_prefix_score: float
    subassembly_count: int = 0
    attach_steps: int = 0


def sequence_similarity(
    reference: Sequence[int],
    candidate: Sequence[int],
) -> SequenceSimilarity:
    """Score ``candidate`` against ``reference`` (permutations of one set).

    ``kendall_tau`` ∈ [-1, 1] counts concordant minus discordant precedence
    pairs over the standard ``n(n-1)/2`` denominator (the paper prints
    ``n(n-2)/2``, an obvious typo — with all ranks distinct, concordant plus
    discordant pairs total ``n(n-1)/2``). ``rlsd`` ∈ [0, ~2/3] is the paper's
    Regularized Location Square Deviation: mean squared index displacement
    normalized by ``(n+1)(n-1)/2`` — note the paper's normalizer caps a full
    reversal at 2/3, not 1.0.
    """
    if sorted(reference) != sorted(candidate) or len(set(reference)) != len(reference):
        msg = "reference and candidate must be permutations of the same ids"
        raise ValueError(msg)
    n = len(reference)
    if n < 2:
        return SequenceSimilarity(kendall_tau=1.0, rlsd=0.0)
    position = {brick_id: index for index, brick_id in enumerate(candidate)}
    concordant = 0
    discordant = 0
    for earlier in range(n):
        for later in range(earlier + 1, n):
            if position[reference[earlier]] < position[reference[later]]:
                concordant += 1
            else:
                discordant += 1
    pairs = n * (n - 1) / 2
    displacement = sum(
        (index - position[brick_id]) ** 2 for index, brick_id in enumerate(reference)
    )
    return SequenceSimilarity(
        kendall_tau=(concordant - discordant) / pairs,
        rlsd=(displacement / n) / ((n + 1) * (n - 1) / 2),
    )


def plan_quality(plan: InstructionPlan) -> PlanQuality:
    """Summarize the plan's stability path without re-running any LP."""
    if not plan.steps:
        return PlanQuality(
            step_count=0,
            unstable_steps=0,
            max_prefix_score=0.0,
            mean_prefix_score=0.0,
        )
    scores = [step.prefix_max_score for step in plan.steps]
    return PlanQuality(
        step_count=len(plan.steps),
        unstable_steps=sum(1 for step in plan.steps if not step.prefix_stable),
        max_prefix_score=max(scores),
        mean_prefix_score=sum(scores) / len(scores),
        subassembly_count=len(plan.subassemblies),
        attach_steps=sum(1 for step in plan.steps if step.attaches is not None),
    )
