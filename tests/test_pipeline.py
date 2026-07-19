"""Pipeline orchestration and CLI smoke tests."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

import legolization.pipeline as pipeline_module
from legolization.grid import EMPTY, IGNORE, VoxelGrid
from legolization.instructions import InstructionsConfig
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


def test_run_rejects_empty_grid():
    grid = VoxelGrid(codes=np.full((1, 1, 1), EMPTY, dtype=np.int16))

    with pytest.raises(ValueError, match="no filled voxels"):
        run(grid)


def test_run_hollow_reduces_mass():
    codes = np.full((6, 6, 4), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
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
    bad = tmp_path / "box.step"
    bad.touch()
    with pytest.raises(ValueError, match="unsupported input format"):
        run_file(bad, tmp_path / "out.ldr")


def test_run_file_rejects_corrupt_mesh(tmp_path: Path) -> None:
    bad = tmp_path / "box.stl"
    bad.write_bytes(b"not a mesh at all")
    with pytest.raises(ValueError, match=r"failed to load mesh|no triangle faces"):
        run_file(bad, tmp_path / "out.ldr")


def test_run_file_writes_instruction_booklet(tmp_path, monkeypatch):
    # ROADMAP acceptance: booklet step sections match the .ldr STEP structure.
    # Renderer disabled: the booklet must still be written, with placeholders.
    monkeypatch.setenv("LEGOLIZATION_RENDERER", "none")
    npy = tmp_path / "box.npy"
    np.save(npy, _box_codes())
    out = tmp_path / "box.ldr"
    booklet_path = tmp_path / "box.html"
    result = run_file(npy, out, PipelineConfig(seed=0), instructions_path=booklet_path)
    markup = booklet_path.read_text()
    assert markup.count('<section class="step"') == result.step_count
    assert out.read_text().count("0 STEP") == result.step_count
    assert "no LDraw renderer" in markup


def test_run_file_booklet_requires_a_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGOLIZATION_RENDERER", "none")
    npy = tmp_path / "box.npy"
    np.save(npy, _box_codes())
    config = PipelineConfig(seed=0, instructions=InstructionsConfig(mode="layer"))
    output_path = tmp_path / "box.ldr"
    with pytest.raises(ValueError, match="needs smart steps"):
        run_file(
            npy,
            output_path,
            config,
            instructions_path=tmp_path / "box.html",
        )
    assert not output_path.exists()


def test_run_file_validates_booklet_suffix_before_writing(
    tmp_path: Path,
) -> None:
    npy = tmp_path / "box.npy"
    np.save(npy, _box_codes())
    output_path = tmp_path / "box.ldr"
    bom_path = tmp_path / "box.json"

    with pytest.raises(ValueError, match="unsupported booklet format"):
        run_file(
            npy,
            output_path,
            PipelineConfig(seed=0),
            bom_path=bom_path,
            instructions_path=tmp_path / "box.docx",
        )

    assert not output_path.exists()
    assert not bom_path.exists()


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


def test_cli_instructions_pdf_end_to_end(tmp_path, capsys, monkeypatch):
    import math

    import pypdf

    monkeypatch.setenv("LEGOLIZATION_RENDERER", "none")
    npy = tmp_path / "box.npy"
    np.save(npy, _box_codes())
    out = tmp_path / "box.ldr"
    pdf = tmp_path / "box.pdf"
    code = main([str(npy), "-o", str(out), "--instructions", str(pdf)])
    captured = capsys.readouterr()
    assert code == 0
    assert str(pdf) in captured.out
    steps = out.read_text().count("0 STEP")
    # Small box: BOM fits the cover, so pages = cover + 2-step pages.
    assert len(pypdf.PdfReader(pdf).pages) == 1 + math.ceil(steps / 2)


def test_cli_instructions_requires_smart_steps(tmp_path):
    npy = tmp_path / "box.npy"
    np.save(npy, _box_codes())
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                str(npy),
                "--steps",
                "layer",
                "--instructions",
                str(tmp_path / "box.html"),
            ]
        )
    assert excinfo.value.code == 2


def test_cli_instructions_rejects_unknown_suffix(tmp_path):
    npy = tmp_path / "box.npy"
    np.save(npy, _box_codes())
    with pytest.raises(SystemExit) as excinfo:
        main([str(npy), "--instructions", str(tmp_path / "box.docx")])
    assert excinfo.value.code == 2


def test_hollow_restore_loop_fires_on_instability(monkeypatch):
    # The restore loop's whole reason to exist — hollowing breaks physics,
    # restoring interior fill repairs it — never triggers on shapes that
    # are stable when hollowed, so fake one unstable verdict: the loop must
    # restore IGNORE fill and re-place.
    real_analyze = pipeline_module.analyze
    calls = {"count": 0}

    def fake_analyze(layout, config=None, graph=None) -> object:
        result = real_analyze(layout, config, graph)
        calls["count"] += 1
        if calls["count"] == 1:
            victim = next(iter(layout.bricks))
            scores = dict(result.scores)
            scores[victim] = replace(scores[victim], score=1.0)
            return replace(result, stable=False, scores=scores)
        return result

    monkeypatch.setattr(pipeline_module, "analyze", fake_analyze)
    codes = np.full((6, 6, 4), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    # repair=False isolates the restore loop (repair would consume the
    # faked unstable verdict by rearranging instead of adding material).
    result = run(grid, PipelineConfig(seed=0, repair=False))

    assert calls["count"] > 1  # the loop re-placed after restoring
    assert result.stability.stable
    assert result.grid is not None
    assert (result.grid.codes == IGNORE).any()  # restored fill is IGNORE
    baseline = run(grid, PipelineConfig(seed=0, hollow_rounds=0, repair=False))
    assert baseline.grid is not None
    assert result.grid.filled_count > baseline.grid.filled_count


def test_luo_no_refine_skips_refinement(monkeypatch):
    import legolization.placement.luo as luo_module

    def boom(*args, **kwargs) -> None:
        msg = "refinement must not run with refine=False"
        raise AssertionError(msg)

    monkeypatch.setattr(luo_module, "improve_connectivity", boom)
    monkeypatch.setattr(luo_module.LuoStrategy, "_stabilize", boom)
    codes = np.full((3, 3, 2), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    result = run(grid, PipelineConfig(strategy="luo", refine=False, seed=0))
    assert result.brick_count > 0


def test_no_ignore_colours_reach_output():
    # Interior cells become the IGNORE label during placement; export-time
    # resolution must leave only real LDraw colour codes.
    codes = np.full((5, 5, 3), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    result = run(grid, PipelineConfig(hollow=False, seed=0))
    assert all(brick.colour_code >= 0 for brick in result.layout)
    assert result.buildable


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


def test_dataclass_positional_layouts_are_stable():
    # New defaulted fields must append after the 0.2.0 layout so old
    # positional callers keep their meaning (PR #17 review).
    from dataclasses import fields

    from legolization.compare import Candidate
    from legolization.mesh import MeshOptions

    def names(cls: type) -> list[str]:
        return [f.name for f in fields(cls)]

    assert names(Candidate)[:5] == ["strategy", "seconds", "result", "metrics", "error"]
    assert names(Candidate)[-1] == "seed"
    assert names(MeshOptions) == [
        "target_studs",
        "pitch",
        "up",
        "colour_code",
        "fill",
        "keep_largest",
        "colour_mode",
    ]
    config_names = names(PipelineConfig)
    assert config_names[-3:] == ["snot", "milp_layer_time_s", "milp_bond_weight"]
    assert config_names.index("tiles") + 1 == config_names.index("refine")
    result_names = names(pipeline_module.PipelineResult)
    assert result_names[-2:] == ["plan", "snot_added"]
