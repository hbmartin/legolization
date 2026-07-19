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
    # The bracket's protruding stud sweeps the tile's column on the way
    # down, so the tile blocks the bracket (the support edge already
    # orders bracket first — consistent, not a deadlock).
    assert blockers[bracket.brick_id] == frozenset({tile.brick_id})
    assert blockers[clear.brick_id] == frozenset()


# --- the pass ---


def test_pass_clads_bonded_walls():
    # v1 refused to carve wall-spanning donors outright; v2 carves them
    # under the per-mount re-bond guard instead. On a free-standing
    # 1x4 wall every ground-course column ends up clad (end-face
    # bracket, a fallback single after the pair failed, an 11211 pair)
    # plus a staggered single on the next course (running bond); the
    # guard rejects every mount that would sever the wall: side-by-side
    # SNOT columns share no studs, so cladding both courses of one
    # column always splits a 1-deep wall.
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 0, 0, 3, 0, 4)
    codes = np.full((4, 1, 6), EMPTY, dtype=np.int16)
    codes[:, :, :] = 4
    assert apply_snot(layout, VoxelGrid(codes=codes)) == 4
    placed = sorted((b.part_key, b.x, b.y, b.layer) for b in layout)
    assert placed == [
        ("brick_1x1_side_stud", 0, 0, 0),  # end face, course 0
        ("brick_1x1_side_stud", 1, 0, 0),  # fallback single
        ("brick_1x1_side_stud", 3, 0, 3),  # staggered course 1
        ("brick_1x2_side_studs", 2, 0, 0),
        ("brick_1x3", 0, 0, 3),
        ("tile_1x1_snot", -1, 0, 0),
        ("tile_1x1_snot", 1, -1, 0),
        ("tile_1x1_snot", 3, -1, 3),
        ("tile_1x2_snot", 2, -1, 0),
    ]
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 1
    assert not graph.floating_ids()
    assert analyze(layout).stable


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


def test_cladding_creates_no_phantom_side_contacts():
    # The tile's occupied cells are a conservative collision prism, not
    # physical volume: its only physical connection is the lateral stud
    # (PR #17 review — the prism used to add a generic side contact
    # alongside the knob mate, shifting the drag numbers).
    layout = Layout(catalog=default_catalog())
    bracket = layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    tile = layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    assert not any(
        {contact.a_id, contact.b_id} & {tile.brick_id}
        for contact in graph.side_contacts
    )
    lateral = [k for k in graph.knob_contacts if k.normal != (0, 0, 1)]
    assert len(lateral) == 1
    assert lateral[0].below_id == bracket.brick_id


def test_mpd_stem_is_case_insensitive(tmp_path):
    # MODEL.MPD used to reference MODEL.MPD-sub-1.ldr while defining
    # 0 FILE MODEL-sub-1.ldr, losing the subassembly in viewers.

    from legolization.instructions import InstructionsConfig, plan_instructions
    from legolization.ldraw_out import write_model

    layout = Layout(catalog=default_catalog())
    for level in (0, 3, 6):
        layout.add("brick_2x2", 3, 3, level, 0, 15)
    layout.add("brick_2x2", 1, 3, 9, 0, 4)
    layout.add("brick_2x2", 3, 3, 9, 0, 4)
    layout.add("brick_2x2", 2, 3, 12, 0, 4)
    plan = plan_instructions(
        layout, config=InstructionsConfig(rotstep=False, subassemblies=True)
    )
    assert plan.subassemblies
    path = tmp_path / "MODEL.MPD"
    write_model(layout, path, plan=plan)
    text = path.read_text()
    assert "0 FILE MODEL-sub-1.ldr" in text
    assert "MODEL.MPD-sub-1" not in text
    reference_lines = [
        line
        for line in text.splitlines()
        if line.startswith("1 16") and line.endswith(".ldr")
    ]
    assert reference_lines
    assert all(line.endswith("MODEL-sub-1.ldr") for line in reference_lines)


def test_v1_emitted_file_imports_identically():
    # tests/data/snot_tower_v1.ldr was written by the v1 (pre-data-driven)
    # emission path; the generalized importer must decode it to the same
    # layout the v1 importer produced. Guards emission and import from
    # drifting together while the pinned bytes stay green.
    from pathlib import Path

    imported = layout_from_ldraw(Path(__file__).parent / "data" / "snot_tower_v1.ldr")
    placements = sorted(
        (b.part_key, b.x, b.y, b.layer, b.yaw, b.colour_code) for b in imported
    )
    assert placements == [
        ("brick_1x1_side_stud", 0, 0, 0, 180, 4),
        ("brick_1x1_side_stud", 0, 0, 3, 180, 4),
        ("tile_1x1_snot", -1, 0, 0, 180, 4),
        ("tile_1x1_snot", -1, 0, 3, 180, 4),
    ]


