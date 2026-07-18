"""Mesh front-end: voxelization, orientation, CLI wiring."""

from pathlib import Path
from typing import Literal, cast

import numpy as np
import pytest
import trimesh
import trimesh.visual
from scipy import ndimage

from legolization.grid import EMPTY, VoxelGrid
from legolization.main import main
from legolization.mesh import MeshOptions, grid_from_mesh, mesh_to_grid
from legolization.pipeline import PipelineConfig, load_grid, run_file

# Extents chosen to keep faces off exact voxel boundaries.
_BOX_EXTENTS = (3.9, 1.9, 0.9)


def _box() -> trimesh.Trimesh:
    return trimesh.creation.box(extents=_BOX_EXTENTS)


def _two_components() -> trimesh.Trimesh:
    return trimesh.util.concatenate(
        [
            trimesh.creation.box(extents=(2.0, 2.0, 2.0)),
            trimesh.creation.box(
                extents=(1.0, 1.0, 1.0),
                transform=trimesh.transformations.translation_matrix((5.0, 0.0, 0.0)),
            ),
        ]
    )


def test_box_shape_and_counts() -> None:
    grid = grid_from_mesh(_box(), options=MeshOptions(target_studs=8))
    assert grid.shape == (9, 5, 5)
    assert grid.filled_count == 225  # solid box: every cell filled


def test_cube_voxelizes_at_plate_resolution() -> None:
    # The 2.5x pre-stretch: a cube must come out ~2.5x taller in layers
    # than it is wide in studs (boundary padding skews the ratio slightly).
    cube = trimesh.creation.box(extents=(4.0, 4.0, 4.0))
    grid = grid_from_mesh(cube, options=MeshOptions(target_studs=10))
    nx, _ny, nlayers = grid.shape
    assert 2.0 < nlayers / nx <= 2.5


def test_pitch_overrides_target_studs() -> None:
    grid = grid_from_mesh(
        _box(),
        options=MeshOptions(target_studs=32, pitch=1.0),
    )
    assert grid.shape[0] <= 6  # 3.9 units at 1 unit/stud, not 32 studs


def test_zero_horizontal_extent_raises() -> None:
    degenerate = trimesh.Trimesh(
        vertices=[(0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 2.0)],
        faces=[[0, 1, 2]],
        process=False,
    )
    with pytest.raises(ValueError, match="no horizontal extent"):
        grid_from_mesh(degenerate)


def test_up_axis_y_moves_long_axis_into_layers() -> None:
    tall_in_y = trimesh.creation.box(extents=(1.9, 3.9, 0.9))
    grid = grid_from_mesh(tall_in_y, options=MeshOptions(target_studs=4, up="y"))
    nx, ny, nlayers = grid.shape
    assert nlayers > nx
    assert nlayers > ny


def test_uniform_colour_applied() -> None:
    grid = grid_from_mesh(
        _box(),
        options=MeshOptions(target_studs=6, colour_code=4),
    )
    filled = grid.codes[grid.filled_mask]
    assert (filled == 4).all()


def test_bad_colour_raises() -> None:
    with pytest.raises(ValueError, match="unknown LDraw colour code"):
        grid_from_mesh(_box(), options=MeshOptions(colour_code=99_999))


def test_no_fill_leaves_shell() -> None:
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


def test_largest_component_filter() -> None:
    messages: list[str] = []
    grid = grid_from_mesh(
        _two_components(),
        options=MeshOptions(target_studs=12, keep_largest=True),
        progress=messages.append,
    )
    structure = ndimage.generate_binary_structure(rank=3, connectivity=1)
    _labels, components = ndimage.label(grid.filled_mask, structure=structure)
    assert components == 1
    assert any("dropped" in message for message in messages)


def test_disconnected_components_are_preserved_by_default() -> None:
    grid = grid_from_mesh(
        _two_components(),
        options=MeshOptions(target_studs=12),
    )
    structure = ndimage.generate_binary_structure(rank=3, connectivity=1)
    _labels, components = ndimage.label(grid.filled_mask, structure=structure)
    assert components == 2


def test_largest_component_filter_warns_without_progress() -> None:
    with pytest.warns(UserWarning, match="dropped"):
        grid_from_mesh(
            _two_components(),
            options=MeshOptions(target_studs=12, keep_largest=True),
        )


