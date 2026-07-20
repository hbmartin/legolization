"""Warm-started prefix/removal solver equivalence and state tests."""

from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from legolization import telemetry
from legolization.catalog import default_catalog
from legolization.grid import VoxelGrid
from legolization.instructions.sequencer import InstructionsConfig, plan_instructions
from legolization.layout import Layout
from legolization.ldraw_out import model_lines
from legolization.pipeline import PipelineConfig, load_grid, run
from legolization.stability.prefix import (
    PrefixSolver,
    RemovalSolver,
    _stud_reach_floating,
)
from legolization.stability.solver import SolverConfig, analyze

_EXAMPLES = Path(__file__).parent.parent / "data" / "examples"
_WARM = SolverConfig(engine="highspy")


def _warm_prefix(layout: Layout) -> PrefixSolver:
    solver = PrefixSolver.create(layout, _WARM)
    assert solver is not None
    return solver


def _warm_removal(layout: Layout, scope: frozenset[int]) -> RemovalSolver:
    solver = RemovalSolver.create(layout, scope, _WARM)
    assert solver is not None
    return solver


def _tower_layout() -> tuple[Layout, list[int]]:
    layout = Layout(catalog=default_catalog())
    ids = [
        layout.add("brick_2x4", 0, 0, 0, 0, 4).brick_id,
        layout.add("brick_1x2", 4, 0, 0, 0, 4).brick_id,
        layout.add("brick_1x6", 0, 0, 3, 90, 4).brick_id,
        layout.add("brick_2x2", 1, 1, 3, 0, 14).brick_id,
        layout.add("brick_1x1", 0, 0, 6, 0, 1).brick_id,
    ]
    return layout, ids


def _score_drift(a, b) -> float:
    return max(
        (abs(a.scores[k].score - b.scores[k].score) for k in b.scores),
        default=0.0,
    )


def test_create_respects_engine_gate():
    layout, _ = _tower_layout()
    assert PrefixSolver.create(layout, SolverConfig(engine="scipy")) is None
    assert PrefixSolver.create(layout, SolverConfig(mode="milp")) is None
    assert PrefixSolver.create(layout, _WARM) is not None
    assert (
        RemovalSolver.create(layout, frozenset(), SolverConfig(engine="scipy")) is None
    )


def test_probe_matches_cold_analyze():
    layout, ids = _tower_layout()
    solver = _warm_prefix(layout)
    placed: list[int] = []
    for chunk in ((ids[0], ids[1]), (ids[2],), (ids[3], ids[4])):
        placed.extend(chunk)
        warm = solver.probe(chunk)
        cold = analyze(layout.subset(placed), _WARM)
        assert warm.stable == cold.stable
        assert _score_drift(warm, cold) < 1e-9
        solver.commit(chunk)


def test_probe_rollback_restores_base():
    layout, ids = _tower_layout()
    solver = _warm_prefix(layout)
    solver.probe((ids[0],))
    solver.commit((ids[0],))
    solver.probe((ids[1],))  # rejected sibling
    probe_after_rollback = solver.probe((ids[2],))
    fresh = _warm_prefix(layout)
    fresh.probe((ids[0],))
    fresh.commit((ids[0],))
    fresh_probe = fresh.probe((ids[2],))
    assert _score_drift(probe_after_rollback, fresh_probe) < 1e-12


def test_commit_after_probe_costs_no_lp():
    layout, ids = _tower_layout()
    with telemetry.record() as session:
        solver = _warm_prefix(layout)
        solver.probe((ids[0],))
        probes_before = session.spans["stability.prefix.probe"].calls
        solver.commit((ids[0],))
        assert session.spans["stability.prefix.probe"].calls == probes_before
        assert "stability.prefix.commit" not in session.spans  # promote path


def test_commit_without_probe_appends():
    layout, ids = _tower_layout()
    solver = _warm_prefix(layout)
    solver.commit((ids[0], ids[1]))  # cache-hit path: no preceding probe
    warm = solver.probe((ids[2],))
    cold = analyze(layout.subset(ids[:3]), _WARM)
    assert warm.stable == cold.stable
    assert _score_drift(warm, cold) < 1e-9


