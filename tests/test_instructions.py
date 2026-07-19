"""Instruction sequencing, chunking, blocking, BOM, and LDraw emission."""

import json
from dataclasses import replace

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import EMPTY, VoxelGrid
from legolization.instructions import (
    BuildStep,
    InstructionPlan,
    InstructionsConfig,
    InstructionsError,
    bill_of_materials,
    plan_instructions,
    verify_plan,
)
from legolization.instructions.blocking import vertical_blockers
from legolization.instructions.chunking import chunk_bands, mirror_pairs
from legolization.layout import Layout
from legolization.ldraw_out import model_lines
from legolization.pipeline import PipelineConfig, run
from legolization.stability import SolverConfig, StabilityResult


def _pyramid_layout() -> Layout:
    codes = np.full((6, 6, 3), EMPTY, dtype=np.int16)
    for z in range(3):
        lo, hi = z, 6 - z
        codes[lo:hi, lo:hi, z] = 4
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    result = run(grid, PipelineConfig(hollow=False, seed=0))
    return result.layout


def test_vertical_blockers_detects_overhangs():
    layout = Layout(catalog=default_catalog())
    base = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x4", 0, 0, 3, 0, 4)  # overhangs x = 1..3
    tucked = layout.add("plate_1x1", 2, 0, 1, 0, 4)  # under the overhang
    blockers = vertical_blockers(layout)
    assert blockers[base.brick_id] == frozenset({beam.brick_id})
    assert blockers[tucked.brick_id] == frozenset({beam.brick_id})
    assert blockers[beam.brick_id] == frozenset()


def test_chunk_sizes_respect_bounds():
    layout = Layout(catalog=default_catalog())
    for x in range(10):
        for y in range(3):
            layout.add("brick_1x1", x, y, 0, 0, 4)
    config = InstructionsConfig()
    chunks = chunk_bands(layout, config=config, pairs={})
    sizes = [len(chunk) for _, chunk in chunks]
    assert sum(sizes) == 30
    assert all(size <= config.max_step_size for size in sizes)
    assert all(size >= config.min_step_size for size in sizes)


def test_mirror_pairs_detected_and_co_stepped():
    layout = Layout(catalog=default_catalog())
    left = layout.add("brick_1x2", 0, 0, 0, 0, 4)
    right = layout.add("brick_1x2", 6, 0, 0, 0, 4)
    centre = layout.add("brick_2x2", 3, 0, 0, 0, 4)
    pairs = mirror_pairs(layout)
    assert pairs[left.brick_id] == right.brick_id
    assert pairs[centre.brick_id] == centre.brick_id

    chunks = chunk_bands(
        layout, config=InstructionsConfig(target_step_size=2), pairs=pairs
    )
    for _, chunk in chunks:
        if left.brick_id in chunk:
            assert right.brick_id in chunk


def test_self_paired_brick_counts_as_single_chunk_addition():
    layout = Layout(catalog=default_catalog())
    bricks = [layout.add("brick_1x1", 0, y, 0, 0, 4) for y in range(10)]
    pairs = mirror_pairs(layout)
    assert pairs == {brick.brick_id: brick.brick_id for brick in bricks}

    chunks = chunk_bands(
        layout,
        config=InstructionsConfig(
            target_step_size=10,
            max_step_size=10,
            min_step_size=1,
        ),
        pairs=pairs,
    )

    assert [len(chunk) for _, chunk in chunks] == [10]


def test_mirror_partner_never_overflows_max_step_size():
    layout = Layout(catalog=default_catalog())
    bricks = [layout.add("brick_1x1", x, 0, 0, 0, 4) for x in range(11)]
    pairs = {
        bricks[-2].brick_id: bricks[-1].brick_id,
        bricks[-1].brick_id: bricks[-2].brick_id,
    }

    chunks = chunk_bands(
        layout,
        config=InstructionsConfig(
            target_step_size=10,
            max_step_size=10,
            min_step_size=1,
        ),
        pairs=pairs,
    )

    assert all(len(chunk) <= 10 for _, chunk in chunks)
    assert any(set(pairs) <= set(chunk) for _, chunk in chunks)


def test_plan_covers_pyramid_with_stable_prefixes():
    layout = _pyramid_layout()
    plan = plan_instructions(layout)
    assert plan.warnings == ()
    assert all(step.prefix_stable for step in plan.steps)
    assert verify_plan(layout, plan) == []
    assert sorted(plan.order) == sorted(layout.bricks)