def test_grid_dim_cap_raises() -> None:
    with pytest.raises(ValueError, match="reduce --target-studs"):
        grid_from_mesh(_box(), options=MeshOptions(pitch=0.001))


def test_grid_cell_cap_raises_below_axis_limit() -> None:
    cube = trimesh.creation.box(extents=(100.0, 100.0, 100.0))
    with pytest.raises(ValueError, match=r"cells; cap 16_000_000"):
        grid_from_mesh(cube, options=MeshOptions(pitch=0.5))


def test_determinism() -> None:
    first = grid_from_mesh(_box(), options=MeshOptions(target_studs=8))
    second = grid_from_mesh(_box(), options=MeshOptions(target_studs=8))
    assert np.array_equal(first.codes, second.codes)


def test_mesh_options_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="target_studs must be positive"):
        MeshOptions(target_studs=0)
    with pytest.raises(ValueError, match="pitch must be finite and positive"):
        MeshOptions(pitch=0.0)
    with pytest.raises(ValueError, match="pitch must be finite and positive"):
        MeshOptions(pitch=float("inf"))
    with pytest.raises(ValueError, match="up must be one of"):
        MeshOptions(up=cast("Literal['x', 'y', 'z']", "invalid"))


def test_load_grid_dispatches_mesh_suffix(tmp_path: Path) -> None:
    path = tmp_path / "box.stl"
    _box().export(path)
    grid = load_grid(path, PipelineConfig(mesh=MeshOptions(target_studs=6)))
    assert isinstance(grid, VoxelGrid)
    assert grid.filled_count > 0


def test_mesh_to_grid_reads_obj(tmp_path: Path) -> None:
    path = tmp_path / "box.obj"
    _box().export(path)
    grid = mesh_to_grid(path, options=MeshOptions(target_studs=6))
    assert grid.filled_count > 0


def test_run_file_mesh_end_to_end(tmp_path: Path) -> None:
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


def test_cli_mesh_flags_reject_voxel_input(tmp_path: Path) -> None:
    npy = tmp_path / "box.npy"
    np.save(npy, np.full((3, 3, 2), 4, dtype=np.int16))
    with pytest.raises(SystemExit) as excinfo:
        main([str(npy), "--target-studs", "8"])
    assert excinfo.value.code == 2


def test_cli_voxel_flags_reject_mesh_input(tmp_path: Path) -> None:
    path = tmp_path / "box.stl"
    _box().export(path)
    with pytest.raises(SystemExit) as excinfo:
        main([str(path), "--plates-per-voxel", "2"])
    assert excinfo.value.code == 2


def test_cli_mesh_happy_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "box.stl"
    _box().export(path)
    out = tmp_path / "box.ldr"
    exit_code = main([str(path), "-o", str(out), "--target-studs", "6"])
    assert exit_code == 0
    assert out.exists()
    assert "wrote" in capsys.readouterr().out


def test_cli_largest_component_only_reports_removal(tmp_path: Path) -> None:
    path = tmp_path / "two-components.stl"
    _two_components().export(path)
    out = tmp_path / "two-components.ldr"

    with pytest.warns(UserWarning, match="dropped"):
        exit_code = main(
            [
                str(path),
                "-o",
                str(out),
                "--target-studs",
                "12",
                "--largest-component-only",
            ]
        )

    assert exit_code == 0
    assert out.exists()


# --- colour sampling -------------------------------------------------------


def _split_colour_box() -> trimesh.Trimesh:
    """Box with red vertices on the low-x half and blue on the high-x half."""
    box = trimesh.creation.box(extents=_BOX_EXTENTS)
    colours = np.where(
        box.vertices[:, 0:1] < 0,
        np.array([200, 30, 30, 255], dtype=np.uint8),
        np.array([30, 30, 200, 255], dtype=np.uint8),
    ).astype(np.uint8)
    box.visual = trimesh.visual.ColorVisuals(mesh=box, vertex_colors=colours)
    return box


