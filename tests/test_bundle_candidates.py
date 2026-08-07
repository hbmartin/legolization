"""Candidate plans, colour-variant dedup, and winner selection."""

from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pytest

from legolization.bundle.candidates import (
    BALANCED_BUDGET_S,
    FAST_BUDGET_S,
    VARIANTS,
    dedup_variants,
    plan_candidates,
)
from legolization.bundle.selection import (
    BundleCandidate,
    select_bundle_winner,
)
from legolization.compare import CandidateMetrics
from legolization.errors import ConfigurationError
from legolization.grid import VoxelGrid
from legolization.placement.global_exact import preflight_reason
from legolization.placement.registry import strategy_names

HARD = VARIANTS[0]
ALL_NAMES = tuple(variant.name for variant in VARIANTS)


def _grid(codes: np.ndarray) -> VoxelGrid:
    return VoxelGrid(codes=codes.astype(np.int16))


def _metrics(  # noqa: PLR0913 - one knob per selection axis
    *,
    buildable: bool = True,
    objective: float = 1.0,
    capacity: float = 1.0,
    bricks: int = 10,
    components: int = 1,
    max_score: float = 0.5,
    colour_error: float = 0.0,
) -> CandidateMetrics:
    return CandidateMetrics(
        buildable=buildable,
        stable=buildable,
        component_count=components,
        floating_count=0,
        objective_total=objective,
        maximin_feasible=True,
        maximin_capacity=capacity,
        max_score=max_score,
        min_capacity=capacity,
        brick_count=bricks,
        mass_g=10.0,
        step_count=3,
        cost=objective,
        aesthetics=0.0,
        colour_error=colour_error,
        perpendicularity=0.0,
        symmetry=0.0,
    )


def _candidate(
    strategy: str,
    *,
    seed: int = 0,
    variant: str = "hard",
    objective: float = 1.0,
    selection_objective: float | None = None,
    **metric_kwargs,
) -> BundleCandidate:
    metrics = _metrics(objective=objective, **metric_kwargs)
    return BundleCandidate(
        strategy=strategy,
        seed=seed,
        variant=variant,
        status="ok",
        seconds=1.0,
        metrics=metrics,
        selection_objective=(
            selection_objective if selection_objective is not None else objective
        ),
        cross_colour_error=metrics.colour_error,
    )


def test_fast_plan_matches_spec():
    plan = plan_candidates(
        quality="fast",
        variants=(HARD,),
        collapsed={},
        exact_skip_reason=None,
    )
    assert [spec.to_dict() for spec in plan.specs] == [
        {"strategy": "greedy", "seed": 0, "variant": "hard"}
    ]
    assert plan.time_budget_s == FAST_BUDGET_S
    assert plan.exact_included is False


def test_balanced_plan_matches_spec():
    plan = plan_candidates(
        quality="balanced",
        variants=VARIANTS,
        collapsed={},
        exact_skip_reason=None,
    )
    heuristics = strategy_names()
    expected = len(heuristics) * len(VARIANTS) + 1
    assert len(plan.specs) == expected
    assert plan.time_budget_s == BALANCED_BUDGET_S
    assert plan.exact_included is True
    exact = [spec for spec in plan.specs if spec.strategy == "global-exact"]
    assert [spec.seed for spec in exact] == [0]
    assert {spec.seed for spec in plan.specs} == {0}


def test_exhaustive_plan_needs_duration_and_uses_three_seeds():
    with pytest.raises(ConfigurationError, match="duration"):
        plan_candidates(
            quality="exhaustive",
            variants=(HARD,),
            collapsed={},
            exact_skip_reason=None,
        )
    plan = plan_candidates(
        quality="exhaustive",
        variants=(HARD,),
        collapsed={},
        exact_skip_reason=None,
        duration_s=300.0,
    )
    assert {spec.seed for spec in plan.specs if spec.strategy != "global-exact"} == {
        0,
        1,
        2,
    }
    assert plan.time_budget_s == 300.0
    assert plan.exact_included is True