def test_plan_is_deterministic():
    layout = _pyramid_layout()
    first = plan_instructions(layout)
    second = plan_instructions(layout)
    assert [s.brick_ids for s in first.steps] == [s.brick_ids for s in second.steps]


def test_empty_layout_has_empty_instruction_plan():
    layout = Layout(catalog=default_catalog())

    plan = plan_instructions(layout)

    assert plan.steps == ()
    assert plan.warnings == ()
    assert plan.bom.brick_count == 0
    assert verify_plan(layout, plan) == []


def test_unbuildable_model_warns_not_crashes(bad_bridge):
    layout, _ = bad_bridge  # collapses even fully built
    plan = plan_instructions(layout)
    assert plan.warnings
    assert any(not step.prefix_stable for step in plan.steps)
    assert sorted(plan.order) == sorted(layout.bricks)


def test_strict_policy_raises_on_unstable_prefix(bad_bridge):
    layout, _ = bad_bridge
    with pytest.raises(InstructionsError, match="no stable ordering"):
        plan_instructions(layout, config=InstructionsConfig(stability_policy="strict"))


def test_verify_plan_checks_prefix_verdicts_even_with_warnings(bad_bridge):
    layout, _ = bad_bridge
    plan = plan_instructions(layout)
    assert plan.warnings
    first = plan.steps[0]
    incorrect = replace(
        plan,
        steps=(replace(first, prefix_stable=not first.prefix_stable), *plan.steps[1:]),
    )

    violations = verify_plan(layout, incorrect)

    assert any("prefix stability mismatch" in violation for violation in violations)


def test_verify_plan_flags_blocked_insertion():
    layout = Layout(catalog=default_catalog())
    base = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x4", 0, 0, 3, 0, 4)
    tucked = layout.add("plate_1x1", 2, 0, 1, 0, 4)
    bad_plan = InstructionPlan(
        steps=(
            BuildStep(
                index=1,
                brick_ids=(base.brick_id, beam.brick_id),
                prefix_stable=True,
                prefix_max_score=0.0,
            ),
            BuildStep(
                index=2,
                brick_ids=(tucked.brick_id,),  # must slide under the beam
                prefix_stable=True,
                prefix_max_score=0.0,
            ),
        ),
        warnings=(),
        bom=bill_of_materials(layout),
    )
    violations = verify_plan(layout, bad_plan)
    assert any("blocked" in violation for violation in violations)


def test_rotstep_emitted_for_offset_feature():
    # A grounded slab with a tall tower on its far corner: the tower steps
    # sit away from the model centre, behind the default view.
    layout = Layout(catalog=default_catalog())
    for x in range(8):
        for y in range(4):
            layout.add("brick_1x1", x, y, 0, 0, 4)
    for level in range(1, 4):
        layout.add("brick_2x2", 0, 2, 3 * level, 0, 4)
    plan = plan_instructions(layout)
    assert any(step.rotstep is not None for step in plan.steps)
    silent = plan_instructions(layout, config=InstructionsConfig(rotstep=False))
    assert all(step.rotstep is None for step in silent.steps)


def test_bom_totals_and_per_step_sums():
    layout = _pyramid_layout()
    plan = plan_instructions(layout)
    bom = plan.bom
    assert bom.brick_count == len(layout.bricks)
    assert bom.mass_g == pytest.approx(layout.total_mass_g(), abs=0.1)
    per_step_total = sum(
        entry.quantity for entries in bom.per_step for entry in entries
    )
    assert per_step_total == bom.brick_count
    payload = json.loads(bom.to_json(model_name="pyramid.ldr"))
    assert payload["model"] == "pyramid.ldr"
    assert payload["brick_count"] == bom.brick_count
    assert len(payload["steps"]) == len(plan.steps)
    assert "qty" in bom.to_text()


def test_model_lines_follow_plan_steps():
    layout = _pyramid_layout()
    plan = plan_instructions(layout)
    lines = list(model_lines(layout, name="p.ldr", plan=plan))
    assert lines.count("0 STEP") == len(plan.steps)
    piece_lines = [line for line in lines if line.startswith("1 ")]
    assert len(piece_lines) == len(layout.bricks)


def test_model_lines_legacy_path_unchanged():
    layout = _pyramid_layout()
    legacy = list(model_lines(layout, name="p.ldr"))
    layers = {brick.layer for brick in layout}
    assert legacy.count("0 STEP") == len(layers)
    assert not any(line.startswith("0 ROTSTEP") for line in legacy)


