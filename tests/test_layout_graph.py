"""Layout occupancy and connection-graph extraction."""

import pytest

from legolization.catalog import default_catalog
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.layout import CollisionError, Layout


@pytest.fixture
def layout():
    return Layout(catalog=default_catalog())


def test_add_and_collision(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    assert len(layout) == 1
    with pytest.raises(CollisionError, match="collides"):
        layout.add("plate_1x1", 3, 1, 2, 0, 4)
    with pytest.raises(CollisionError, match="below ground"):
        layout.add("plate_1x1", 9, 9, -1, 0, 4)


def test_remove_frees_cells(layout):
    brick = layout.add("brick_2x2", 0, 0, 0, 0, 4)
    layout.remove(brick.brick_id)
    assert len(layout) == 0
    assert not layout.occupancy
    layout.add("brick_2x2", 0, 0, 0, 0, 4)  # no collision anymore


def test_yaw_90_occupies_rotated_cells(layout):
    brick = layout.add("brick_1x4", 0, 0, 0, 90, 4)
    columns = {(x, y) for x, y, _ in layout.cells_of(brick)}
    assert columns == {(0, 0), (0, 1), (0, 2), (0, 3)}


def test_stacked_bricks_connect(layout):
    lower = layout.add("brick_2x4", 0, 0, 0, 0, 4)
    upper = layout.add("brick_2x4", 0, 0, 3, 0, 14)
    graph = ConnectionGraph.from_layout(layout)
    assert (lower.brick_id, upper.brick_id) in graph.support_edges()
    assert graph.grounded_ids == {lower.brick_id}
    assert graph.component_count() == 1
    assert not graph.floating_ids()
    interface = [k for k in graph.knob_contacts if k.below_id == lower.brick_id]
    assert len(interface) == 8
    ground = [k for k in graph.knob_contacts if k.below_id == GROUND_ID]
    assert len(ground) == 8


def test_brick_on_tile_does_not_connect(layout):
    layout.add("tile_2x2", 0, 0, 0, 0, 4)
    upper = layout.add("brick_2x2", 0, 0, 1, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    assert upper.brick_id in graph.floating_ids()


def test_side_contacts(layout):
    a = layout.add("brick_1x2", 0, 0, 0, 0, 4)
    b = layout.add("brick_1x2", 0, 1, 0, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    assert len(graph.side_contacts) == 1
    contact = graph.side_contacts[0]
    assert {contact.a_id, contact.b_id} == {a.brick_id, b.brick_id}
    assert contact.axis == 1
    assert contact.face_count == 2 * 3  # two columns, three plate layers
    assert (contact.z_lo, contact.z_hi) == (0, 2)


def test_floating_component(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 10, 10, 6, 0, 4)  # in the air, no support
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 2
    assert len(graph.floating_ids()) == 1


def test_disconnected_grounded_towers_are_two_components(layout):
    # The audit's F4 repro: both towers stand on the ground, but nothing
    # ties them together — the ground node must not merge them.
    for level in range(2):
        layout.add("brick_2x2", 0, 0, 3 * level, 0, 4)
        layout.add("brick_2x2", 10, 10, 3 * level, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 2
    assert not graph.floating_ids()
    assert len(set(graph.brick_components().values())) == 2


def test_empty_layout_has_zero_components(layout):
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 0
    assert graph.brick_components() == {}
