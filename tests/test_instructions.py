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
    Subassembly,
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
    code = main(["--restarts", "1", str(npy), "-o", str(out), "--bom", str(bom_path)])
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
    assert main(["--restarts", "1", str(npy), "-o", str(smart)]) == 0
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


# --- subassemblies ---------------------------------------------------------


def _add_mini_mushroom(layout: Layout, x0: int) -> None:
    """Stem with an overhanging petal only held by a bridge from above."""
    for level in (0, 3, 6):
        layout.add("brick_2x2", x0 + 2, 3, level, 0, 15)  # stem
    layout.add("brick_2x2", x0, 3, 9, 0, 4)  # petal, no support below
    layout.add("brick_2x2", x0 + 2, 3, 9, 0, 4)  # hub on the stem
    layout.add("brick_2x2", x0 + 1, 3, 12, 0, 4)  # bridge petal to hub


def _mini_mushroom() -> Layout:
    layout = Layout(catalog=default_catalog())
    _add_mini_mushroom(layout, 1)
    return layout


def test_subassembly_extraction_on_mini_mushroom() -> None:
    layout = _mini_mushroom()
    base = plan_instructions(
        layout, config=InstructionsConfig(rotstep=False, subassemblies=False)
    )
    base_unstable = sum(1 for s in base.steps if not s.prefix_stable)
    assert base_unstable > 0
    config = InstructionsConfig(rotstep=False, subassemblies=True)
    plan = plan_instructions(layout, config=config)
    assert plan.subassemblies
    assert sorted(plan.order) == sorted(layout.bricks)
    unstable = sum(1 for s in plan.steps if not s.prefix_stable)
    assert unstable <= base_unstable
    for sub in plan.subassemblies:
        sub_steps = plan.sub_steps(sub.name)
        assert sub_steps
        assert all(s.prefix_stable for s in sub_steps)
        attach = [s for s in plan.steps if s.attaches == sub.name]
        assert len(attach) == 1
        assert attach[0].brick_ids == ()
        assert sub.anchor_layer == min(
            layout.bricks[bid].layer for bid in sub.brick_ids
        )
    assert verify_plan(layout, plan, config=config) == []


def test_subassemblies_opt_out_gives_a_flat_plan() -> None:
    # Default-on since v5 (measured: three corpus models go fully clean);
    # explicit False restores the flat single-sequence plan.
    layout = _mini_mushroom()
    config = InstructionsConfig(rotstep=False, subassemblies=False)
    plain = plan_instructions(layout, config=config)
    assert plain.subassemblies == ()
    assert all(s.submodel is None and s.attaches is None for s in plain.steps)


def test_subassemblies_default_on() -> None:
    layout = _mini_mushroom()
    plan = plan_instructions(layout, config=InstructionsConfig(rotstep=False))
    assert plan.subassemblies


def test_min_sub_bricks_gate() -> None:
    layout = _mini_mushroom()
    config = InstructionsConfig(
        rotstep=False, subassemblies=True, min_sub_bricks=10_000
    )
    plan = plan_instructions(layout, config=config)
    assert plan.subassemblies == ()


def test_max_subassemblies_cap() -> None:
    layout = Layout(catalog=default_catalog())
    _add_mini_mushroom(layout, 1)
    _add_mini_mushroom(layout, 11)  # disjoint twin -> two candidate clusters
    uncapped = plan_instructions(
        layout, config=InstructionsConfig(rotstep=False, subassemblies=True)
    )
    assert len(uncapped.subassemblies) == 2
    capped = plan_instructions(
        layout,
        config=InstructionsConfig(
            rotstep=False, subassemblies=True, max_subassemblies=1
        ),
    )
    assert len(capped.subassemblies) == 1


def test_verify_plan_flags_broken_subassembly() -> None:
    layout = _mini_mushroom()
    config = InstructionsConfig(rotstep=False, subassemblies=True)
    plan = plan_instructions(layout, config=config)
    assert plan.subassemblies
    # Attach step that claims to place bricks is a violation.
    broken_steps = tuple(
        replace(s, brick_ids=tuple(plan.subassemblies[0].brick_ids[:1]))
        if s.attaches is not None
        else s
        for s in plan.steps
    )
    broken = replace(plan, steps=broken_steps)
    violations = verify_plan(layout, broken, config=config)
    assert violations


