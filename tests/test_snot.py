"""Sideways (SNOT) parts: catalog, graph, physics, emission, and the pass."""

import numpy as np
import pytest

from legolization.catalog import Category, default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import EMPTY, VoxelGrid
from legolization.instructions import InstructionsConfig, plan_instructions, verify_plan
from legolization.instructions.blocking import vertical_blockers
from legolization.layout import Layout
from legolization.ldraw_in import layout_from_ldraw
from legolization.ldraw_out import piece_for, write_model
from legolization.placement.snot import apply_snot
from legolization.stability.prefix import PrefixSolver
from legolization.stability.solver import SolverConfig, analyze


def _clad_tower(courses: int = 2) -> tuple[Layout, VoxelGrid]:
    layout = Layout(catalog=default_catalog())
    for i in range(courses):
        layout.add("brick_1x1", 0, 0, 3 * i, 0, 4)
    codes = np.full((1, 1, 3 * courses), EMPTY, dtype=np.int16)
    codes[:, :, :] = 4
    return layout, VoxelGrid(codes=codes)


# --- catalog ---


def test_snot_parts_modelled():
    catalog = default_catalog()
    bracket = catalog["brick_1x1_side_stud"]
    tile = catalog["tile_1x1_snot"]
    assert bracket.category is Category.SNOT
    assert tile.category is Category.SNOT
    lateral = [c for c in bracket.top_connectors if c.direction == (1, 0, 0)]
    assert len(lateral) == 1
    assert lateral[0].cell == (0, 0, 1)  # mid-height of the column
    (socket,) = tile.bottom_connectors
    assert socket.direction == (-1, 0, 0)
    assert tile.filled_cells == frozenset({(0, 0, 1)})  # token fill only
    assert len(tile.occupied_cells) == 3  # conservative collision volume


def test_snot_excluded_from_rect_tiling():
    catalog = default_catalog()
    rect_keys = {p.key for p in catalog.by_category(Category.BRICK, Category.PLATE)}
    assert "brick_1x1_side_stud" not in rect_keys
    assert "tile_1x1_snot" not in rect_keys


def test_lateral_connector_rotates_with_yaw():
    catalog = default_catalog()
    bracket = catalog["brick_1x1_side_stud"]
    directions = {
        yaw: next(
            c.direction
            for c in bracket.connectors_at(0, 0, 0, yaw, top=True)
            if c.direction[2] == 0
        )
        for yaw in (0, 90, 180, 270)
    }
    assert directions == {
        0: (1, 0, 0),
        90: (0, 1, 0),
        180: (-1, 0, 0),
        270: (0, -1, 0),
    }


# --- graph ---


def test_lateral_mate_and_grounding():
    layout = Layout(catalog=default_catalog())
    bracket = layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    tile = layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    lateral = [k for k in graph.knob_contacts if k.normal != (0, 0, 1)]
    assert len(lateral) == 1
    assert (lateral[0].below_id, lateral[0].above_id) == (
        bracket.brick_id,
        tile.brick_id,
    )
    assert lateral[0].normal == (1, 0, 0)
    # The tile touches layer 0 but its only connector is lateral: the
    # bracket grounds, the tile hangs from it.
    assert graph.grounded_ids == {bracket.brick_id}
    assert not graph.floating_ids()


