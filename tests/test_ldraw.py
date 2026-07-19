"""LDraw emission: coordinates, rotation, steps, and file structure."""

import pytest

from legolization.catalog import default_catalog
from legolization.layout import Layout
from legolization.ldraw_out import model_lines, piece_for, write_model


@pytest.fixture
def layout():
    return Layout(catalog=default_catalog())


def _type1_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("1 ")]


def test_plate_at_origin(layout):
    brick = layout.add("plate_1x1", 0, 0, 0, 0, 4)
    line = piece_for(layout, brick).to_ldraw()
    assert line == "1 4 0 -8 0 1 0 0 0 1 0 0 0 1 3024.dat"


def test_brick_height_and_center(layout):
    brick = layout.add("brick_2x4", 0, 0, 0, 0, 1)
    line = piece_for(layout, brick).to_ldraw()
    # 4 studs along x centered at 1.5 studs = 30 LDU; 2 in y = 10 LDU.
    assert line.split()[2:5] == ["30", "-24", "10"]


def test_stacked_plate_y(layout):
    brick = layout.add("plate_1x1", 0, 0, 5, 0, 4)
    line = piece_for(layout, brick).to_ldraw()
    assert line.split()[3] == "-48"  # top face at -8·(5+1)


def test_yaw_rotation_matrix(layout):
    brick = layout.add("brick_1x4", 2, 3, 0, 90, 4)
    fields = piece_for(layout, brick).to_ldraw().split()
    # Footprint occupies (2,3)..(2,6): center x=2 → 40, y=4.5 → 90.
    assert fields[2:5] == ["40", "-24", "90"]
    assert fields[5:14] == ["0", "0", "-1", "0", "1", "0", "1", "0", "0"]


def test_slope_origin_lands_on_stud_cell(layout):
    brick = layout.add("slope_45_2x1", 0, 0, 0, 0, 4)
    fields = piece_for(layout, brick).to_ldraw().split()
    # Stud cell is local (0,1) → world (0,1): X=0, Z=20, brick height.
    assert fields[2:5] == ["0", "-24", "20"]
    assert fields[14] == "3040b.dat"


def test_steps_between_layers(layout):
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 0, 4)
    layout.add("plate_2x4", 0, 0, 6, 0, 4)
    lines = list(model_lines(layout))
    assert lines.count("0 STEP") == 3
    type1 = _type1_lines(lines)
    assert len(type1) == 3
    # Bottom-up: Y strictly decreases (up is negative).
    ys = [float(line.split()[3]) for line in type1]
    assert ys == sorted(ys, reverse=True)


def test_write_ldr_and_mpd(layout, tmp_path):
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    ldr = tmp_path / "out.ldr"
    write_model(layout, ldr)
    content = ldr.read_text()
    assert content.startswith("0 out")
    assert "3005.dat" in content

    mpd = tmp_path / "out.mpd"
    write_model(layout, mpd)
    mpd_lines = mpd.read_text().splitlines()
    assert mpd_lines[0] == "0 FILE out.mpd"
    assert mpd_lines[-1] == "0 NOFILE"


def test_roundtrip_through_pyldraw3(layout, tmp_path):
    from ldraw.model import read_model
    from ldraw.pieces import Piece

    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("plate_2x4", 0, 0, 3, 90, 14)
    layout.add("slope_45_2x1", 4, 0, 0, 180, 15)
    path = tmp_path / "roundtrip.ldr"
    write_model(layout, path)
    model = read_model(path)
    pieces = [obj for obj in model.objects if isinstance(obj, Piece)]
    assert len(pieces) == 3
    assert sorted(p.part for p in pieces) == ["3001", "3020", "3040b"]


def _subassembly_layout(layout: Layout) -> Layout:
    for level in (0, 3, 6):
        layout.add("brick_2x2", 3, 3, level, 0, 15)  # stem
    layout.add("brick_2x2", 1, 3, 9, 0, 4)  # petal, no support below
    layout.add("brick_2x2", 3, 3, 9, 0, 4)  # hub on the stem
    layout.add("brick_2x2", 2, 3, 12, 0, 4)  # bridge petal to hub
    return layout


def test_mpd_submodel_structure(layout, tmp_path):
    from legolization.instructions import InstructionsConfig, plan_instructions

    _subassembly_layout(layout)
    plan = plan_instructions(
        layout, config=InstructionsConfig(rotstep=False, subassemblies=True)
    )
    assert len(plan.subassemblies) == 1
    sub = plan.subassemblies[0]

    mpd = tmp_path / "model.mpd"
    write_model(layout, mpd, plan=plan)
    lines = mpd.read_text().splitlines()

    file_headers = [line for line in lines if line.startswith("0 FILE ")]
    assert file_headers == ["0 FILE model.mpd", f"0 FILE model-{sub.name}.ldr"]
    assert lines.count("0 NOFILE") == 2

    # The attach step is a single colour-16 reference at -8 * anchor_layer.
    reference = next(
        line for line in _type1_lines(lines) if line.endswith(f"model-{sub.name}.ldr")
    )
    fields = reference.split()
    assert fields[:5] == ["1", "16", "0", str(-8 * sub.anchor_layer), "0"]
    assert fields[5:14] == ["1", "0", "0", "0", "1", "0", "0", "0", "1"]

    # Main-file steps == main + attach steps; sub file carries its own steps.
    boundary = lines.index(f"0 FILE model-{sub.name}.ldr")
    main_lines, sub_lines = lines[:boundary], lines[boundary:]
    assert main_lines.count("0 STEP") == len(plan.main_steps())
    assert sub_lines.count("0 STEP") == len(plan.sub_steps(sub.name))

    # Sub bricks are emitted in the local grounded frame: the unit's lowest
    # brick tops out at y = -8 * height, exactly as if it sat on the table.
    sub_type1 = _type1_lines(sub_lines)
    assert len(sub_type1) == len(sub.brick_ids)
    ys = [float(line.split()[3]) for line in sub_type1]
    assert max(ys) == -24.0  # brick_2x2 on the table


def test_ldr_fallback_flattens_submodels(layout, tmp_path):
    from legolization.instructions import InstructionsConfig, plan_instructions

    _subassembly_layout(layout)
    plan = plan_instructions(
        layout, config=InstructionsConfig(rotstep=False, subassemblies=True)
    )
    assert plan.subassemblies

    ldr = tmp_path / "model.ldr"
    write_model(layout, ldr, plan=plan)
    lines = ldr.read_text().splitlines()
    assert not any(line.startswith("0 FILE") for line in lines)
    type1 = _type1_lines(lines)
    assert len(type1) == len(layout.bricks)
    assert all(line.endswith(".dat") for line in type1)