def test_subassembly_plans_are_deterministic() -> None:
    layout = _mini_mushroom()
    config = InstructionsConfig(rotstep=False, subassemblies=True)
    a = plan_instructions(layout, config=config)
    b = plan_instructions(layout, config=config)
    assert a.steps == b.steps
    assert a.subassemblies == b.subassemblies


def test_layout_translated_preserves_ids() -> None:
    layout = _mini_mushroom()
    ids = sorted(layout.bricks)
    top = layout.subset(bid for bid in ids if layout.bricks[bid].layer >= 9)
    moved = top.translated(dz=9)
    assert sorted(moved.bricks) == sorted(top.bricks)
    assert min(b.layer for b in moved) == 0
    assert all(
        moved.bricks[bid].layer == top.bricks[bid].layer - 9 for bid in moved.bricks
    )
    with pytest.raises(ValueError, match="below ground"):
        top.translated(dz=100)


def test_profile_rejected_for_sweep_and_import(tmp_path):
    from legolization.main import main

    npy = tmp_path / "m.npy"
    np.save(npy, np.full((2, 2, 2), 4, dtype=np.int16))
    with pytest.raises(SystemExit) as excinfo:
        main([str(npy), "--strategy", "all", "--profile", str(tmp_path / "p.json")])
    assert excinfo.value.code == 2

    source = tmp_path / "m.ldr"
    source.write_text("0 m\n1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3005.dat\n")
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                str(source),
                "-o",
                str(tmp_path / "out.ldr"),
                "--profile",
                str(tmp_path / "p.json"),
            ]
        )
    assert excinfo.value.code == 2


def test_verify_plan_requires_exactly_one_attach():
    # Removing the attach step used to leave plan.order complete while
    # the emitted world lost the whole subassembly (PR #17 review).
    layout = _mini_mushroom()
    config = InstructionsConfig(rotstep=False, subassemblies=True)
    plan = plan_instructions(layout, config=config)
    assert plan.subassemblies
    without_attach = replace(
        plan,
        steps=tuple(s for s in plan.steps if s.attaches is None),
    )
    violations = verify_plan(layout, without_attach, config=config)
    assert any("attached 0 time" in v for v in violations)
    assert any("does not place every brick" in v for v in violations)

    doubled = replace(
        plan,
        steps=(*plan.steps, next(s for s in plan.steps if s.attaches is not None)),
    )
    violations = verify_plan(layout, doubled, config=config)
    assert any("attached 2 time" in v for v in violations)


def _stable_subassembly_plan(
    *,
    fragile_sub_step: bool = False,
    fragile_attach_step: bool = False,
) -> tuple[Layout, InstructionPlan, InstructionsConfig]:
    """Two stacked 2x2s whose table-build and seated presses are stable."""
    layout = Layout(catalog=default_catalog())
    base = layout.add("brick_2x2", 0, 0, 0, 0, 4)
    unit = layout.add("brick_2x2", 0, 0, 3, 0, 4)
    plan = InstructionPlan(
        steps=(
            BuildStep(
                index=1,
                brick_ids=(base.brick_id,),
                prefix_stable=True,
                prefix_max_score=0.0,
            ),
            BuildStep(
                index=2,
                brick_ids=(unit.brick_id,),
                prefix_stable=True,
                prefix_max_score=0.0,
                submodel="sub-1",
                insertion_fragile=fragile_sub_step,
            ),
            BuildStep(
                index=3,
                brick_ids=(),
                prefix_stable=True,
                prefix_max_score=0.0,
                attaches="sub-1",
                insertion_fragile=fragile_attach_step,
            ),
        ),
        warnings=(),
        bom=bill_of_materials(layout),
        subassemblies=(
            Subassembly(
                name="sub-1",
                brick_ids=(unit.brick_id,),
                anchor_layer=3,
            ),
        ),
    )
    config = InstructionsConfig(
        rotstep=False,
        subassemblies=True,
        insertion_check=True,
    )
    return layout, plan, config


