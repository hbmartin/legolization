"""Sequence-similarity and plan-quality metrics."""

import pytest

from legolization.catalog import default_catalog
from legolization.instructions import (
    BillOfMaterials,
    BuildStep,
    InstructionPlan,
    plan_instructions,
)
from legolization.instructions.metrics import (
    SequenceSimilarity,
    plan_quality,
    sequence_similarity,
)
from legolization.layout import Layout


def test_identity_is_perfect():
    order = [3, 1, 4, 15, 9, 2]
    assert sequence_similarity(order, order) == SequenceSimilarity(
        kendall_tau=1.0, rlsd=0.0
    )


def test_reversal_hits_the_closed_forms():
    order = list(range(10))
    result = sequence_similarity(order, list(reversed(order)))
    assert result.kendall_tau == -1.0
    # The paper's normalizer caps a full reversal at 2/3, not 1.0.
    assert result.rlsd == pytest.approx(2 / 3)


def test_single_swap_is_nearly_perfect():
    result = sequence_similarity([1, 2, 3, 4], [2, 1, 3, 4])
    assert result.kendall_tau == pytest.approx(1 - 2 / 6)
    assert 0 < result.rlsd < 0.2


def test_mismatched_ids_raise():
    with pytest.raises(ValueError, match="permutations of the same ids"):
        sequence_similarity([1, 2, 3], [1, 2, 4])
    with pytest.raises(ValueError, match="permutations of the same ids"):
        sequence_similarity([1, 2, 2], [2, 2, 1])


def test_tiny_sequences_are_trivially_similar():
    assert sequence_similarity([], []) == SequenceSimilarity(kendall_tau=1.0, rlsd=0.0)
    assert sequence_similarity([7], [7]) == SequenceSimilarity(
        kendall_tau=1.0, rlsd=0.0
    )


def test_plan_quality_reads_stored_verdicts():
    plan = InstructionPlan(
        steps=(
            BuildStep(
                index=1, brick_ids=(1,), prefix_stable=True, prefix_max_score=0.1
            ),
            BuildStep(
                index=2, brick_ids=(2,), prefix_stable=False, prefix_max_score=1.0
            ),
            BuildStep(
                index=3, brick_ids=(3,), prefix_stable=True, prefix_max_score=0.4
            ),
        ),
        warnings=(),
        bom=BillOfMaterials(total=(), per_step=()),
    )
    quality = plan_quality(plan)
    assert quality.step_count == 3
    assert quality.unstable_steps == 1
    assert quality.max_prefix_score == 1.0
    assert quality.mean_prefix_score == pytest.approx(0.5)


def test_plan_quality_of_empty_plan():
    plan = InstructionPlan(
        steps=(), warnings=(), bom=BillOfMaterials(total=(), per_step=())
    )
    assert plan_quality(plan).step_count == 0


def test_smart_plan_tracks_bottom_up_raster_order():
    layout = Layout(catalog=default_catalog())
    for layer in (0, 3):
        for x in range(0, 12, 2):
            layout.add("brick_1x2", x, 0, layer, 0, 4)
    plan = plan_instructions(layout)
    raster = tuple(
        sorted(
            plan.order,
            key=lambda bid: (
                layout.bricks[bid].layer,
                layout.bricks[bid].y,
                layout.bricks[bid].x,
                bid,
            ),
        )
    )
    result = sequence_similarity(raster, plan.order)
    assert result.kendall_tau > 0.5