def test_floating_prefix_uses_shortcut():
    layout = Layout(catalog=default_catalog())
    grounded = layout.add("brick_2x4", 0, 0, 0, 0, 4).brick_id
    floater = layout.add("brick_1x2", 10, 10, 3, 0, 4).brick_id
    solver = _warm_prefix(layout)
    with telemetry.record() as session:
        result = solver.probe((grounded, floater))
    assert not result.stable
    assert result.scores[floater].score == 1.0
    assert result.scores[grounded].score == 0.0
    assert "stability.prefix.floating_shortcut" in session.spans
    cold = analyze(layout.subset([grounded, floater]), _WARM)
    assert cold.stable == result.stable
    assert cold.scores[floater].score == 1.0


def test_stud_reach_floating_matches_graph():
    from legolization.graph import GROUND_ID, ConnectionGraph

    config = PipelineConfig(seed=0)
    result = run(load_grid(_EXAMPLES / "heart.vox", config), config)
    layout = result.layout
    # Independent inputs built from public APIs only.
    full_graph = ConnectionGraph.from_layout(layout)
    grounded = frozenset(full_graph.grounded_ids)
    adjacent: dict[int, set[int]] = {bid: set() for bid in layout.bricks}
    for below, above in full_graph.support_edges():
        if below != GROUND_ID:
            adjacent[below].add(above)
            adjacent[above].add(below)
    ids = sorted(layout.bricks)
    for size in (3, 6, 9, len(ids)):
        subset = frozenset(ids[:size])
        expected = ConnectionGraph.from_layout(layout.subset(subset)).floating_ids()
        actual = _stud_reach_floating(subset, grounded, adjacent)
        assert actual == set(expected)


