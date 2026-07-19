"""Assembly-by-disassembly rescue: coverage, stability path, determinism."""

from pathlib import Path

import pytest

from legolization.catalog import default_catalog
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.instructions import (
    InstructionPlan,
    InstructionsConfig,
    InstructionsError,
    plan_instructions,
    verify_plan,
)
from legolization.instructions.blocking import vertical_blockers
from legolization.instructions.chunking import chunk_bands, mirror_pairs
from legolization.instructions.metrics import plan_quality
from legolization.instructions.search import disassembly_order
from legolization.layout import Layout
from legolization.pipeline import PipelineConfig, load_grid, run
from legolization.stability import SolverConfig, StabilityResult

_EXAMPLES = Path(__file__).parent.parent / "data" / "examples"


@pytest.fixture(scope="module")
def hollow_pyramid() -> tuple[Layout, InstructionPlan]:
    """Build the shipped pyramid at default settings.

    Its hollow shell has genuinely unorderable overhang steps, so the
    greedy pass degrades and the disassembly rescue plans the remainder.
    """
    config = PipelineConfig(seed=0)
    result = run(load_grid(_EXAMPLES / "pyramid.npy", config), config)
    assert result.plan is not None
    return result.layout, result.plan


def _sequencing_inputs(
    layout: Layout,
    config: InstructionsConfig,
) -> tuple[
    list[tuple[int, tuple[int, ...]]],
    dict[int, set[int]],
    dict[int, frozenset[int]],
]:
    graph = ConnectionGraph.from_layout(layout)
    supports = {brick_id: set() for brick_id in layout.bricks}
    for below_id, above_id in graph.support_edges():
        if below_id != GROUND_ID:
            supports[above_id].add(below_id)
    blockers = vertical_blockers(layout)
    chunks = chunk_bands(layout, config=config, pairs=mirror_pairs(layout))
    return chunks, supports, blockers


def test_full_disassembly_covers_all_chunks_stably() -> None:
    # Solid ground row + a second storey: every forward prefix is stable,
    # so a full-layout disassembly must find an all-stable order.
    layout = _two_storey_layout()
    config = InstructionsConfig(target_step_size=4, rotstep=False)
    chunks, supports, blockers = _sequencing_inputs(layout, config)
    verdicts = disassembly_order(
        layout,
        placed=frozenset(),
        chunks=chunks,
        supports=supports,
        blockers=blockers,
        config=config,
        cache={},
    )
    ordered_ids = [brick_id for verdict in verdicts for brick_id in verdict.chunk]
    assert sorted(ordered_ids) == sorted(layout.bricks)
    assert all(verdict.stable for verdict in verdicts)

    # Forward feasibility: supports precede dependents, inserts unblocked.
    placed: set[int] = set()
    for verdict in verdicts:
        chunk_set = set(verdict.chunk)
        for brick_id in verdict.chunk:
            assert supports[brick_id] <= placed | chunk_set
            assert not (blockers[brick_id] & placed)
        placed |= chunk_set