def test_verify_plan_rejects_false_fragile_sub_build_mark() -> None:
    layout, plan, config = _stable_subassembly_plan(fragile_sub_step=True)

    violations = verify_plan(layout, plan, config=config)

    assert any(
        "step 2: flagged insertion-fragile but the press verdict is stable" in violation
        for violation in violations
    )


def test_verify_plan_rejects_false_fragile_attach_mark() -> None:
    layout, plan, config = _stable_subassembly_plan(fragile_attach_step=True)

    violations = verify_plan(layout, plan, config=config)

    assert any(
        "step 3: flagged insertion-fragile but the press verdict is stable" in violation
        for violation in violations
    )


def test_strict_policy_judged_after_subassembly_rewrite():
    # The mini mushroom is unorderable without subassemblies (strict
    # raises), but fully stable with them: strict + subassemblies must
    # succeed by enforcing strictness on the rewritten plan.
    layout = _mini_mushroom()
    with pytest.raises(InstructionsError, match="no stable ordering"):
        plan_instructions(
            layout,
            config=InstructionsConfig(
                rotstep=False, stability_policy="strict", subassemblies=False
            ),
        )
    plan = plan_instructions(
        layout,
        config=InstructionsConfig(
            rotstep=False, stability_policy="strict", subassemblies=True
        ),
    )
    assert plan.subassemblies
    assert all(step.prefix_stable for step in plan.steps)


def test_strict_with_subassemblies_still_raises_when_unfixable(bad_bridge):
    layout, _ = bad_bridge  # collapses even fully built
    with pytest.raises(InstructionsError, match="no stable ordering"):
        plan_instructions(
            layout,
            config=InstructionsConfig(
                rotstep=False, stability_policy="strict", subassemblies=True
            ),
        )


def test_cli_profile_payload_is_schema_two(tmp_path):
    from legolization.main import main

    npy = tmp_path / "m.npy"
    np.save(npy, np.full((3, 3, 2), 4, dtype=np.int16))
    profile = tmp_path / "profile.json"
    out = tmp_path / "m.ldr"
    assert (
        main(["--restarts", "1", str(npy), "-o", str(out), "--profile", str(profile)])
        == 0
    )
    payload = json.loads(profile.read_text())
    assert payload["schema"] == 2
    assert payload["source"] == "cli"
    sha = payload["git_sha"]
    assert isinstance(sha, str)
    assert len(sha) == 40
    assert payload["spans"]["stability.analyze"]["calls"] >= 1


def _verdict(*, stable: bool, score: float) -> StabilityResult:
    from legolization.stability.solver import BrickScore

    return StabilityResult(
        stable=stable,
        scores={
            1: BrickScore(brick_id=1, score=score, drag_max=0.0, in_equilibrium=True)
        },
    )


def test_scan_window_defers_press_fragile_candidates() -> None:
    # WS-I: with the check on, a statically-stable-but-press-fragile
    # candidate is skipped when the window holds a press-stable
    # alternative; with the check off the scan accepts the first stable
    # candidate exactly as before.
    from legolization.instructions.sequencer import _scan_ready_window

    chunks: list[tuple[int, tuple[int, ...]]] = [(0, (1,)), (0, (2,))]
    static = _verdict(stable=True, score=0.2)
    press_bad = _verdict(stable=False, score=1.0)
    press_ok = _verdict(stable=True, score=0.3)
    accepted: list[tuple[int, ...]] = []

    def analyze_prefix(chunk: tuple[int, ...]) -> StabilityResult:
        return static

    def press_prefix(chunk: tuple[int, ...]) -> StabilityResult:
        return press_bad if chunk == (1,) else press_ok

    def accept(chunk: tuple[int, ...], score: float) -> None:
        accepted.append(chunk)

    on = InstructionsConfig(insertion_check=True)
    pos, _best, best_fragile = _scan_ready_window(
        [0, 1],
        chunks,
        on,
        analyze_prefix=analyze_prefix,
        press_prefix=press_prefix,
        accept=accept,
    )
    assert pos == 1  # the press-stable second candidate wins
    assert accepted == [(2,)]
    assert best_fragile == (1.0, 0, 0.2)

    accepted.clear()
    off = InstructionsConfig()
    pos, _best, best_fragile = _scan_ready_window(
        [0, 1],
        chunks,
        off,
        analyze_prefix=analyze_prefix,
        press_prefix=press_prefix,
        accept=accept,
    )
    assert pos == 0  # byte-identical to the historical scan
    assert accepted == [(1,)]
    assert best_fragile is None


