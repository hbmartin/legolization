"""Input inspection: reports, conditions, recommendations, normalization."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import trimesh
import trimesh.visual

from legolization.errors import ConfigurationError
from legolization.grid import VoxelGrid
from legolization.inspection import (
    classify_up_axis,
    inspect_input,
    recommend_plates_per_voxel,
    resolve_prepared_input,
    write_normalized,
)

EXAMPLES = Path(__file__).parent.parent / "data" / "examples"


def _rotated_cone(up: str) -> trimesh.Trimesh:
    cone = trimesh.creation.cone(radius=6.0, height=10.0, sections=48)
    match up:
        case "y":
            matrix = trimesh.transformations.rotation_matrix(
                -np.pi / 2,
                (1.0, 0.0, 0.0),
            )
            cone.apply_transform(matrix)
        case "x":
            matrix = trimesh.transformations.rotation_matrix(
                np.pi / 2,
                (0.0, 1.0, 0.0),
            )
            cone.apply_transform(matrix)
        case _:
            pass
    return cone


def test_heart_vox_report_is_stable():
    inspection = inspect_input(EXAMPLES / "heart.vox")
    assert inspection.kind == "voxel"
    assert inspection.format == "vox"
    assert inspection.voxel is not None
    assert inspection.voxel.filled_count > 0
    assert inspection.voxel.recommended_plates_per_voxel == 3
    assert sum(inspection.voxel.colour_codes.values()) == pytest.approx(1.0)
    assert inspection.to_dict() == inspect_input(EXAMPLES / "heart.vox").to_dict()


@pytest.mark.parametrize("name", ["pyramid.npy", "arch.npy"])
def test_npy_reports(name):
    inspection = inspect_input(EXAMPLES / name)
    assert inspection.kind == "voxel"
    assert inspection.voxel is not None
    assert inspection.voxel.filled_count > 0
    assert inspection.conditions == ()


def test_recommend_plates_per_voxel_bounds():
    assert recommend_plates_per_voxel((10, 10, 100)) == 3
    assert recommend_plates_per_voxel((10, 10, 150)) == 2
    assert recommend_plates_per_voxel((10, 10, 300)) == 1


def test_cone_up_axis_is_high_confidence():
    for up in ("x", "y", "z"):
        report = classify_up_axis(_rotated_cone(up))
        assert report.recommended == up
        assert report.confidence == "high"


def test_sphere_up_axis_is_ambiguous():
    sphere = trimesh.creation.icosphere(subdivisions=2, radius=5.0)
    report = classify_up_axis(sphere)
    assert report.confidence == "ambiguous"


def test_mesh_inspection_reports_recommendation(tmp_path):
    cone = _rotated_cone("y")
    path = tmp_path / "cone.stl"
    cone.export(path)
    inspection = inspect_input(path, auto_scale=(8, 16))
    assert inspection.kind == "mesh"
    assert inspection.mesh is not None
    assert inspection.mesh.up.recommended == "y"
    assert inspection.mesh.up.confidence == "high"
    assert "ambiguous-up-axis" not in inspection.conditions
    assert 8 <= inspection.mesh.recommendation.target_studs <= 16
    assert "no-colour-data" in inspection.conditions
    assert inspection.mesh.colour_summary == {}


def test_colourless_sphere_flags_ambiguity_and_colour(tmp_path):
    sphere = trimesh.creation.icosphere(subdivisions=2, radius=5.0)
    path = tmp_path / "sphere.stl"
    sphere.export(path)
    inspection = inspect_input(path, auto_scale=(8, 12))
    assert "ambiguous-up-axis" in inspection.conditions
    assert "no-colour-data" in inspection.conditions


def test_vertex_coloured_mesh_summarizes_colours(tmp_path):
    box = trimesh.creation.box(extents=(6.0, 6.0, 6.0))
    colours = np.tile(
        np.asarray([200, 30, 30, 255], dtype=np.uint8),
        (len(box.vertices), 1),
    )
    box.visual = trimesh.visual.ColorVisuals(box, vertex_colors=colours)
    path = tmp_path / "red-box.ply"
    box.export(path)
    inspection = inspect_input(path, auto_scale=(6, 10))
    assert inspection.mesh is not None
    assert inspection.mesh.has_colour_data is True
    assert "no-colour-data" not in inspection.conditions
    assert inspection.mesh.colour_summary
    assert sum(inspection.mesh.colour_summary.values()) == pytest.approx(1.0)


def test_multi_component_mesh_is_preserved_and_reported(tmp_path):
    left = trimesh.creation.box(extents=(4.0, 4.0, 4.0))
    right = trimesh.creation.box(extents=(4.0, 4.0, 4.0))
    right.apply_translation((10.0, 0.0, 0.0))
    combined = trimesh.util.concatenate([left, right])
    path = tmp_path / "pair.stl"
    combined.export(path)
    inspection = inspect_input(path, auto_scale=(8, 14))
    assert inspection.mesh is not None
    assert inspection.mesh.component_count == 2
    assert "multiple-components" in inspection.conditions
    assert any("preserved" in warning for warning in inspection.warnings)


def test_ldraw_input_is_rejected_with_guidance():
    with pytest.raises(ConfigurationError, match="bundle"):
        inspect_input(EXAMPLES / "heart.ldr")


def test_unsupported_suffix_gets_conversion_guidance(tmp_path):
    weird = tmp_path / "model.docx"
    weird.write_text("not a model")
    with pytest.raises(ConfigurationError, match="not a general"):
        inspect_input(weird)


def test_write_normalized_round_trips_voxels(tmp_path):
    source = tmp_path / "box.npy"
    codes = np.full((4, 3, 2), -1, dtype=np.int16)
    codes[0:3, :, :] = 4
    np.save(source, codes)
    inspection, output = write_normalized(source)
    assert output.directory == tmp_path / "box-prepared"
    reloaded = VoxelGrid.from_npy(output.npy_path, plates_per_voxel=1)
    original = VoxelGrid.from_npy(source, plates_per_voxel=3)
    assert reloaded.filled_count == original.filled_count
    sidecar = json.loads(output.sidecar_path.read_text())
    assert sidecar["schema"] == "legolization.input-normalized/v1"
    assert sidecar["source"]["filename"] == "box.npy"
    assert sidecar["source"]["sha256"] == inspection.sha256
    assert sidecar["scale"]["plates_per_voxel"] == 3
    record = json.loads((output.directory / "bundle.json").read_text())
    assert record["schema"] == "legolization.bundle/v1"
    assert {entry["path"] for entry in record["artifacts"]} == {
        "normalized.npy",
        "normalized.json",
    }


def test_resolve_prepared_input_round_trips_through_load_grid(tmp_path):
    from legolization.pipeline import load_grid

    source = tmp_path / "box.npy"
    codes = np.full((4, 3, 2), -1, dtype=np.int16)
    codes[0:3, :, :] = 4
    np.save(source, codes)
    _, output = write_normalized(source)
    assert resolve_prepared_input(source) is None
    prepared = resolve_prepared_input(output.directory)
    assert prepared is not None
    assert prepared.npy_path == output.npy_path
    grid = load_grid(output.directory)
    expected = VoxelGrid.from_npy(output.npy_path, plates_per_voxel=1)
    assert grid.codes.shape == expected.codes.shape
    assert grid.filled_count == expected.filled_count


def test_resolve_prepared_input_rejects_a_plain_directory(tmp_path):
    with pytest.raises(ConfigurationError, match="not a prepared input bundle"):
        resolve_prepared_input(tmp_path)


def test_resolve_prepared_input_rejects_a_stale_npy(tmp_path):
    source = tmp_path / "box.npy"
    codes = np.full((3, 3, 2), -1, dtype=np.int16)
    codes[0:2, :, :] = 4
    np.save(source, codes)
    _, output = write_normalized(source)
    np.save(output.npy_path, codes)
    with pytest.raises(ConfigurationError, match="stale"):
        resolve_prepared_input(output.directory)


def test_resolve_prepared_input_rejects_a_foreign_sidecar(tmp_path):
    source = tmp_path / "box.npy"
    codes = np.full((3, 3, 2), -1, dtype=np.int16)
    codes[0:2, :, :] = 4
    np.save(source, codes)
    _, output = write_normalized(source)
    output.sidecar_path.write_text(json.dumps({"schema": "something/else"}))
    with pytest.raises(ConfigurationError, match="invalid"):
        resolve_prepared_input(output.directory)


def test_write_normalized_numbers_collisions(tmp_path):
    source = tmp_path / "box.npy"
    codes = np.full((3, 3, 2), -1, dtype=np.int16)
    codes[0:2, :, :] = 4
    np.save(source, codes)
    (tmp_path / "box-prepared").mkdir()
    _, output = write_normalized(source)
    assert output.directory == tmp_path / "box-prepared-2"


def test_input_inspect_cli_emits_single_envelope(capsys):
    from legolization.cli import main

    code = main(["input", "inspect", str(EXAMPLES / "pyramid.npy"), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "input inspect"
    assert payload["status"] == "complete"
    assert payload["data"]["kind"] == "voxel"
    assert payload["data"]["voxel"]["filled_count"] > 0


def test_input_inspect_cli_write_reports_artifacts(tmp_path, capsys):
    source = tmp_path / "box.npy"
    codes = np.full((3, 3, 2), -1, dtype=np.int16)
    codes[0:2, :, :] = 4
    np.save(source, codes)
    from legolization.cli import main

    code = main(["input", "inspect", str(source), "--write", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    kinds = {artifact["kind"] for artifact in payload["artifacts"]}
    assert kinds == {"normalized", "sidecar", "bundle-record"}
