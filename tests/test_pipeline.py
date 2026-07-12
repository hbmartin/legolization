"""Pipeline orchestration and CLI smoke tests."""

import numpy as np
import pytest

from legolization.grid import EMPTY, VoxelGrid
from legolization.main import main
from legolization.pipeline import PipelineConfig, run, run_file


def _box_codes(n: int = 4, height: int = 2) -> np.ndarray:
    codes = np.full((n, n, height), EMPTY, dtype=np.int16)
    codes[:, :, :] = 4
    return codes


def test_run_solid_box_is_buildable():
    grid = VoxelGrid.from_array(_box_codes(), plates_per_voxel=3)
    result = run(grid, PipelineConfig(hollow=False, seed=0))
    assert result.buildable
    assert result.brick_count > 0
    assert result.stability.stable


def test_run_hollow_reduces_mass():
    codes = np.full((5, 5, 5), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=1)
    solid = run(grid, PipelineConfig(hollow=False, seed=0))
    hollow = run(grid, PipelineConfig(hollow=True, seed=0))
    assert hollow.mass_g < solid.mass_g
    assert hollow.buildable


def test_run_file_npy_to_ldr(tmp_path):
    npy = tmp_path / "box.npy"
    np.save(npy, _box_codes())
    out = tmp_path / "box.ldr"
    result = run_file(npy, out, PipelineConfig(seed=0))
    assert out.exists()
    assert result.buildable
    content = out.read_text()
    assert "0 STEP" in content
    for line in content.splitlines():
        assert line.startswith(("0", "1"))


def test_run_file_rejects_unknown_suffix(tmp_path):
    bad = tmp_path / "box.stl"
    bad.touch()
    with pytest.raises(ValueError, match="unsupported input format"):
        run_file(bad, tmp_path / "out.ldr")


def test_cli_end_to_end(tmp_path, capsys):
    npy = tmp_path / "box.npy"
    np.save(npy, _box_codes())
    out = tmp_path / "box.ldr"
    code = main([str(npy), "-o", str(out), "--seed", "1"])
    captured = capsys.readouterr()
    assert code == 0
    assert out.exists()
    assert "STABLE" in captured.out


def test_cli_reports_missing_file(tmp_path, capsys):
    code = main([str(tmp_path / "nope.npy"), "-o", str(tmp_path / "o.ldr")])
    assert code == 1
    assert "error" in capsys.readouterr().err


def test_pipeline_is_deterministic_for_seed():
    codes = np.full((5, 5, 3), EMPTY, dtype=np.int16)
    for z in range(3):
        codes[z : 5 - z, z : 5 - z, z] = 4
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    config = PipelineConfig(seed=3)

    def snapshot() -> list[tuple[str, int, int, int, int, int]]:
        result = run(grid, config)
        return sorted(
            (b.part_key, b.x, b.y, b.layer, b.yaw, b.colour_code) for b in result.layout
        )

    assert snapshot() == snapshot()


def test_disjoint_islands_are_not_buildable(tmp_path, capsys):
    # Two voxel islands with an air gap: each stands, but no single model
    # connects them — brick-graph semantics report 2 components, exit 2.
    codes = np.full((3, 1, 1), EMPTY, dtype=np.int16)
    codes[0, 0, 0] = 4
    codes[2, 0, 0] = 4
    npy = tmp_path / "islands.npy"
    np.save(npy, codes)
    code = main([str(npy), "-o", str(tmp_path / "islands.ldr"), "--solid"])
    captured = capsys.readouterr()
    assert code == 2
    assert "2 components" in captured.err


def test_tiles_and_slopes_flags(tmp_path):
    codes = np.full((3, 3, 2), EMPTY, dtype=np.int16)
    codes[:, :, 0] = 4
    codes[1, 1, 1] = 14  # a bump that creates steps on every side
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    result = run(grid, PipelineConfig(hollow=False, slopes=True, tiles=True, seed=2))
    assert result.stability.stable
    assert result.slopes_added >= 0
    assert result.tiles_added >= 0