def test_cli_smart_steps_and_bom(tmp_path, capsys):
    from legolization.main import main

    codes = np.full((4, 4, 2), 4, dtype=np.int16)
    npy = tmp_path / "box.npy"
    np.save(npy, codes)
    out = tmp_path / "box.ldr"
    bom_path = tmp_path / "box-bom.json"
    code = main([str(npy), "-o", str(out), "--bom", str(bom_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "steps:" in captured.out
    payload = json.loads(bom_path.read_text())
    assert payload["brick_count"] > 0
    assert "0 STEP" in out.read_text()


def test_cli_layer_steps_keep_legacy_output(tmp_path):
    from legolization.main import main

    codes = np.full((4, 4, 2), 4, dtype=np.int16)
    npy = tmp_path / "box.npy"
    np.save(npy, codes)
    smart = tmp_path / "smart.ldr"
    layer = tmp_path / "layer.ldr"
    assert main([str(npy), "-o", str(smart)]) == 0
    assert main([str(npy), "-o", str(layer), "--steps", "layer"]) == 0
    assert "ROTSTEP" not in layer.read_text()
    # The legacy path steps once per plate layer.
    layout_layers = layer.read_text().count("0 STEP")
    assert layout_layers > 0


def _three_islands() -> tuple[Layout, list[int], list[int], list[int]]:
    """Three separated ground clusters that chunk one-to-one.

    Chunk order (by centroid y, then x) is A, B, C; spatially C is much
    closer to A (122 studs squared) than B is (400), so continuity should
    visit A, C, B while plain chunk order visits A, B, C.
    """
    layout = Layout(catalog=default_catalog())
    island_a = [layout.add("brick_1x1", x, 0, 0, 0, 4).brick_id for x in range(3)]
    island_b = [layout.add("brick_1x1", x, 0, 0, 0, 4).brick_id for x in range(20, 23)]
    island_c = [layout.add("brick_1x1", 0, y, 0, 0, 4).brick_id for y in range(10, 13)]
    return layout, island_a, island_b, island_c


def test_spatial_tiebreak_prefers_the_adjacent_chunk() -> None:
    layout, island_a, island_b, island_c = _three_islands()
    config = InstructionsConfig(target_step_size=3, max_step_size=3, rotstep=False)
    plan = plan_instructions(layout, config=config)
    assert verify_plan(layout, plan, config=config) == []
    assert set(plan.steps[0].brick_ids) == set(island_a)
    # All three chunks are equally stable; continuity picks the nearby
    # island over the distant one.
    assert set(plan.steps[1].brick_ids) == set(island_c)
    assert set(plan.steps[2].brick_ids) == set(island_b)


def test_spatial_tiebreak_off_restores_chunk_order() -> None:
    layout, island_a, island_b, island_c = _three_islands()
    config = InstructionsConfig(
        target_step_size=3,
        max_step_size=3,
        rotstep=False,
        spatial_tiebreak=False,
    )
    plan = plan_instructions(layout, config=config)
    assert verify_plan(layout, plan, config=config) == []
    assert [set(step.brick_ids) for step in plan.steps] == [
        set(island_a),
        set(island_b),
        set(island_c),
    ]


def test_happy_path_runs_one_lp_per_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import legolization.instructions.sequencer as sequencer_module

    layout, *_ = _three_islands()
    real_analyze = sequencer_module.analyze
    calls = {"count": 0}

    def counting_analyze(
        target: Layout,
        config: SolverConfig | None = None,
        graph: ConnectionGraph | None = None,
    ) -> StabilityResult:
        calls["count"] += 1
        return real_analyze(target, config, graph)

    monkeypatch.setattr(sequencer_module, "analyze", counting_analyze)
    config = InstructionsConfig(
        target_step_size=3,
        max_step_size=3,
        rotstep=False,
        solver=SolverConfig(engine="scipy"),
    )
    plan = plan_instructions(layout, config=config)
    # Ground-band chunks are all stable: the early exit takes the first
    # candidate every time, so the spatial ordering costs no extra LPs.
    assert calls["count"] == len(plan.steps) == 3


def test_happy_path_runs_one_probe_per_step_warm() -> None:
    from legolization import telemetry

    layout, *_ = _three_islands()
    config = InstructionsConfig(
        target_step_size=3,
        max_step_size=3,
        rotstep=False,
        solver=SolverConfig(engine="highspy"),
    )
    with telemetry.record() as session:
        plan = plan_instructions(layout, config=config)
    probes = session.spans.get("stability.prefix.probe")
    assert probes is not None
    assert probes.calls == len(plan.steps) == 3