def test_sampled_vertex_colours_split_box() -> None:
    grid = grid_from_mesh(
        _split_colour_box(),
        options=MeshOptions(target_studs=8, colour_mode="sampled"),
    )
    nx = grid.shape[0]
    low = grid.codes[: nx // 2 - 1][grid.filled_mask[: nx // 2 - 1]]
    high = grid.codes[nx // 2 + 1 :][grid.filled_mask[nx // 2 + 1 :]]
    assert low.size
    assert high.size
    assert len(set(low.tolist())) == 1
    assert len(set(high.tolist())) == 1
    assert set(low.tolist()) != set(high.tolist())


def test_sampled_interior_inherits_surface_colour() -> None:
    grid = grid_from_mesh(
        _split_colour_box(),
        options=MeshOptions(target_studs=8, colour_mode="sampled"),
    )
    assert grid.filled_count == 225  # same coverage as the uniform golden
    assert (grid.codes[grid.filled_mask] != EMPTY).all()


def test_sampled_falls_back_to_uniform_without_colours() -> None:
    messages: list[str] = []
    grid = grid_from_mesh(
        _box(),
        options=MeshOptions(target_studs=6, colour_mode="sampled", colour_code=4),
        progress=messages.append,
    )
    assert (grid.codes[grid.filled_mask] == 4).all()
    assert any("no colour data" in message for message in messages)


def test_sampled_fallback_warns_without_progress() -> None:
    with pytest.warns(UserWarning, match="no colour data"):
        grid_from_mesh(
            _box(),
            options=MeshOptions(target_studs=6, colour_mode="sampled"),
        )


def test_sampled_is_deterministic() -> None:
    options = MeshOptions(target_studs=8, colour_mode="sampled")
    first = grid_from_mesh(_split_colour_box(), options=options)
    second = grid_from_mesh(_split_colour_box(), options=options)
    assert np.array_equal(first.codes, second.codes)


def test_sampled_respects_up_axis() -> None:
    # Red on the +y half in a y-up frame must land in the TOP layers.
    box = trimesh.creation.box(extents=(1.9, 3.9, 0.9))
    colours = np.where(
        box.vertices[:, 1:2] > 0,
        np.array([200, 30, 30, 255], dtype=np.uint8),
        np.array([240, 240, 240, 255], dtype=np.uint8),
    ).astype(np.uint8)
    box.visual = trimesh.visual.ColorVisuals(mesh=box, vertex_colors=colours)
    grid = grid_from_mesh(
        box,
        options=MeshOptions(target_studs=4, up="y", colour_mode="sampled"),
    )
    nlayers = grid.shape[2]
    top = set(
        grid.codes[:, :, nlayers - 2 :][grid.filled_mask[:, :, nlayers - 2 :]].tolist()
    )
    bottom = set(grid.codes[:, :, :2][grid.filled_mask[:, :, :2]].tolist())
    assert top != bottom


def test_sampled_textured_mesh_in_memory() -> None:
    image_module = pytest.importorskip("PIL.Image")
    image = image_module.new("RGB", (2, 1))
    image.putpixel((0, 0), (200, 30, 30))
    image.putpixel((1, 0), (30, 30, 200))
    box = trimesh.creation.box(extents=_BOX_EXTENTS)
    uv = np.where(box.vertices[:, 0:1] < 0, 0.0, 1.0) * np.array([1.0, 0.0])
    uv = np.hstack([uv[:, 0:1], np.full((len(box.vertices), 1), 0.5)])
    box.visual = trimesh.visual.TextureVisuals(
        uv=uv,
        material=trimesh.visual.material.SimpleMaterial(image=image),
    )
    grid = grid_from_mesh(
        box,
        options=MeshOptions(target_studs=8, colour_mode="sampled"),
    )
    codes = set(grid.codes[grid.filled_mask].tolist())
    assert len(codes) == 2


def test_mesh_options_reject_invalid_colour_mode() -> None:
    with pytest.raises(ValueError, match="colour_mode"):
        MeshOptions(colour_mode=cast("Literal['uniform', 'sampled']", "vibes"))


def test_cli_sampled_colour_mode_on_ply(tmp_path: Path) -> None:
    path = tmp_path / "box.ply"
    _split_colour_box().export(path)
    out = tmp_path / "box.ldr"
    exit_code = main(
        [
            str(path),
            "-o",
            str(out),
            "--target-studs",
            "6",
            "--mesh-colour-mode",
            "sampled",
        ]
    )
    assert exit_code == 0
    assert out.exists()


def test_cli_colour_mode_rejects_voxel_input(tmp_path: Path) -> None:
    npy = tmp_path / "box.npy"
    np.save(npy, np.full((3, 3, 2), 4, dtype=np.int16))
    with pytest.raises(SystemExit) as excinfo:
        main([str(npy), "--mesh-colour-mode", "sampled"])
    assert excinfo.value.code == 2
