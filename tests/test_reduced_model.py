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


def test_lateral_layout_declines(layout):
    layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    assert build_reduced_model(layout, SolverConfig()) is None


def test_contact_free_layout_declines(layout):
    layout.add("brick_2x4", 0, 0, 9, 0, 4)  # floating, no contacts at all
    graph = ConnectionGraph.from_layout(layout)
    assert not graph.knob_contacts
    assert build_reduced_model(layout, SolverConfig()) is None


def test_bricksim_fields_not_yet_ported_declines(layout):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    config = SolverConfig(screen_fields="bricksim")
    assert build_reduced_model(layout, config) is None


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
