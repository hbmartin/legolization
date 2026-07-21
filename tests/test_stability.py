"""Analytic RBE validation: cases with known physical outcomes."""

from typing import Any

import pytest
from scipy.optimize import OptimizeResult

import legolization.stability.solver as solver_module
from legolization.catalog import Catalog, default_catalog
from legolization.graph import ConnectionGraph
from legolization.layout import Layout
from legolization.stability import (
    T_CAPACITY_N,
    SolverConfig,
    analyze,
    build_model,
    build_model_from_config,
    solve_maximin,
    solve_model,
)


@pytest.fixture
def layout():
    return Layout(catalog=default_catalog())


def test_single_grounded_brick_is_relaxed(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    result = analyze(layout)
    assert result.stable
    score = next(iter(result.scores.values()))
    assert score.in_equilibrium
    assert score.drag_max == pytest.approx(0.0, abs=1e-6)
    assert score.score == pytest.approx(0.0, abs=1e-6)


def test_well_bonded_wall_is_stable(layout):
    # Two-layer running-bond wall of 1x4 bricks.
    for x in (0, 4):
        layout.add("brick_1x4", x, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    layout.add("brick_1x4", 2, 0, 3, 0, 4)
    layout.add("brick_1x2", 6, 0, 3, 0, 4)
    result = analyze(layout)
    assert result.stable
    assert all(s.score < 1.0 for s in result.scores.values())


def test_floating_brick_collapses(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    floater = layout.add("brick_2x4", 20, 20, 9, 0, 4)
    result = analyze(layout)
    assert not result.stable
    assert result.unstable_ids == {floater.brick_id}
    assert not result.scores[floater.brick_id].in_equilibrium


def test_brick_resting_on_tile_collapses(layout):
    layout.add("tile_2x2", 0, 0, 0, 0, 4)
    upper = layout.add("brick_2x2", 0, 0, 1, 0, 4)
    result = analyze(layout)
    assert upper.brick_id in result.unstable_ids


def test_single_stud_cantilever_holds_with_drag(layout):
    # A 1x1 tower with a 1x4 brick attached by its end stud: the knob
    # friction couple must carry the overhang torque — stressed but stable.
    # Closed form: the beam's weight W acts 1.5 studs from the knob, whose
    # rear contact pair (two points at -0.25 pitch, torque arm 0.5 studs to
    # the front pair) must drag 2.5 W split two ways: drag_max = 1.25 W.
    tower = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x4", 0, 0, 3, 0, 4)
    result = analyze(layout)
    beam_weight_n = 1.57e-3 * 9.8
    assert result.stable
    assert result.scores[beam.brick_id].drag_max == pytest.approx(
        1.25 * beam_weight_n, rel=1e-4
    )
    assert result.scores[beam.brick_id].score == pytest.approx(0.019625, rel=1e-4)
    assert result.weakest_pair == (tower.brick_id, beam.brick_id)
    assert result.min_capacity < T_CAPACITY_N


def test_maximin_capacity_on_cantilever(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    result = solve_maximin(build_model(layout))
    assert result.feasible
    assert result.capacity == pytest.approx(T_CAPACITY_N - 1.25 * 1.57e-3 * 9.8)


def test_maximin_infeasible_for_floating_brick(layout):
    layout.add("brick_2x4", 0, 0, 9, 0, 4)
    result = solve_maximin(build_model(layout))
    assert not result.feasible


def test_maximin_orders_layouts_by_stress(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    short = solve_maximin(build_model(layout))

    long_layout = Layout(catalog=default_catalog())
    long_layout.add("brick_1x1", 0, 0, 0, 0, 4)
    long_layout.add("brick_1x6", 0, 0, 3, 0, 4)
    long = solve_maximin(build_model(long_layout))
    assert long.feasible
    assert short.feasible
    assert long.capacity < short.capacity < T_CAPACITY_N


def test_longer_cantilever_is_more_stressed(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    short = analyze(layout).max_score

    long_layout = Layout(catalog=default_catalog())
    long_layout.add("brick_1x1", 0, 0, 0, 0, 4)
    long_layout.add("brick_1x6", 0, 0, 3, 0, 4)
    long = analyze(long_layout).max_score
    assert 0.0 < short < long < 1.0


def test_overloaded_cantilever_collapses(layout):
    # Load the free end of a single-stud cantilever with a tall heavy
    # column until the knob friction capacity (T = 0.98 N) is exceeded.
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x6", 1, 0, 3, 0, 4)
    for level in range(24):
        layout.add("brick_2x2", 5, 0, 6 + 3 * level, 0, 4)
    result = analyze(layout)
    assert not result.stable
    assert result.scores[beam.brick_id].score == 1.0


def test_milp_matches_lp_on_stable_stack(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    lp = analyze(layout, SolverConfig(mode="lp"))
    milp = analyze(layout, SolverConfig(mode="milp"))
    assert lp.stable
    assert milp.stable


def _assert_lp_milp_agree(layout: Layout) -> None:
    """Assert the MILP reproduces the (exact) LP relaxation everywhere."""
    lp = analyze(layout, SolverConfig(mode="lp"))
    milp = analyze(layout, SolverConfig(mode="milp"))
    assert lp.stable == milp.stable
    assert lp.unstable_ids == milp.unstable_ids
    for brick_id, lp_score in lp.scores.items():
        assert milp.scores[brick_id].score == pytest.approx(lp_score.score, abs=1e-4)


def test_milp_matches_lp_on_stressed_cantilever(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    _assert_lp_milp_agree(layout)


def test_milp_matches_lp_on_overloaded_cantilever(layout):
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x6", 1, 0, 3, 0, 4)
    for level in range(24):
        layout.add("brick_2x2", 5, 0, 6 + 3 * level, 0, 4)
    _assert_lp_milp_agree(layout)


def test_model_shape(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    model = build_model(layout)
    assert model.brick_count == 2
    assert model.a_matrix.shape[0] == 10  # 5 equations per brick
    # 8 ground knobs + 8 interface knobs, 3 points each (2x4 is 2 wide),
    # each with normal+drag, plus 4 knob-press vars per knob.
    knobs = 16
    assert model.var_count == knobs * 3 * 2 + knobs * 4


def test_lp_fallback_requires_successful_linprog(layout, monkeypatch):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    model = build_model(layout)
    original_linprog = solver_module.linprog
    calls = 0

    def fake_linprog(**kwargs) -> OptimizeResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return OptimizeResult(
                success=False,
                status=2,
                message="failed with iterate",
                x=[0.0] * len(kwargs["c"]),
                fun=0.0,
            )
        return original_linprog(**kwargs)

    monkeypatch.setattr(solver_module, "linprog", fake_linprog)

    result = solve_model(model, SolverConfig(mode="lp"))

    assert result.status == "optimal"
    assert calls == 2


def test_milp_fallback_skips_non_optimal_statuses(monkeypatch):
    solve_with_fallback = solver_module._solve_with_fallback  # noqa: SLF001
    monkeypatch.setattr(solver_module, "_MILP_SOLVERS", ("HIGHS", "SCIP"))

    class FakeProblem:
        """Fake cvxpy problem that succeeds only on the second solver."""

        def __init__(self) -> None:
            self.status = "not-started"
            self.solvers: list[str] = []

        def solve(self, *, solver: str, **options: object) -> None:
            """Record solver attempts and expose a cvxpy-like status."""
            assert options == ({"threads": 1} if solver == "HIGHS" else {})
            self.solvers.append(solver)
            self.status = "optimal" if len(self.solvers) == 2 else "infeasible"

    problem: Any = FakeProblem()

    assert solve_with_fallback(problem, SolverConfig(mode="milp")) == "optimal"
    assert problem.solvers == ["HIGHS", "SCIP"]


def test_milp_fallback_does_not_chain_stale_solver_error(monkeypatch):
    solve_with_fallback = solver_module._solve_with_fallback  # noqa: SLF001
    monkeypatch.setattr(solver_module, "_MILP_SOLVERS", ("HIGHS", "SCIP"))

    class FakeProblem:
        """Fake cvxpy problem where a later solver returns a final status."""

        def __init__(self) -> None:
            self.status = "not-started"
            self.solvers: list[str] = []

        def solve(self, *, solver: str, **options: object) -> None:
            """Raise once, then expose a cvxpy-like non-optimal status."""
            assert options == ({"threads": 1} if solver == "HIGHS" else {})
            self.solvers.append(solver)
            if len(self.solvers) == 1:
                raise solver_module.cp.SolverError
            self.status = "infeasible"

    problem: Any = FakeProblem()

    with pytest.raises(RuntimeError, match="last status: infeasible") as exc_info:
        solve_with_fallback(problem, SolverConfig(mode="milp"))

    assert exc_info.value.__cause__ is None
    assert problem.solvers == ["HIGHS", "SCIP"]


def test_empty_layout_is_stable(layout):
    assert analyze(layout).stable


def _bridge(gap: int) -> Layout:
    """Two cantilevered 1x4 beams on end towers, ``gap`` studs apart."""
    layout = Layout(catalog=default_catalog())
    span = 8 + gap
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", span - 1, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    layout.add("brick_1x4", 4 + gap, 0, 3, 0, 4)
    return layout


def test_side_presses_shed_cantilever_load():
    # Luo §6: beams butted mid-span lean on each other through their shared
    # vertical face; the side presses at the face's vertical extremes carry
    # torque, so each half is far less stressed than a free cantilever.
    butted = analyze(_bridge(gap=0))
    gapped = analyze(_bridge(gap=1))
    assert butted.stable
    assert gapped.stable
    assert butted.max_score < 0.5 * gapped.max_score


def test_mirrored_layout_scores_identically(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)  # extends toward +x

    mirrored = Layout(catalog=default_catalog())
    mirrored.add("brick_1x1", 10, 0, 0, 0, 4)
    mirrored.add("brick_1x4", 7, 0, 3, 0, 4)  # extends toward -x
    assert analyze(mirrored).max_score == pytest.approx(analyze(layout).max_score)


def test_side_contacts_add_two_variables_per_pair(layout):
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 1, 0, 0, 4)
    model = build_model(layout)
    # 4 ground knobs x 4 points (1-wide cavities) x (normal + drag) plus
    # 4 knob presses per knob, plus 2 side presses at the shared face's
    # vertical extremes (was 1 torque-inert press before F1).
    knobs = 4
    assert model.var_count == knobs * 4 * 2 + knobs * 4 + 2


def test_cavity_pattern_var_counts(layout):
    # 2-wide cavities pinch studs at three points, 1-wide at four.
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    layout.add("brick_2x2", 0, 0, 3, 0, 4)
    model = build_model(layout)
    knobs = 8  # 4 ground + 4 interface, all under 2x2 bricks
    assert model.var_count == knobs * 3 * 2 + knobs * 4


def test_ground_pull_keeps_tipping_column_stable(layout):
    # A 1x6 beam off a grounded 1x2 with a column at the beam tip: the
    # load's line of action falls outside the base footprint, so the far
    # ground contacts must pull down (StableLego baseplate-style ground).
    base = layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x6", 0, 0, 3, 0, 4)
    for level in range(3):
        layout.add("brick_1x1", 5, 0, 6 + 3 * level, 0, 4)
    result = analyze(layout)
    assert result.stable
    assert result.scores[base.brick_id].drag_max > 1e-3


# --- yaw torque (torque_z) ---


def test_torque_z_adds_sixth_row_per_brick(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    model = build_model(layout, torque_z=True)
    assert model.rows_per_brick == 6
    assert model.a_matrix.shape[0] == 12
    # Contact-variable census is unchanged: the row grows, not the vars.
    knobs = 16
    assert model.var_count == knobs * 3 * 2 + knobs * 4


def test_torque_z_side_contacts_use_four_corner_generators(layout):
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 1, 0, 0, 4)
    model = build_model(layout, torque_z=True)
    # Same knob census as the 5-row pin, but the side pair now carries
    # 4 corner presses (2 vertical extremes x 2 transverse extremes)
    # so lateral load can express yaw torque.
    knobs = 4
    assert model.var_count == knobs * 4 * 2 + knobs * 4 + 4


def test_torque_z_preserves_untwisted_verdicts(layout):
    # Gravity never loads the yaw row (its lever is identically zero),
    # so verdicts and scores on gravity-only classics must match the
    # 5-row physics exactly.
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x2", 1, 0, 3, 0, 4)
    del beam
    base = analyze(layout, SolverConfig())
    yaw = analyze(layout, SolverConfig(torque_z=True))
    assert yaw.stable == base.stable
    assert yaw.max_score == pytest.approx(base.max_score, rel=1e-6)


def test_side_contact_transverse_extent_recorded(layout):
    # A 1x4 beside a 1x4: physical edges extend half a stud past centers.
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 1, 0, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    (side,) = graph.side_contacts
    assert side.axis == 1
    assert (side.t_lo, side.t_hi) == (-0.5, 3.5)


def test_one_wide_side_contact_has_four_physical_corner_generators(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", 0, 1, 0, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    (side,) = graph.side_contacts
    assert (side.t_lo, side.t_hi) == (-0.5, 0.5)

    model = build_model(layout, graph, torque_z=True)
    side_columns = model.a_matrix[:, -4:].toarray()
    assert len({tuple(side_columns[:, i]) for i in range(4)}) == 4
    yaw_rows = side_columns[[5, 11], :]
    assert (abs(yaw_rows).max(axis=0) > 0.0).all()


# --- paper knob rule (Q x X) ---


def _catalog_with_3x3() -> Catalog:
    from legolization.catalog import DOWN, UP, Category, Connector, Part

    catalog = Catalog(parts=dict(default_catalog().parts))
    columns = [(dx, dy) for dx in range(3) for dy in range(3)]
    part = Part(
        key="brick_3x3",
        ldraw_part="99999",
        category=Category.BRICK,
        occupied_cells=frozenset((dx, dy, dz) for dx, dy in columns for dz in range(3)),
        top_connectors=tuple(
            Connector(cell=(dx, dy, 2), direction=UP) for dx, dy in columns
        ),
        bottom_connectors=tuple(
            Connector(cell=(dx, dy, 0), direction=DOWN) for dx, dy in columns
        ),
        height_plates=3,
        mass_g=3.0,
    )
    catalog.parts["brick_3x3"] = part
    return catalog


def test_paper_knob_rule_splits_edge_and_interior():
    layout = Layout(catalog=_catalog_with_3x3())
    layout.add("brick_3x3", 0, 0, 0, 0, 4)
    # 9 ground knobs under a 3x3 body: release rule gives 3 points each;
    # paper rule keeps 3 on the 8 edge knobs and lifts the centre knob
    # to 4 — one extra contact point = 2 extra shared vars.
    release = build_model(layout)
    paper = build_model(layout, paper_knob_rule=True)
    knobs = 9
    assert release.var_count == knobs * 3 * 2 + knobs * 4
    assert paper.var_count == release.var_count + 1 * 2


def test_paper_knob_rule_inert_for_shipped_catalog(layout):
    # No shipped part has min footprint dimension >= 3, so the flag is
    # a provable no-op on any layout built from the default catalog.
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    release = build_model(layout)
    paper = build_model(layout, paper_knob_rule=True)
    assert paper.var_count == release.var_count
    assert paper.a_matrix.shape == release.a_matrix.shape


# --- yaw-rotated contact patterns ---


def _single_knob_cantilever(*, rotated: bool) -> Layout:
    # A 2x4 hanging off a 1x1 tower by one stud: the three-point
    # triangle's orientation binds (single knob, no cross-knob spread).
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    if rotated:
        layout.add("brick_2x4", 1, 0, 3, 90, 4)
    else:
        layout.add("brick_2x4", 0, 0, 3, 0, 4)
    return layout


def test_release_triangle_is_rotation_variant():
    # The StableLego release keeps the triangle axis-aligned: the same
    # physical structure scores differently built rotated 90 degrees
    # (measured 0.0792 vs 0.1080 — a real verdict distortion). Release
    # parity is opt-in since the v5 flip.
    config = SolverConfig(rotate_contact_pattern=False)
    plain = analyze(_single_knob_cantilever(rotated=False), config).max_score
    turned = analyze(_single_knob_cantilever(rotated=True), config).max_score
    assert plain != pytest.approx(turned, rel=1e-6)


def test_rotate_contact_pattern_restores_rotation_invariance():
    # The default physics since the v5 flip.
    plain = analyze(_single_knob_cantilever(rotated=False)).max_score
    turned = analyze(_single_knob_cantilever(rotated=True)).max_score
    assert plain == pytest.approx(turned, rel=1e-9)


def test_configured_maximin_restores_rotation_invariance():
    config = SolverConfig()
    plain = solve_maximin(
        build_model_from_config(_single_knob_cantilever(rotated=False), config)
    )
    turned = solve_maximin(
        build_model_from_config(_single_knob_cantilever(rotated=True), config)
    )
    assert plain.feasible
    assert turned.feasible
    assert plain.capacity == pytest.approx(turned.capacity, rel=1e-9)


def test_model_from_config_maps_every_physics_switch(layout):
    beam = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    config = SolverConfig(
        torque_z=True,
        paper_knob_rule=True,
        rotate_contact_pattern=True,
        ground_pull=False,
    )
    configured = build_model_from_config(
        layout,
        config,
        extra_masses={beam.brick_id: 0.25},
    )
    explicit = build_model(
        layout,
        torque_z=True,
        paper_knob_rule=True,
        rotate_contact_pattern=True,
        ground_pull=False,
        extra_masses={beam.brick_id: 0.25},
    )
    assert configured.rows_per_brick == explicit.rows_per_brick
    assert (configured.a_matrix != explicit.a_matrix).nnz == 0
    assert configured.b_vector == pytest.approx(explicit.b_vector)


def test_rotate_pattern_moves_only_the_triangle():
    from legolization.stability.constants import (
        FOUR_POINT_OFFSETS,
        THREE_POINT_OFFSETS,
    )
    from legolization.stability.model import rotate_pattern

    assert set(rotate_pattern(FOUR_POINT_OFFSETS, 90)) == set(FOUR_POINT_OFFSETS)
    assert set(rotate_pattern(THREE_POINT_OFFSETS, 180)) == {
        (-ox, -oy) for ox, oy in THREE_POINT_OFFSETS
    }
    assert rotate_pattern(THREE_POINT_OFFSETS, 0) == THREE_POINT_OFFSETS


# --- table mode (ground_pull=False) and external loads ---


def test_table_mode_lets_top_heavy_column_tip(layout):
    # The exact structure the baseplate pin keeps stable becomes a
    # tipping verdict when the ground can push but not pull.
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x6", 0, 0, 3, 0, 4)
    for level in range(3):
        layout.add("brick_1x1", 5, 0, 6 + 3 * level, 0, 4)
    assert analyze(layout).stable
    assert not analyze(layout, SolverConfig(ground_pull=False)).stable


def test_extra_mass_overloads_a_stable_cantilever(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x4", 0, 0, 3, 0, 4)
    assert analyze(layout).stable
    loaded = analyze(layout, extra_masses={beam.brick_id: 1.0})
    assert not loaded.stable
    assert beam.brick_id in loaded.unstable_ids


def test_extra_mass_zero_is_identity(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    beam = layout.add("brick_1x4", 0, 0, 3, 0, 4)
    plain = analyze(layout)
    zero = analyze(layout, extra_masses={beam.brick_id: 0.0})
    assert zero.max_score == pytest.approx(plain.max_score, rel=1e-9)