@settings(max_examples=25, deadline=None)
@given(st.data())
def test_incremental_contacts_match_cold(data: st.DataObject):
    codes = np.full((4, 3, 3), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    config = PipelineConfig(seed=0, hollow=False)
    layout = run(grid, config).layout
    ids = sorted(layout.bricks)
    order = data.draw(st.permutations(ids))
    cut_count = data.draw(st.integers(min_value=1, max_value=min(3, len(ids))))
    cuts = sorted(
        data.draw(
            st.sets(
                st.integers(min_value=1, max_value=len(ids)),
                min_size=cut_count,
                max_size=cut_count,
            )
        )
    )
    solver = _warm_prefix(layout)
    start = 0
    placed: list[int] = []
    for cut in cuts:
        chunk = tuple(order[start:cut])
        if not chunk:
            continue
        placed.extend(chunk)
        warm = solver.probe(chunk)
        cold = analyze(layout.subset(placed), _WARM)
        # Verdicts must agree; per-brick scores may shuffle between
        # equal-objective alternative optima (same class of drift the
        # legacy engine shows across scipy versions).
        assert warm.stable == cold.stable
        if warm.status != "floating-shortcut":
            rel = abs(warm.objective - cold.objective) / max(abs(cold.objective), 1e-12)
            assert rel < 1e-3
        solver.commit(chunk)
        start = cut


def test_removal_solver_matches_cold():
    config = PipelineConfig(seed=0)
    result = run(load_grid(_EXAMPLES / "heart.vox", config), config)
    layout = result.layout
    plan = result.plan
    assert plan is not None
    solver = _warm_removal(layout, frozenset(layout.bricks))
    for step in reversed(plan.steps):
        chunk = step.brick_ids
        warm = solver.probe_without(chunk)
        cold = analyze(layout.subset(solver.scope - set(chunk)), _WARM)
        assert warm.stable == cold.stable
        if warm.status != "floating-shortcut":
            assert _score_drift(warm, cold) < 1e-6  # component solves ARE cold
        solver.commit_without(chunk)
    assert not solver.scope


def test_dual_engine_plans_identical_on_examples():
    for name in ("pyramid.npy", "arch.npy", "heart.vox"):
        config = PipelineConfig(seed=0)
        path = _EXAMPLES / name
        layout = run(load_grid(path, config), config).layout
        plans = {}
        for engine in ("scipy", "highspy"):
            icfg = InstructionsConfig(solver=SolverConfig(engine=engine))
            plans[engine] = plan_instructions(layout, config=icfg)
        a, b = plans["scipy"], plans["highspy"]
        assert [s.brick_ids for s in a.steps] == [s.brick_ids for s in b.steps]
        assert [s.prefix_stable for s in a.steps] == [s.prefix_stable for s in b.steps]
        assert list(model_lines(layout, name="m", plan=a)) == list(
            model_lines(layout, name="m", plan=b)
        )


def test_warm_fail_falls_back_cold(monkeypatch: pytest.MonkeyPatch):
    import legolization.stability.prefix as prefix_module

    layout, ids = _tower_layout()
    solver = _warm_prefix(layout)

    def always_fail(status: object) -> None:
        msg = f"forced failure ({status})"
        raise prefix_module.PrefixSolverError(msg)

    monkeypatch.setattr(prefix_module, "_require_optimal", always_fail)
    with telemetry.record() as session:
        result = solver.probe((ids[0],))
    cold = analyze(layout.subset([ids[0]]), _WARM)
    assert result.stable == cold.stable
    assert "stability.prefix.warm_fail" in session.spans


def _hanging_decoration_layout() -> tuple[Layout, int, int]:
    """Two towers, a bridge, and a decorative piece hanging BELOW it.

    The decoration's only connection is its top studs into the bridge's
    bottom sockets — the class of piece the user's models add underneath
    (vertically below) existing structure.
    """
    layout = Layout(catalog=default_catalog())
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    layout.add("brick_2x2", 4, 0, 0, 0, 4)
    layout.add("brick_2x2", 0, 0, 3, 0, 4)
    layout.add("brick_2x2", 4, 0, 3, 0, 4)
    bridge = layout.add("brick_1x6", 0, 0, 6, 0, 4)
    decoration = layout.add("brick_1x1", 2, 0, 3, 0, 14)  # hangs under it
    return layout, bridge.brick_id, decoration.brick_id


def test_probe_brick_below_matches_cold():
    # Probing a chunk whose brick seats BELOW an already-present brick
    # grows the present brick's bottom-drag rows (the grown-base
    # plumbing); the warm result must match cold exactly, including
    # after a rollback of the grown state.
    layout, bridge, decoration = _hanging_decoration_layout()
    towers = sorted(set(layout.bricks) - {bridge, decoration})
    solver = _warm_prefix(layout)
    solver.probe(tuple(towers))
    solver.commit(tuple(towers))
    solver.probe((bridge,))
    solver.commit((bridge,))

    warm = solver.probe((decoration,))
    cold = analyze(layout, _WARM)
    assert warm.stable == cold.stable
    assert _score_drift(warm, cold) < 1e-9

    # Reject the decoration (rollback trims the grown drag columns),
    # probe it again: identical to a fresh solver's answer.
    solver.probe((decoration,))
    again = solver.probe((decoration,))
    fresh = _warm_prefix(layout)
    fresh.probe(tuple(towers))
    fresh.commit(tuple(towers))
    fresh.probe((bridge,))
    fresh.commit((bridge,))
    fresh_probe = fresh.probe((decoration,))
    assert _score_drift(again, fresh_probe) < 1e-12


_DIRECT = SolverConfig(engine="highspy", rescue_direct_min_bricks=1)


def test_direct_rescue_solve_matches_cold():
    # With the size gate forced open, every uncached component solves
    # through highspy directly; verdicts must match the scipy path with
    # only alternative-optima-class drift.
    layout, ids = _tower_layout()
    scope = frozenset(ids)
    direct = RemovalSolver.create(layout, scope, _DIRECT)
    assert direct is not None
    cold = RemovalSolver.create(layout, scope, _WARM)
    assert cold is not None
    for chunk in ((), (ids[4],), (ids[3], ids[4])):
        a = direct.probe_without(chunk)
        b = cold.probe_without(chunk)
        assert a.stable == b.stable
        assert abs(a.objective - b.objective) <= 1e-6 + 1e-3 * abs(b.objective)


def test_direct_rescue_failure_falls_back_to_scipy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import legolization.stability.prefix as prefix_mod

    def failing(*args: object, **kwargs: object) -> object:
        msg = "direct solve exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(prefix_mod, "_solve_lp_highspy", failing)
    layout, ids = _tower_layout()
    solver = RemovalSolver.create(layout, frozenset(ids), _DIRECT)
    assert solver is not None
    with telemetry.record() as session:
        result = solver.probe_without(())
    cold = analyze(layout, _WARM)
    assert result.stable == cold.stable
    assert session.spans["stability.rescue.cold_fallback"].calls >= 1


def test_direct_rescue_gate_keeps_small_models_scipy_exact():
    # Default gate (200 bricks): heart-sized components never take the
    # direct path, protecting the 1e-6 equivalence and plan-bytes pins.
    layout, ids = _tower_layout()
    solver = RemovalSolver.create(layout, frozenset(ids), _WARM)
    assert solver is not None
    with telemetry.record() as session:
        solver.probe_without(())
    assert "stability.rescue.cold_direct" not in session.spans


def test_dual_engine_agrees_with_torque_z():
    # The warm engine must mirror the six-row physics exactly: same
    # plans, same verdicts, on the shipped examples with torque_z on.
    for name in ("pyramid.npy", "heart.vox"):
        config = PipelineConfig(seed=0)
        path = _EXAMPLES / name
        layout = run(load_grid(path, config), config).layout
        plans = {}
        for engine in ("scipy", "highspy"):
            icfg = InstructionsConfig(solver=SolverConfig(engine=engine, torque_z=True))
            plans[engine] = plan_instructions(layout, config=icfg)
        a, b = plans["scipy"], plans["highspy"]
        assert [s.brick_ids for s in a.steps] == [s.brick_ids for s in b.steps]
        assert [s.prefix_stable for s in a.steps] == [s.prefix_stable for s in b.steps]


def test_press_probe_matches_cold_extra_masses():
    # WS-I: the warm bound-change press must equal the cold
    # analyze(extra_masses=) verdicts and scores on both chunks.
    layout, ids = _tower_layout()
    solver = _warm_prefix(layout)
    solver.probe((ids[0], ids[1]))
    solver.commit((ids[0], ids[1]))
    chunk = (ids[2], ids[3])
    warm = solver.press_probe(chunk, 1.0)
    cold = analyze(
        layout.subset(frozenset(ids[:4])),
        SolverConfig(),
        extra_masses=dict.fromkeys(chunk, 1.0),
    )
    assert warm.stable == cold.stable
    assert _score_drift(warm, cold) <= 1e-6


def test_press_probe_restores_bounds_before_commit():
    # The top-risk invariant: probe -> press -> probe must be bitwise
    # unchanged, and a commit after a press must never bake the press
    # into the base model.
    layout, ids = _tower_layout()
    solver = _warm_prefix(layout)
    solver.probe((ids[0], ids[1]))
    solver.commit((ids[0], ids[1]))
    chunk = (ids[2], ids[3])
    before = solver.probe(chunk)
    solver.press_probe(chunk, 5.0)
    after = solver.probe(chunk)
    assert before.stable == after.stable
    assert _score_drift(before, after) == 0.0
    solver.commit(chunk)
    committed = solver.probe((ids[4],))
    cold = analyze(layout.subset(frozenset(ids)), SolverConfig())
    assert committed.stable == cold.stable
    assert _score_drift(committed, cold) <= 1e-6


def test_press_probe_scipy_engine_goes_cold():
    # The scipy engine has no warm model: PrefixSolver.create returns
    # None there, so the sequencer's press path is the cold analyze —
    # equivalence is definitional; pin the gate itself.
    layout, _ids = _tower_layout()
    assert PrefixSolver.create(layout, SolverConfig(engine="scipy")) is None


def test_torque_z_warm_path_stays_warm():
    # PR #20 review (severity 3): the near-boundary zip hard-coded five
    # tolerances, so torque_z's sixth residual raised inside the broad
    # warm-fail handler and EVERY probe silently ran cold — the
    # dual-engine regression passed while testing the fallback engine.
    layout, ids = _tower_layout()
    solver = PrefixSolver.create(layout, SolverConfig(torque_z=True))
    assert solver is not None
    with telemetry.record() as session:
        solver.probe((ids[0], ids[1]))
        solver.commit((ids[0], ids[1]))
        solver.probe((ids[2], ids[3]))
    assert "stability.prefix.warm_fail" not in session.spans
    assert "stability.prefix.rebuild" not in session.spans