def test_preflight_ineligible_exact_is_skipped_with_reason():
    codes = np.full((10, 10, 10), 4)
    reason = preflight_reason(_grid(codes), max_cells=256)
    assert reason is not None
    assert "exact cap is 256" in reason
    plan = plan_candidates(
        quality="balanced",
        variants=(HARD,),
        collapsed={},
        exact_skip_reason=reason,
    )
    assert plan.exact_included is False
    assert plan.exact_skip_reason == reason
    assert all(spec.strategy != "global-exact" for spec in plan.specs)


def test_preflight_eligible_grid_has_no_reason():
    codes = np.full((2, 2, 2), 4)
    assert preflight_reason(_grid(codes), max_cells=256) is None


def test_dedup_collapses_single_colour_targets():
    codes = np.where(np.ones((3, 3, 2), dtype=bool), 4, -1)
    grid = _grid(codes)
    variants, collapsed = dedup_variants(
        {"hard": grid, "soft": grid, "soft-dither": grid}
    )
    assert [variant.name for variant in variants] == ["hard"]
    assert collapsed == {"soft": "hard", "soft-dither": "hard"}


def test_dedup_collapses_identical_dither_targets():
    codes = np.full((3, 3, 2), -1)
    codes[0] = 4
    codes[1] = 15
    codes[2] = 4
    grid = _grid(codes)
    variants, collapsed = dedup_variants(
        {"hard": grid, "soft": grid, "soft-dither": grid}
    )
    assert [variant.name for variant in variants] == ["hard", "soft"]
    assert collapsed == {"soft-dither": "soft"}


def test_dedup_keeps_distinct_dither_targets():
    codes = np.full((3, 3, 2), -1)
    codes[0] = 4
    codes[1] = 15
    dithered = codes.copy()
    dithered[2] = 4
    variants, collapsed = dedup_variants(
        {
            "hard": _grid(codes),
            "soft": _grid(codes),
            "soft-dither": _grid(dithered),
        }
    )
    assert [variant.name for variant in variants] == list(ALL_NAMES)
    assert collapsed == {}


def test_selection_gates_on_buildable_then_canonical_objective():
    selection = select_bundle_winner(
        [
            _candidate("greedy", objective=0.5, buildable=False),
            _candidate("bond", objective=2.0),
            _candidate("luo", objective=1.0),
        ]
    )
    assert selection.winner is not None
    assert selection.winner.strategy == "luo"
    assert "buildable" in selection.reason


def test_selection_uses_cross_variant_objective_not_self_score():
    dither_cheat = _candidate(
        "greedy",
        variant="soft-dither",
        objective=0.1,
        selection_objective=3.0,
    )
    honest = _candidate("greedy", variant="hard", objective=1.0)
    selection = select_bundle_winner([dither_cheat, honest])
    assert selection.winner is not None
    assert selection.winner.variant == "hard"


def test_selection_tie_breaks_are_deterministic():
    tie_a = _candidate("bond", variant="soft", objective=1.0, capacity=2.0)
    tie_b = _candidate("bond", variant="hard", objective=1.0, capacity=2.0)
    selection = select_bundle_winner([tie_a, tie_b])
    assert selection.winner is not None
    assert selection.winner.variant == "hard"


def test_selection_least_bad_when_nothing_buildable():
    selection = select_bundle_winner(
        [
            _candidate("greedy", buildable=False, components=3, max_score=0.9),
            _candidate("bond", buildable=False, components=2, max_score=0.7),
        ]
    )
    assert selection.winner is not None
    assert selection.winner.strategy == "bond"
    assert "least-bad" in selection.reason


def test_candidate_payload_round_trip():
    original = _candidate("greedy", variant="soft", objective=1.5)
    assert original.metrics is not None
    payload = {
        "strategy": original.strategy,
        "seed": original.seed,
        "variant": original.variant,
        "status": original.status,
        "seconds": original.seconds,
        "error": None,
        "metrics": asdict(original.metrics),
        "selection_objective": original.selection_objective,
        "cross_colour_error": original.cross_colour_error,
    }
    restored = BundleCandidate.from_payload(payload)
    assert restored == original