def test_scan_window_all_fragile_returns_best() -> None:
    from legolization.instructions.sequencer import _scan_ready_window

    chunks: list[tuple[int, tuple[int, ...]]] = [(0, (1,)), (0, (2,))]
    static = _verdict(stable=True, score=0.2)

    def analyze_prefix(chunk: tuple[int, ...]) -> StabilityResult:
        return static

    def press_prefix(chunk: tuple[int, ...]) -> StabilityResult:
        return _verdict(stable=False, score=2.0 if chunk == (1,) else 1.5)

    pos, _best, best_fragile = _scan_ready_window(
        [0, 1],
        chunks,
        InstructionsConfig(insertion_check=True),
        analyze_prefix=analyze_prefix,
        press_prefix=press_prefix,
        accept=lambda chunk, score: None,
    )
    assert pos is None
    assert best_fragile == (1.5, 1, 0.2)  # least-fragile candidate tracked


def _press_arm_layout() -> Layout:
    # A slim column with one short two-knob cantilever arm: statically
    # stable throughout, but seating the arm under a 1 kg press tears
    # its joint (the press-tower corpus class, minimal).
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x2", 0, 0, 0, 0, 1)
    layout.add("plate_1x6", 0, 0, 3, 0, 4)  # arm: two knobs on the column
    return layout


def test_insertion_check_flags_forced_fragile_step() -> None:
    layout = _press_arm_layout()
    plan = plan_instructions(
        layout,
        config=InstructionsConfig(
            rotstep=False, subassemblies=False, insertion_check=True
        ),
    )
    fragile = [step for step in plan.steps if step.insertion_fragile]
    assert len(fragile) == 1
    assert fragile[0].prefix_stable  # statically fine, press-fragile
    assert any("insertion-fragile" in w for w in plan.warnings)
    config = InstructionsConfig(
        rotstep=False, subassemblies=False, insertion_check=True
    )
    assert verify_plan(layout, plan, config=config) == []


def test_insertion_check_off_is_byte_identical() -> None:
    layout = _press_arm_layout()
    base = plan_instructions(
        layout, config=InstructionsConfig(rotstep=False, subassemblies=False)
    )
    assert all(not step.insertion_fragile for step in base.steps)
    assert not any("insertion-fragile" in w for w in base.warnings)


def test_insertion_mass_must_be_positive() -> None:
    with pytest.raises(ValueError, match="insertion_mass_kg"):
        InstructionsConfig(insertion_mass_kg=0.0)
    with pytest.raises(ValueError, match="insertion_mass_kg"):
        InstructionsConfig(insertion_mass_kg=float("nan"))


def test_cross_band_union_constraints_and_dependency_order() -> None:
    from legolization.instructions.sequencer import (
        _insertion_order,
        _press_union_allowed,
    )

    layout = Layout(catalog=default_catalog())
    base = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    middle = layout.add("brick_1x1", 0, 0, 3, 0, 4)
    upper = layout.add("brick_1x1", 0, 0, 6, 0, 4)
    top = layout.add("brick_1x1", 0, 0, 9, 0, 4)
    supports = {
        base.brick_id: set(),
        middle.brick_id: {base.brick_id},
        upper.brick_id: {middle.brick_id},
        top.brick_id: {upper.brick_id},
    }
    blockers = {brick.brick_id: frozenset() for brick in layout}
    blocks = {brick.brick_id: set() for brick in layout}
    neighbours = {
        base.brick_id: {middle.brick_id},
        middle.brick_id: {base.brick_id, upper.brick_id},
        upper.brick_id: {middle.brick_id, top.brick_id},
        top.brick_id: {upper.brick_id},
    }
    rank = {0: 0, 3: 1, 6: 2, 9: 3}
    pair = (upper.brick_id, middle.brick_id)
    assert _press_union_allowed(
        layout,
        pair,
        placed={base.brick_id},
        supports=supports,
        blockers=blockers,
        blocks=blocks,
        neighbours=neighbours,
        band_rank=rank,
        max_step_size=2,
    )
    assert _insertion_order(
        layout,
        pair,
        supports=supports,
        blockers=blockers,
    ) == (middle.brick_id, upper.brick_id)
    assert not _press_union_allowed(
        layout,
        (middle.brick_id, upper.brick_id, top.brick_id),
        placed={base.brick_id},
        supports=supports,
        blockers=blockers,
        blocks=blocks,
        neighbours=neighbours,
        band_rank=rank,
        max_step_size=3,
    )
    assert not _press_union_allowed(
        layout,
        pair,
        placed={base.brick_id},
        supports=supports,
        blockers=blockers,
        blocks=blocks,
        neighbours=neighbours,
        band_rank=rank,
        max_step_size=1,
    )