def test_rescue_plan_verifies_and_warns(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    layout, plan = hollow_pyramid
    assert verify_plan(layout, plan) == []
    assert sorted(plan.order) == sorted(layout.bricks)
    # The hollow shell's clamped overhangs cannot be ordered stable.
    assert any("prefix unstable" in warning for warning in plan.warnings)
    assert plan_quality(plan).unstable_steps >= 1


def test_rescue_is_deterministic(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    layout, plan = hollow_pyramid
    again = plan_instructions(layout, config=InstructionsConfig())
    assert [step.brick_ids for step in again.steps] == [
        step.brick_ids for step in plan.steps
    ]


def test_rescue_never_beats_band_fallback_on_unstable_steps(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    layout, rescue_plan = hollow_pyramid
    band_plan = plan_instructions(layout, config=InstructionsConfig(fallback="band"))
    assert verify_plan(layout, band_plan) == []
    assert (
        plan_quality(rescue_plan).unstable_steps
        <= plan_quality(band_plan).unstable_steps
    )


def test_strict_mode_raises_through_the_rescue(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    layout, _ = hollow_pyramid
    with pytest.raises(InstructionsError, match="no stable ordering"):
        plan_instructions(layout, config=InstructionsConfig(stability_policy="strict"))


def test_band_fallback_still_warns_per_step(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    layout, _ = hollow_pyramid
    plan = plan_instructions(layout, config=InstructionsConfig(fallback="band"))
    assert any("prefix unstable" in warning for warning in plan.warnings)
    assert any(not step.prefix_stable for step in plan.steps)


def test_configs_share_the_greedy_happy_path(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    # Until the first degradation, rescue and band configs agree exactly.
    layout, rescue_plan = hollow_pyramid
    band_plan = plan_instructions(layout, config=InstructionsConfig(fallback="band"))
    first_unstable = next(
        index for index, step in enumerate(rescue_plan.steps) if not step.prefix_stable
    )
    prefix = slice(0, first_unstable)
    assert [step.brick_ids for step in rescue_plan.steps[prefix]] == [
        step.brick_ids for step in band_plan.steps[prefix]
    ]


def test_plan_quality_reflects_the_rescue_path(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    _, plan = hollow_pyramid
    quality = plan_quality(plan)
    assert quality.step_count == len(plan.steps)
    assert quality.max_prefix_score >= 1.0  # the unorderable steps score 1.0


def _two_storey_layout() -> Layout:
    layout = Layout(catalog=default_catalog())
    for x in range(0, 8, 2):
        layout.add("brick_1x2", x, 0, 0, 0, 4)
    for x in range(0, 8, 2):
        layout.add("brick_1x2", x, 0, 3, 0, 4)
    return layout


def test_beam_search_verifies_and_is_deterministic() -> None:
    layout = _two_storey_layout()
    config = InstructionsConfig(search="beam", target_step_size=4, rotstep=False)
    plan = plan_instructions(layout, config=config)
    assert verify_plan(layout, plan, config=config) == []
    assert sorted(plan.order) == sorted(layout.bricks)
    assert all(step.prefix_stable for step in plan.steps)
    again = plan_instructions(layout, config=config)
    assert [step.brick_ids for step in again.steps] == [
        step.brick_ids for step in plan.steps
    ]


def test_beam_never_does_worse_than_greedy_rescue(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    layout, rescue_plan = hollow_pyramid
    config = InstructionsConfig(search="beam")
    beam_plan = plan_instructions(layout, config=config)
    assert verify_plan(layout, beam_plan, config=config) == []
    assert sorted(beam_plan.order) == sorted(layout.bricks)
    assert (
        plan_quality(beam_plan).unstable_steps
        <= plan_quality(rescue_plan).unstable_steps
    )


def test_beam_respects_the_lp_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import legolization.instructions.search as search_module

    layout = _two_storey_layout()
    real_analyze = search_module.analyze
    calls = {"count": 0}

    def counting_analyze(
        target: Layout,
        config: SolverConfig | None = None,
        graph: ConnectionGraph | None = None,
    ) -> StabilityResult:
        calls["count"] += 1
        return real_analyze(target, config, graph)

    monkeypatch.setattr(search_module, "analyze", counting_analyze)
    budget = 3
    config = InstructionsConfig(
        search="beam", target_step_size=4, rotstep=False, lp_budget=budget
    )
    plan = plan_instructions(layout, config=config)
    assert verify_plan(layout, plan, config=config) == []
    # The cap is checked per depth; one degraded depth may add at most
    # beam_width more calls before the next check.
    assert calls["count"] <= budget + config.beam_width


def test_beam_strict_mode_raises_on_unorderable_models(
    hollow_pyramid: tuple[Layout, InstructionPlan],
) -> None:
    layout, _ = hollow_pyramid
    with pytest.raises(InstructionsError, match="no stable ordering"):
        plan_instructions(
            layout,
            config=InstructionsConfig(search="beam", stability_policy="strict"),
        )


def test_band_deadlock_honours_strict_policy(
    hollow_pyramid: tuple[Layout, InstructionPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import legolization.instructions.sequencer as sequencer_module

    layout, _ = hollow_pyramid

    def no_ready(*_args: object, **_kwargs: object) -> list[int]:
        return []

    monkeypatch.setattr(sequencer_module, "_gather_ready", no_ready)
    with pytest.raises(InstructionsError, match="no stable ordering"):
        plan_instructions(
            layout,
            config=InstructionsConfig(
                fallback="band",
                stability_policy="strict",
            ),
        )


def test_instructions_config_rejects_nonpositive_beam_width() -> None:
    with pytest.raises(ValueError, match="beam_width must be positive"):
        InstructionsConfig(beam_width=0)


def test_rescue_orders_underneath_decoration_before_support() -> None:
    # A decorative piece hanging BELOW a bridge deadlocks the greedy
    # pass (it is vertically blocked once the bridge is placed, and the
    # bridge is unsupported without it) - the rescue must order the
    # decoration BEFORE its overhang, identically on both engines, and
    # verify_plan must accept the result. This pins the ordering
    # semantics every rescue optimization has to preserve.
    from legolization.stability.solver import SolverConfig

    layout = Layout(catalog=default_catalog())
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    layout.add("brick_2x2", 4, 0, 0, 0, 4)
    layout.add("brick_2x2", 0, 0, 3, 0, 4)
    layout.add("brick_2x2", 4, 0, 3, 0, 4)
    bridge = layout.add("brick_1x6", 0, 0, 6, 0, 4)
    decoration = layout.add("brick_1x1", 2, 0, 3, 0, 14)

    orders = {}
    for engine in ("scipy", "highspy"):
        config = InstructionsConfig(rotstep=False, solver=SolverConfig(engine=engine))
        plan = plan_instructions(layout, config=config)
        assert verify_plan(layout, plan, config=config) == []
        order = plan.order
        assert order.index(decoration.brick_id) < order.index(bridge.brick_id)
        orders[engine] = order
    assert orders["scipy"] == orders["highspy"]