def test_orphan_tile_floats():
    layout = Layout(catalog=default_catalog())
    layout.add("tile_1x1_snot", 0, 0, 0, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    assert len(graph.floating_ids()) == 1


# --- physics ---


def test_clad_tower_is_stable():
    layout, grid = _clad_tower()
    assert apply_snot(layout, grid) == 2
    result = analyze(layout)
    assert result.stable
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 1
    assert not graph.floating_ids()


def test_prefix_solver_declines_snot_layouts():
    layout, grid = _clad_tower()
    apply_snot(layout, grid)
    assert PrefixSolver.create(layout, SolverConfig()) is None


# --- emission and import ---


def test_snot_ldraw_lines_pinned():
    layout = Layout(catalog=default_catalog())
    bracket = layout.add("brick_1x1_side_stud", 2, 3, 3, 0, 4)
    tile = layout.add("tile_1x1_snot", 3, 3, 3, 0, 14)
    assert (
        piece_for(layout, bracket).to_ldraw()
        == "1 4 40 -48 60 0 0 -1 0 1 0 1 0 0 87087.dat"
    )
    assert (
        piece_for(layout, tile).to_ldraw()
        == "1 14 58 -36 60 0 -1 0 1 0 0 0 0 1 3070b.dat"
    )


@pytest.mark.parametrize("yaw", [0, 90, 180, 270])
def test_snot_roundtrips_through_import(yaw, tmp_path):
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1_side_stud", 4, 4, 0, yaw, 4)
    dx, dy, _ = (
        default_catalog()["brick_1x1_side_stud"]
        .connectors_at(4, 4, 0, yaw, top=True)[1]
        .direction
    )
    layout.add("tile_1x1_snot", 4 + dx, 4 + dy, 0, yaw, 14)
    path = tmp_path / "snot.ldr"
    write_model(layout, path)
    back = layout_from_ldraw(path)
    key = sorted(
        (b.part_key, b.x, b.y, b.layer, b.yaw % 360, b.colour_code)
        for b in back.bricks.values()
    )
    want = sorted(
        (b.part_key, b.x, b.y, b.layer, b.yaw % 360, b.colour_code)
        for b in layout.bricks.values()
    )
    assert key == want


# --- blocking ---


def test_tile_blockers_follow_the_outward_ray():
    layout = Layout(catalog=default_catalog())
    bracket = layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    tile = layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    wall = layout.add("brick_1x1", 3, 0, 0, 0, 4)  # on the slide-in path
    clear = layout.add("brick_1x1", 0, 2, 0, 0, 4)  # unrelated
    blockers = vertical_blockers(layout)
    assert blockers[tile.brick_id] == frozenset({wall.brick_id})
    assert blockers[bracket.brick_id] == frozenset()
    assert blockers[clear.brick_id] == frozenset()


# --- the pass ---


def test_pass_skips_bonded_walls():
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    codes = np.full((4, 1, 6), EMPTY, dtype=np.int16)
    codes[:, :, :] = 4
    assert apply_snot(layout, VoxelGrid(codes=codes)) == 0
    assert sorted(b.part_key for b in layout) == ["brick_1x4", "brick_1x4"]


def test_pass_respects_min_run():
    layout, grid = _clad_tower(courses=1)
    assert apply_snot(layout, grid) == 0  # a single window is not a run
    layout, grid = _clad_tower(courses=2)
    assert apply_snot(layout, grid) == 2


def test_pass_is_deterministic():
    def run_once() -> list[tuple]:
        layout, grid = _clad_tower(courses=3)
        apply_snot(layout, grid)
        return sorted(
            (b.part_key, b.x, b.y, b.layer, b.yaw, b.colour_code) for b in layout
        )

    assert run_once() == run_once()


def test_clad_tower_sequences_tile_after_bracket():
    layout, grid = _clad_tower()
    apply_snot(layout, grid)
    config = InstructionsConfig(rotstep=False)
    plan = plan_instructions(layout, config=config)
    assert verify_plan(layout, plan, config=config) == []
    step_of = {
        brick_id: step.index for step in plan.steps for brick_id in step.brick_ids
    }
    for tile in (b for b in layout if b.part_key == "tile_1x1_snot"):
        (socket,) = layout.connectors_of(tile, top=False)
        sx, sy, sz = socket.cell
        dx, dy, dz = socket.direction
        mate = layout.brick_at((sx + dx, sy + dy, sz + dz))
        assert mate is not None
        # The bracket is never in a later step than its tile (same step is
        # fine: a step is placed as one unit).
        assert step_of[mate.brick_id] <= step_of[tile.brick_id]


def test_perpendicular_faces_sharing_a_corner_column_do_not_collide():
    # Towers at (0,1) and (1,0): the +x face of one and the +y face of
    # the other both open onto column (1,1). The first mount hangs its
    # tile there; the second must skip, not crash (hit on suzanne).
    layout = Layout(catalog=default_catalog())
    for z in (0, 3):
        layout.add("brick_1x1", 0, 1, z, 0, 4)
        layout.add("brick_1x1", 1, 0, z, 0, 4)
    codes = np.full((2, 2, 6), EMPTY, dtype=np.int16)
    codes[0, 1, :] = 4
    codes[1, 0, :] = 4
    mounted = apply_snot(layout, VoxelGrid(codes=codes))
    assert mounted >= 2
    cells = list(layout.occupancy)
    assert len(cells) == len(set(cells))  # no double occupancy


def test_outward_ray_blockers_reach_beyond_64_studs():
    # A wall 70 studs out is still on the slide-in path; the old fixed
    # 64-cell scan cap approved the impossible insertion (PR #17 review).
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    tile = layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    far_wall = layout.add("brick_1x1", 70, 0, 0, 0, 4)
    blockers = vertical_blockers(layout)
    assert far_wall.brick_id in blockers[tile.brick_id]


def test_pass_never_clads_enclosed_cavities():
    # A hollow shell authored with an EMPTY cavity: v1 mounted tiles
    # inside it while the exterior stayed bare (PR #17 review). Only
    # boundary-connected empty space may be clad into.
    import numpy as np

    from legolization.grid import EMPTY, VoxelGrid
    from legolization.placement.snot import apply_snot

    # 3x3 footprint, 2-course shell of 1x1 columns around an empty core.
    codes = np.full((3, 3, 6), EMPTY, dtype=np.int16)
    layout = Layout(catalog=default_catalog())
    for x in range(3):
        for y in range(3):
            if (x, y) == (1, 1):
                continue  # enclosed cavity column
            codes[x, y, :] = 4
            for z in (0, 3):
                layout.add("brick_1x1", x, y, z, 0, 4)
    grid = VoxelGrid(codes=codes)
    mounted = apply_snot(layout, grid)
    assert mounted > 0  # exterior faces are clad...
    assert layout.brick_at((1, 1, 0)) is None  # ...the cavity stays empty
    for brick in layout:
        if brick.part_key == "tile_1x1_snot":
            assert (brick.x, brick.y) != (1, 1)