def test_press_union_prefers_stable_then_truthful_fragile_fallback() -> None:
    from legolization.instructions.sequencer import _best_press_union

    layout = Layout(catalog=default_catalog())
    base = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    middle = layout.add("brick_1x1", 0, 0, 3, 0, 4)
    upper = layout.add("brick_1x1", 0, 0, 6, 0, 4)
    chunks: list[tuple[int, tuple[int, ...]]] = [
        (3, (middle.brick_id,)),
        (6, (upper.brick_id,)),
    ]
    supports = {
        base.brick_id: set(),
        middle.brick_id: {base.brick_id},
        upper.brick_id: {middle.brick_id},
    }
    blockers = {brick.brick_id: frozenset() for brick in layout}
    blocks = {brick.brick_id: set() for brick in layout}
    neighbours = {
        base.brick_id: {middle.brick_id},
        middle.brick_id: {base.brick_id, upper.brick_id},
        upper.brick_id: {middle.brick_id},
    }
    static = _verdict(stable=True, score=0.2)
    fragile = _verdict(stable=False, score=1.2)
    robust = _verdict(stable=True, score=0.4)
    common = {
        "layout": layout,
        "seed": 0,
        "pending": [0, 1],
        "chunks": chunks,
        "placed": {base.brick_id},
        "supports": supports,
        "blockers": blockers,
        "blocks": blocks,
        "neighbours": neighbours,
        "band_rank": {3: 0, 6: 1},
        "brick_position": {middle.brick_id: 0, upper.brick_id: 1},
        "max_step_size": 2,
        "analyze_prefix": lambda _chunk: static,
    }
    stable_choice = _best_press_union(
        **common,
        press_prefix=lambda chunk: robust if len(chunk) == 2 else fragile,
    )
    assert stable_choice is not None
    assert stable_choice[0] == (0, 1)
    assert stable_choice[3] is False

    fallback = _best_press_union(
        **common,
        press_prefix=lambda _chunk: fragile,
    )
    assert fallback is not None
    assert fallback[0] == (0, 1)
    assert fallback[3] is True


@pytest.mark.slow
@pytest.mark.parametrize(
    ("name", "fragile_limit"),
    [("press-tower", 4), ("cantilever", 4), ("mushroom", 19)],
)
def test_press_corpus_chunking_acceptance(name: str, fragile_limit: int) -> None:
    from pathlib import Path

    from legolization.pipeline import PipelineConfig, load_grid, run

    path = (
        Path(__file__).parent.parent / "data" / "corpus" / "synthetic" / f"{name}.npy"
    )
    instructions = InstructionsConfig(insertion_check=True, rotstep=False)
    result = run(
        load_grid(path),
        PipelineConfig(instructions=instructions),
    )
    assert result.plan is not None
    # Chunking cannot change whole-unit subassembly seating; this gate
    # measures ordinary and rescue chunks, the workstream's scope.
    fragile = [
        step
        for step in result.plan.steps
        if step.insertion_fragile and step.attaches is None
    ]
    assert len(fragile) < fragile_limit
    assert all(step.prefix_stable for step in result.plan.steps)
    assert all(
        len(step.brick_ids) <= instructions.max_step_size for step in result.plan.steps
    )
    assert verify_plan(result.layout, result.plan, config=instructions) == []
