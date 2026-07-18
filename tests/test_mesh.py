"""Mesh front-end: voxelization, orientation, CLI wiring."""

import numpy as np
import pytest
import trimesh
from scipy import ndimage

from legolization.grid import EMPTY, VoxelGrid
from legolization.main import main
from legolization.mesh import MeshOptions, grid_from_mesh, mesh_to_grid
from legolization.pipeline import PipelineConfig, load_grid, run_file

# Extents chosen to keep faces off exact voxel boundaries.
_BOX_EXTENTS = (3.9, 1.9, 0.9)


def _box() -> trimesh.Trimesh:
    return trimesh.creation.box(extents=_BOX_EXTENTS)


def test_box_shape_and_counts():
    grid = grid_from_mesh(_box(), options=MeshOptions(target_studs=8))
    assert grid.shape == (9, 5, 5)
    assert grid.filled_count == 225  # solid box: every cell filled


def test_cube_voxelizes_at_plate_resolution():
    # The 2.5x pre-stretch: a cube must come out ~2.5x taller in layers
    # than it is wide in studs (boundary padding skews the ratio slightly).
    cube = trimesh.creation.box(extents=(4.0, 4.0, 4.0))
    grid = grid_from_mesh(cube, options=MeshOptions(target_studs=10))
    nx, _ny, nlayers = grid.shape
    assert 2.0 < nlayers / nx <= 2.5


def test_pitch_overrides_target_studs():
    grid = grid_from_mesh(
        _box(),
        options=MeshOptions(target_studs=32, pitch=1.0),
    )
    assert grid.shape[0] <= 6  # 3.9 units at 1 unit/stud, not 32 studs


def test_zero_horizontal_extent_raises():
    degenerate = trimesh.Trimesh(
        vertices=[(0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 2.0)],
        faces=[[0, 1, 2]],
        process=False,
    )
    with pytest.raises(ValueError, match="no horizontal extent"):
        grid_from_mesh(degenerate)


def test_up_axis_y_moves_long_axis_into_layers():
    tall_in_y = trimesh.creation.box(extents=(1.9, 3.9, 0.9))
    grid = grid_from_mesh(tall_in_y, options=MeshOptions(target_studs=4, up="y"))
    nx, ny, nlayers = grid.shape
    assert nlayers > nx
    assert nlayers > ny


def test_uniform_colour_applied():
    grid = grid_from_mesh(
        _box(),
        options=MeshOptions(target_studs=6, colour_code=4),
    )
    filled = grid.codes[grid.filled_mask]
    assert (filled == 4).all()


def test_bad_colour_raises():
    with pytest.raises(ValueError, match="unknown LDraw colour code"):
        grid_from_mesh(_box(), options=MeshOptions(colour_code=99_999))


def test_no_fill_leaves_shell():
    sphere = trimesh.creation.icosphere(subdivisions=2, radius=1.0)
    solid = grid_from_mesh(sphere, options=MeshOptions(target_studs=10))
    shell = grid_from_mesh(
        sphere,
        options=MeshOptions(target_studs=10, fill=False),
    )
    assert shell.filled_count < solid.filled_count
    centre = tuple(dim // 2 for dim in solid.shape)
    assert solid.codes[centre] != EMPTY
    assert shell.codes[centre] == EMPTY


def test_largest_component_filter():
    two = trimesh.util.concatenate(
        [
            trimesh.creation.box(extents=(2.0, 2.0, 2.0)),
            trimesh.creation.box(
                extents=(1.0, 1.0, 1.0),
                transform=trimesh.transformations.translation_matrix((5.0, 0.0, 0.0)),
            ),
        ]
    )
    messages: list[str] = []
    grid = grid_from_mesh(
        two,
        options=MeshOptions(target_studs=12),
        progress=messages.append,
    )
    structure = ndimage.generate_binary_structure(rank=3, connectivity=1)
    _labels, components = ndimage.label(grid.filled_mask, structure=structure)
    assert components == 1
    assert any("dropped" in message for message in messages)


def test_grid_dim_cap_raises():
    with pytest.raises(ValueError, match="reduce --target-studs"):
        grid_from_mesh(_box(), options=MeshOptions(pitch=0.001))


def test_determinism():
    first = grid_from_mesh(_box(), options=MeshOptions(target_studs=8))
    second = grid_from_mesh(_box(), options=MeshOptions(target_studs=8))
    assert np.array_equal(first.codes, second.codes)


def test_load_grid_dispatches_mesh_suffix(tmp_path):
    path = tmp_path / "box.stl"
    _box().export(path)
    grid = load_grid(path, PipelineConfig(mesh=MeshOptions(target_studs=6)))
    assert isinstance(grid, VoxelGrid)
    assert grid.filled_count > 0


def test_mesh_to_grid_reads_obj(tmp_path):
    path = tmp_path / "box.obj"
    _box().export(path)
    grid = mesh_to_grid(path, options=MeshOptions(target_studs=6))
    assert grid.filled_count > 0


def test_run_file_mesh_end_to_end(tmp_path):
    path = tmp_path / "box.stl"
    _box().export(path)
    out = tmp_path / "box.ldr"
    result = run_file(
        path,
        out,
        PipelineConfig(seed=0, mesh=MeshOptions(target_studs=6)),
    )
    assert out.exists()
    assert result.buildable


def test_cli_mesh_flags_reject_voxel_input(tmp_path):
    npy = tmp_path / "box.npy"
    np.save(npy, np.full((3, 3, 2), 4, dtype=np.int16))
    with pytest.raises(SystemExit) as excinfo:
        main([str(npy), "--target-studs", "8"])
    assert excinfo.value.code == 2


def test_cli_voxel_flags_reject_mesh_input(tmp_path):
    path = tmp_path / "box.stl"
    _box().export(path)
    with pytest.raises(SystemExit) as excinfo:
        main([str(path), "--plates-per-voxel", "2"])
    assert excinfo.value.code == 2


def test_cli_mesh_happy_path(tmp_path, capsys):
    path = tmp_path / "box.stl"
    _box().export(path)
    out = tmp_path / "box.ldr"
    exit_code = main([str(path), "-o", str(out), "--target-studs", "6"])
    assert exit_code == 0
    assert out.exists()
    assert "wrote" in capsys.readouterr().out
