"""Reduced-model builder: geometry sharing, expansion semantics, declines."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.layout import Layout
from legolization.stability import SolverConfig, build_model_from_config
from legolization.stability.reduced import build_reduced_model


@pytest.fixture
def layout():
    return Layout(catalog=default_catalog())


def _wall(layout: Layout) -> Layout:
    for x in (0, 4):
        layout.add("brick_1x4", x, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    layout.add("brick_1x4", 2, 0, 3, 0, 4)
    layout.add("brick_1x2", 6, 0, 3, 0, 4)
    return layout


@pytest.mark.parametrize("torque_z", [False, True])
@pytest.mark.parametrize("paper_knob_rule", [False, True])
@pytest.mark.parametrize("rotate_contact_pattern", [False, True])
@pytest.mark.parametrize("ground_pull", [False, True])
def test_walk_matches_exact_allocation_under_every_switch(
    layout,
    *,
    torque_z: bool,
    paper_knob_rule: bool,
    rotate_contact_pattern: bool,
    ground_pull: bool,
):
    # Yawed parts, mixed patterns, a side contact, and ground knobs: if
    # the reduced walk ever drifts from _build_model_body's allocation
    # order, build_reduced_model raises rather than mis-mapping columns.
    _wall(layout)
    layout.add("brick_2x2", 0, 1, 0, 90, 4)  # side contact with the wall
    config = SolverConfig(
        torque_z=torque_z,
        paper_knob_rule=paper_knob_rule,
        rotate_contact_pattern=rotate_contact_pattern,
        ground_pull=ground_pull,
    )
    reduced = build_reduced_model(layout, config)
    assert reduced is not None
    exact = build_model_from_config(layout, config)
    assert reduced.expansion.shape == (exact.var_count, reduced.var_count)
    assert reduced.a_matrix.shape == (
        exact.a_matrix.shape[0],
        reduced.var_count,
    )
    np.testing.assert_array_equal(reduced.b_vector, exact.b_vector)


def test_var_census_single_knob_tower(layout):
    # 1x1 tower: two connections (ground->lower, lower->upper), each a
    # FOUR_POINT knob: 6 field coefficients + 4 constant press fields.
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", 0, 0, 3, 0, 4)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    assert set(reduced.connection_pairs) == {
        (GROUND_ID, reduced.brick_ids[0]),
        (reduced.brick_ids[0], reduced.brick_ids[1]),
    }
    assert reduced.var_count == 2 * (2 * 3 + 4 * 1)
    assert reduced.exact.var_count == 2 * (4 * 2 + 4)


def test_var_census_wide_interface_shrinks(layout):
    # 2x4 on 2x4: 8 mated knobs collapse to one connection's 6 field
    # coefficients + 4 press fields of width 3 — 36 vars vs the exact
    # model's 160.
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    assert reduced.exact.var_count == 2 * 8 * (3 * 2 + 4)
    assert reduced.var_count == 2 * (2 * 3 + 4 * 3)
    assert reduced.var_count < reduced.exact.var_count


def test_constant_field_expands_to_unit_point_values(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    exact = reduced.exact
    for ordinal in range(len(reduced.connection_pairs)):
        # Constant drag field = 1 on one connection: every one of that
        # connection's drag points evaluates to 1, everything else to 0.
        x = np.zeros(reduced.var_count)
        block = ordinal * (2 * 3 + 4 * 3)
        x[block + 3] = 1.0  # drag field constant coefficient
        expanded = reduced.expansion @ x
        mask = reduced.drag_connection == ordinal
        drags = expanded[exact.drag_cols]
        np.testing.assert_allclose(drags[mask], 1.0)
        np.testing.assert_allclose(drags[~mask], 0.0)
        normals = expanded[exact.normal_cols]
        np.testing.assert_allclose(normals, 0.0)


def test_linear_field_expands_to_centered_offsets(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    exact = reduced.exact
    # u-coefficient of the brick-pair connection's normal field: values
    # are the centered x offsets in studs — zero mean over the
    # interface, both signs present (the THREE_POINT pattern is
    # asymmetric, so no stronger symmetry holds).
    pair_ordinal = next(
        i for i, pair in enumerate(reduced.connection_pairs) if pair[0] != GROUND_ID
    )
    x = np.zeros(reduced.var_count)
    x[pair_ordinal * (2 * 3 + 4 * 3) + 1] = 1.0  # normal field u coefficient
    expanded = reduced.expansion @ x
    normals = expanded[exact.normal_cols]
    mask = reduced.drag_connection == pair_ordinal
    values = normals[mask]
    assert values.sum() == pytest.approx(0.0, abs=1e-12)
    assert values.max() > 0.0 > values.min()


def test_vertical_expansion_pins_pattern_geometry(layout):
    # Freeze the vertical branch's field semantics against refactors: on
    # a 2x2-on-2x2 stack every knob takes THREE_POINT_OFFSETS, so each
    # normal row of E must be exactly [1, x - cx, y - cy] at the world
    # pattern point, hand-reconstructed here from the same constants.
    from legolization.stability.constants import THREE_POINT_OFFSETS

    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    layout.add("brick_2x2", 0, 0, 3, 0, 4)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    graph = ConnectionGraph.from_layout(layout)
    pair_points: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for knob in graph.knob_contacts:
        for ox, oy in THREE_POINT_OFFSETS:
            pair_points.setdefault((knob.below_id, knob.above_id), []).append(
                (knob.x + ox, knob.y + oy)
            )
    dense = reduced.expansion.toarray()
    block = 2 * 3 + 4 * 3  # per-connection columns on a 4-knob interface
    for ordinal, pair in enumerate(reduced.connection_pairs):
        points = pair_points[pair]
        cx = sum(x for x, _ in points) / len(points)
        cy = sum(y for _, y in points) / len(points)
        normal_cols = [
            p.normal_col
            for p in reduced.exact.contact_points
            if (p.below_id, p.above_id) == pair
        ]
        for row, (x, y) in zip(normal_cols, points, strict=True):
            expected = np.zeros(reduced.var_count)
            expected[ordinal * block : ordinal * block + 3] = (1.0, x - cx, y - cy)
            np.testing.assert_allclose(dense[row], expected, atol=1e-12)


def test_reduced_residual_matches_exact_expansion(layout):
    _wall(layout)
    config = SolverConfig()
    reduced = build_reduced_model(layout, config)
    assert reduced is not None
    rng = np.random.default_rng(0)
    for _ in range(5):
        x = rng.normal(size=reduced.var_count)
        direct = reduced.a_matrix @ x
        via_expansion = reduced.exact.a_matrix @ (reduced.expansion @ x)
        np.testing.assert_allclose(direct, via_expansion, atol=1e-12)


def test_drag_connection_groups_match_contact_pairs(layout):
    _wall(layout)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    points = reduced.exact.contact_points
    assert reduced.drag_connection.shape == (len(points),)
    for point, ordinal in zip(points, reduced.drag_connection, strict=True):
        assert reduced.connection_pairs[ordinal] == (
            point.below_id,
            point.above_id,
        )


def test_ground_pull_off_still_builds(layout):
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    with_pull = build_reduced_model(layout, SolverConfig(ground_pull=True))
    without = build_reduced_model(layout, SolverConfig(ground_pull=False))
    assert with_pull is not None
    assert without is not None
    # Same variable structure; only the exact A entries differ.
    assert with_pull.var_count == without.var_count
    assert (with_pull.expansion != without.expansion).nnz == 0


def test_lateral_layout_builds(layout):
    # One ground (vertical, FOUR_POINT) connection + one lateral
    # connection: 10 reduced vars each (6 field coefficients + 4
    # width-1 press/shear fields) against 24 exact columns.
    layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    tile = layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    assert reduced.exact.var_count == 2 * (4 * 2 + 4)
    assert reduced.var_count == 2 * (2 * 3 + 4 * 1)
    lateral_ordinals = {
        int(ordinal)
        for point, ordinal in zip(
            reduced.exact.contact_points, reduced.drag_connection, strict=True
        )
        if point.above_id == tile.brick_id
    }
    assert len(lateral_ordinals) == 1


def test_lateral_field_lives_in_the_mating_plane(layout):
    # The coincidence-hazard killer: a lateral knob allocates exactly as
    # many columns as a four-point vertical knob, so only the field
    # coordinates can prove the branch. Two side studs one stud apart
    # (normal (0,-1,0)) must form ONE connection whose normal-field u
    # spans the transverse axis (offsets one stud apart) and whose v
    # carries the diamond's vertical offsets.
    from legolization.stability.constants import FOUR_POINT_OFFSETS

    layout.add("brick_1x2_side_studs", 0, 1, 0, 0, 4)
    tile = layout.add("tile_1x2_snot", 0, 0, 0, 0, 4)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    lateral_points = [
        (point, int(ordinal))
        for point, ordinal in zip(
            reduced.exact.contact_points, reduced.drag_connection, strict=True
        )
        if point.above_id == tile.brick_id
    ]
    ordinals = {ordinal for _, ordinal in lateral_points}
    assert len(ordinals) == 1  # both side studs share one connection
    # Expected field offsets: knob transverse centers 0.0 and 1.0
    # (centroid 0.5) plus the FOUR_POINT diamond in (transverse,
    # vertical) coordinates.
    expected = sorted(
        (t_center - 0.5 + ox, oy)
        for t_center in (0.0, 1.0)
        for ox, oy in FOUR_POINT_OFFSETS
    )
    dense = reduced.expansion.toarray()
    first_row = lateral_points[0][0].normal_col
    block = next(j for j in range(reduced.var_count) if dense[first_row, j])
    observed = sorted(
        (
            float(dense[point.normal_col, block + 1]),
            float(dense[point.normal_col, block + 2]),
        )
        for point, _ in lateral_points
    )
    np.testing.assert_allclose(observed, expected, atol=1e-12)


def test_contact_free_layout_declines(layout):
    layout.add("brick_2x4", 0, 0, 9, 0, 4)  # floating, no contacts at all
    graph = ConnectionGraph.from_layout(layout)
    assert not graph.knob_contacts
    assert build_reduced_model(layout, SolverConfig()) is None


def test_bricksim_fields_use_their_own_builder(layout):
    # The restricted builder still declines the research basis; the
    # screen routes it to build_bricksim_model instead: 12 coefficients
    # per connection (alpha/beta/gamma + the co-located compression
    # field) and per-point scalar maps.
    from legolization.stability.bricksim_fields import build_bricksim_model

    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    config = SolverConfig(screen_fields="bricksim")
    assert build_reduced_model(layout, config) is None
    model = build_bricksim_model(layout, config)
    assert model is not None
    assert model.var_count == 12  # one ground connection
    assert model.axial.shape == (4, 12)  # FOUR_POINT pattern
    assert model.compression.shape == (4, 12)
    # Tension and compression fields push along opposite axes: their
    # net-force columns must be exact negatives on the fz row.
    fz = model.a_matrix.toarray()[2]
    np.testing.assert_allclose(fz[0], -fz[9], atol=1e-12)


def test_bricksim_screen_reports_lateral_like_restricted(layout):
    # Rank-rejection is scoped to vertical-only layouts, so the lateral
    # flag has to survive the research basis too: reporting False on a
    # clad layout drops it into the stress-margin clause that
    # ScreenReport.lateral exists to disable (and silently un-scopes the
    # false-reject measurement in scripts/benchmark_screen.py).
    from legolization.stability.screen import screen_layout

    bases = (
        SolverConfig(screen_fields="restricted"),
        SolverConfig(screen_fields="bricksim"),
    )

    vertical = Layout(catalog=default_catalog())
    vertical.add("brick_2x4", 0, 0, 0, 0, 4)
    for config in bases:
        report = screen_layout(vertical, config)
        assert report.status == "ok"
        assert not report.lateral

    layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    for config in bases:
        report = screen_layout(layout, config)
        assert report.status == "ok"
        assert report.lateral


def test_bricksim_screen_matches_cold_verdicts(layout):
    from legolization.stability import analyze
    from legolization.stability.screen import screen_layout

    config = SolverConfig(screen_fields="bricksim")
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    cold = analyze(layout)
    report = screen_layout(layout, config)
    assert report.status == "ok"
    assert report.stable == cold.stable

    floating = Layout(catalog=default_catalog())
    floating.add("brick_2x4", 0, 0, 0, 0, 4)
    floater = floating.add("brick_2x4", 20, 20, 9, 0, 4)
    report = screen_layout(floating, config)
    assert report.status == "ok"
    assert not report.stable
    assert floater.brick_id in report.unstable_ids


def test_side_contacts_keep_identity_columns(layout):
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 1, 0, 0, 4)
    config = SolverConfig(torque_z=False)
    reduced = build_reduced_model(layout, config)
    assert reduced is not None
    graph = ConnectionGraph.from_layout(layout)
    assert graph.side_contacts
    side_vars = 2 * len(graph.side_contacts)
    tail = reduced.expansion[:, -side_vars:].toarray()
    # Each side generator keeps its own reduced column (identity block).
    assert np.count_nonzero(tail) == side_vars
    np.testing.assert_allclose(tail[tail != 0.0], 1.0)


def test_hull_vertices_shapes():
    from legolization.stability.reduced import _hull_vertices

    # Collinear 1xN row: only the two extremes.
    row = [(float(i), 0.0) for i in range(5)]
    assert _hull_vertices(row) == {0, 4}
    # Two or fewer unique points: everything.
    assert _hull_vertices([(0.0, 0.0), (1.0, 1.0)]) == {0, 1}
    assert _hull_vertices([(0.0, 0.0), (0.0, 0.0)]) == {0, 1}
    # Square with an interior point: the interior index drops.
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (1.0, 1.0)]
    assert _hull_vertices(square) == {0, 1, 2, 3}
    # Duplicates of a hull coordinate all stay; interior points drop.
    doubled = [(0.0, 0.0), (2.0, 0.0), (1.0, 1.0), (0.0, 0.0), (0.9, 0.3)]
    assert _hull_vertices(doubled) == {0, 1, 2, 3}


def test_constraint_mask_shrinks_wide_interfaces(layout):
    # plate_2x16 on plate_2x16: 32 knobs x 3 points collapse to a
    # hull-vertex band; the mask must cut the pointwise rows hard while
    # keeping every side row (none here) and staying exact.
    layout.add("plate_2x16", 0, 0, 0, 0, 4)
    layout.add("plate_2x16", 0, 0, 1, 0, 4)
    reduced = build_reduced_model(layout, SolverConfig())
    assert reduced is not None
    kept = int(reduced.constraint_mask.sum())
    assert kept < reduced.exact.var_count / 3
    # Drag rows on the hull keep their dmax coverage: at least one drag
    # per connection stays masked in.
    drag_mask = reduced.constraint_mask[reduced.exact.drag_cols]
    for ordinal in range(len(reduced.connection_pairs)):
        assert drag_mask[reduced.drag_connection == ordinal].any()


def test_masked_screen_matches_full_rows(layout):
    from dataclasses import replace as dc_replace

    import numpy as np

    from legolization.stability.screen import solve_screen

    _wall(layout)
    layout.add("brick_2x2", 0, 1, 0, 90, 4)
    config = SolverConfig()
    reduced = build_reduced_model(layout, config)
    assert reduced is not None
    full = dc_replace(
        reduced, constraint_mask=np.ones(reduced.exact.var_count, dtype=bool)
    )
    masked_report = solve_screen(reduced, config)
    full_report = solve_screen(full, config)
    assert masked_report.status == full_report.status == "ok"
    assert masked_report.stable == full_report.stable
    assert masked_report.q == pytest.approx(full_report.q, abs=5e-3)
    assert masked_report.scores is not None
    assert full_report.scores is not None
    for bid, score in full_report.scores.items():
        assert masked_report.scores[bid] == pytest.approx(score, abs=5e-3)