# --- v2 parts: 11211 carrier + sideways 1x2 tile ---


def test_two_stud_carrier_modelled():
    catalog = default_catalog()
    carrier = catalog["brick_1x2_side_studs"]
    ups = [c for c in carrier.top_connectors if c.direction == (0, 0, 1)]
    laterals = [c for c in carrier.top_connectors if c.direction[2] == 0]
    assert len(ups) == 2
    assert [c.cell for c in laterals] == [(0, 0, 1), (1, 0, 1)]
    assert all(c.direction == (0, -1, 0) for c in laterals)
    assert len(carrier.bottom_connectors) == 2
    tile = catalog["tile_1x2_snot"]
    assert len(tile.occupied_cells) == 6  # two conservative 3-plate windows
    assert tile.filled_cells == frozenset({(0, 0, 1), (1, 0, 1)})
    assert [c.direction for c in tile.bottom_connectors] == [(0, 1, 0), (0, 1, 0)]


def test_two_stud_carrier_mates_both_sockets():
    layout = Layout(catalog=default_catalog())
    carrier = layout.add("brick_1x2_side_studs", 0, 1, 0, 0, 4)
    tile = layout.add("tile_1x2_snot", 0, 0, 0, 0, 4)
    graph = ConnectionGraph.from_layout(layout)
    lateral = [k for k in graph.knob_contacts if k.normal != (0, 0, 1)]
    assert len(lateral) == 2  # one KnobContact per stud pair
    assert all(
        (k.below_id, k.above_id, k.normal)
        == (carrier.brick_id, tile.brick_id, (0, -1, 0))
        for k in lateral
    )
    assert graph.component_count() == 1
    assert graph.grounded_ids == {carrier.brick_id}
    assert not graph.floating_ids()
    assert analyze(layout).stable


@pytest.mark.parametrize("yaw", [0, 90, 180, 270])
@pytest.mark.parametrize("key", ["brick_1x2_side_studs", "tile_1x2_snot"])
def test_v2_parts_roundtrip_through_import(key, yaw, tmp_path):
    layout = Layout(catalog=default_catalog())
    layout.add(key, 5, 5, 3, yaw, 4)
    path = tmp_path / "part.ldr"
    write_model(layout, path)
    back = layout_from_ldraw(path)
    assert [(b.part_key, b.x, b.y, b.layer, b.yaw) for b in back] == [
        (key, 5, 5, 3, yaw)
    ]


@pytest.mark.parametrize("yaw", [0, 90])
def test_flat_tile_1x2_still_imports_flat(yaw, tmp_path):
    # 3069b is shared between tile_1x2 and its sideways twin; the flat
    # orientation must keep decoding as the flat part (decode sets are
    # disjoint: yaw matrices have middle row (0, 1, 0), the pinned mount
    # matrices never do).
    layout = Layout(catalog=default_catalog())
    layout.add("tile_1x2", 2, 3, 6, yaw, 4)
    path = tmp_path / "flat.ldr"
    write_model(layout, path)
    back = layout_from_ldraw(path)
    assert [(b.part_key, b.yaw) for b in back] == [("tile_1x2", yaw)]


def test_carrier_stud_sweep_is_blocked_from_above():
    # The carrier's side stud protrudes into the neighbour column during
    # its vertical insertion: a brick at or above the stud's height in
    # that column blocks the carrier; one below does not. Latent in
    # 87087 (one stud) exactly as in 11211 (two).
    layout = Layout(catalog=default_catalog())
    carrier = layout.add("brick_1x1_side_stud", 0, 1, 0, 0, 4)  # stud -> (1, 1)...
    blocker = layout.add("brick_1x1", 1, 1, 3, 0, 4)
    below = layout.add("plate_1x1", 1, 1, 0, 0, 4)
    blockers = vertical_blockers(layout)
    stud = next(
        c for c in layout.connectors_of(carrier, top=True) if c.direction[2] == 0
    )
    target = (stud.cell[0] + stud.direction[0], stud.cell[1] + stud.direction[1])
    assert target == (1, 1)
    assert blocker.brick_id in blockers[carrier.brick_id]
    assert below.brick_id not in blockers[carrier.brick_id]


def test_two_stud_carrier_sweeps_both_columns():
    layout = Layout(catalog=default_catalog())
    carrier = layout.add("brick_1x2_side_studs", 0, 1, 0, 0, 4)  # studs -> (0,0),(1,0)
    left = layout.add("brick_1x1", 0, 0, 3, 0, 4)
    right = layout.add("brick_1x1", 1, 0, 3, 0, 4)
    blockers = vertical_blockers(layout)
    assert {left.brick_id, right.brick_id} <= blockers[carrier.brick_id]
