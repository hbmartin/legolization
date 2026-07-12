"""Instruction sequencing, chunking, blocking, BOM, and LDraw emission."""

import json

import numpy as np
import pytest

from legolization.catalog import default_catalog
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


def _pyramid_layout() -> Layout:
    codes = np.full((6, 6, 3), EMPTY, dtype=np.int16)
    for z in range(3):
        lo, hi = z, 6 - z
        codes[lo:hi, lo:hi, z] = 4
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    result = run(grid, PipelineConfig(hollow=False, seed=0))
    return result.layout


def _bad_bridge() -> Layout:
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", 10, 0, 0, 0, 4)
    for level in (3, 6, 9):
        layout.add("brick_1x6", 0, 0, level, 0, 4)
        layout.add("brick_1x4", 6, 0, level, 0, 4)
        layout.add("brick_1x1", 10, 0, level, 0, 4)
    return layout


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


def test_unbuildable_model_warns_not_crashes():
    layout = _bad_bridge()  # collapses even fully built
    plan = plan_instructions(layout)
    assert plan.warnings
    assert any(not step.prefix_stable for step in plan.steps)
    assert sorted(plan.order) == sorted(layout.bricks)


def test_strict_policy_raises_on_unstable_prefix():
    layout = _bad_bridge()
    with pytest.raises(InstructionsError, match="no stable ordering"):
        plan_instructions(layout, config=InstructionsConfig(stability_policy="strict"))


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
