"""Existing-model analysis reports, source provenance, and redesigns."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Never

import pytest

from legolization import AnalysisConfig, AnalysisResult, analyze_ldraw
from legolization.catalog import Category, load_catalog
from legolization.layout import Layout
from legolization.ldraw_in import import_ldraw, layout_from_ldraw
from legolization.ldraw_out import write_model
from legolization.main import main
from legolization.redesign import (
    _enclosed_cells,
    _envelope_candidates,
    _exterior_candidates,
    _support_candidates,
    _validate_candidate,
    search_repair,
    write_repair_model,
)
from legolization.stability import SolverConfig, analyze

if TYPE_CHECKING:
    from legolization.graph import ConnectionGraph
    from legolization.stability import StabilityResult

_HEART = Path(__file__).parent.parent / "data" / "examples" / "heart.ldr"
_PARITY = SolverConfig(torque_z=False, ground_pull=True)
_STRICT = SolverConfig(torque_z=True, ground_pull=True)


def test_import_tracks_source_lines_steps_and_ground_offset(tmp_path):
    layout = Layout(catalog=load_catalog())
    layout.add("brick_1x1", 0, 0, 3, 0, 4)
    path = tmp_path / "raised.ldr"
    write_model(layout, path)

    imported = import_ldraw(path, ground=True)

    assert imported.ground_offset_layers == 3
    assert next(iter(imported.layout)).layer == 0
    source = imported.source_refs[0]
    assert source.source_model == "raised.ldr"
    assert source.source_line is not None
    assert source.global_step == 1


def test_auto_ground_accepts_an_origin_below_layer_zero(tmp_path):
    path = tmp_path / "below.ldr"
    path.write_text("0 below\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3005.dat\n")

    imported = import_ldraw(path, ground=True)

    assert imported.ground_offset_layers == -3
    assert next(iter(imported.layout)).layer == 0
    with pytest.raises(ValueError, match="below ground"):
        layout_from_ldraw(path)


def test_analysis_report_contains_both_profiles_and_source_steps():
    result = analyze_ldraw(_HEART, AnalysisConfig(repair=False))

    assert result.report.status == "complete"
    assert result.report.verdict == "feasible"
    assert result.report.solvers["rbe_5dof"]["stable"] is True
    assert result.report.solvers["rbe_6dof"]["stable"] is True
    assert result.report.solvers["maximin_6dof"]["capacity_n"] > 0
    assert result.report.source_steps
    assert result.report.bricks[0]["source"]["line"] is not None
    assert json.loads(result.report.to_json())["schema"] == 1


def test_strict_profile_failure_prevents_a_five_dof_only_pass(monkeypatch):
    analysis_module = import_module("legolization.analysis")
    real_analyze = analysis_module.analyze

    def fail_only_strict(
        layout: Layout,
        config: SolverConfig,
        graph: ConnectionGraph | None = None,
    ) -> StabilityResult:
        result = real_analyze(layout, config, graph)
        return replace(result, stable=False) if config.torque_z else result

    monkeypatch.setattr(analysis_module, "analyze", fail_only_strict)

    result = analyze_ldraw(_HEART, AnalysisConfig(repair=False))

    assert result.report.verdict == "infeasible"
    assert result.report.solvers["rbe_5dof"]["stable"] is True
    assert result.report.solvers["rbe_6dof"]["stable"] is False


def test_solver_failure_is_indeterminate_instead_of_a_false_pass(monkeypatch):
    analysis_module = import_module("legolization.analysis")

    def fail_solver(model: object) -> Never:
        del model
        message = "simulated maximin failure"
        raise RuntimeError(message)

    monkeypatch.setattr(analysis_module, "solve_maximin", fail_solver)

    result = analyze_ldraw(_HEART, AnalysisConfig(repair=False))

    assert result.report.status == "error"
    assert result.report.verdict == "indeterminate"
    assert "simulated maximin failure" in result.report.errors[0]


def test_analysis_import_failure_is_a_structured_report(tmp_path):
    path = tmp_path / "unknown.ldr"
    path.write_text("0 unknown\n1 4 0 -24 0 1 0 0 0 1 0 0 0 1 9999.dat\n")

    result = analyze_ldraw(path, AnalysisConfig(repair=False))

    assert result.report.status == "error"
    assert result.report.verdict == "indeterminate"
    assert "part not in the catalog" in result.report.errors[0]


def test_versioned_catalog_extension_is_merged(tmp_path):
    path = tmp_path / "parts.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "parts": [
                    {
                        "key": "brick_1x5_custom",
                        "ldraw_part": "9998",
                        "category": "brick",
                        "size": [1, 5],
                        "height_plates": 3,
                        "mass_g": 1.9,
                    }
                ],
            }
        )
    )

    catalog = load_catalog(path)

    assert catalog.rect_key(1, 5, 3, category=Category.BRICK) == "brick_1x5_custom"


def test_custom_nonrect_part_requires_explicit_physics_geometry(tmp_path):
    path = tmp_path / "parts.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "parts": [
                    {
                        "key": "corner_custom",
                        "ldraw_part": "9997",
                        "category": "special",
                        "occupied_columns": [[0, 0], [1, 0], [0, 1]],
                        "height_plates": 3,
                        "mass_g": 0.9,
                    }
                ],
            }
        )
    )

    with pytest.raises(ValueError, match="bottom_connectors"):
        load_catalog(path)


def test_catalog_extension_rejects_ambiguous_ldraw_decode(tmp_path):
    path = tmp_path / "parts.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "parts": [
                    {
                        "key": "ambiguous_1x1",
                        "ldraw_part": "3005",
                        "category": "brick",
                        "size": [1, 1],
                        "height_plates": 3,
                        "mass_g": 0.43,
                    }
                ],
            }
        )
    )

    with pytest.raises(ValueError, match="ambiguous LDraw decode"):
        load_catalog(path)


@pytest.mark.parametrize(
    ("part", "message"),
    [
        (
            {
                "key": "duplicate",
                "ldraw_part": "9991",
                "category": "brick",
                "size": [1, 5],
                "height_plates": 3,
                "mass_g": 1.0,
            },
            "duplicate key",
        ),
        (
            {
                "key": "bad_mass",
                "ldraw_part": "9992",
                "category": "brick",
                "size": [1, 5],
                "height_plates": 3,
                "mass_g": 0,
            },
            "mass_g must be finite and positive",
        ),
        (
            {
                "key": "bad_rotation",
                "ldraw_part": "9993",
                "category": "brick",
                "size": [1, 5],
                "height_plates": 3,
                "mass_g": 1.0,
                "orientations": [45],
            },
            "unsupported rotations",
        ),
    ],
)
def test_catalog_extension_rejects_invalid_core_metadata(tmp_path, part, message):
    parts = [part, part] if message == "duplicate key" else [part]
    path = tmp_path / "parts.json"
    path.write_text(json.dumps({"schema": 1, "parts": parts}))

    with pytest.raises(ValueError, match=message):
        load_catalog(path)


def test_catalog_extension_rejects_malformed_connector_geometry(tmp_path):
    path = tmp_path / "parts.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "parts": [
                    {
                        "key": "bad_connector",
                        "ldraw_part": "9994",
                        "category": "special",
                        "height_plates": 1,
                        "mass_g": 1.0,
                        "occupied_cells": [[0, 0, 0]],
                        "filled_cells": [[0, 0, 0]],
                        "top_connectors": [{"cell": [0, 0, 0], "direction": [2, 0, 0]}],
                        "bottom_connectors": [],
                        "orientations": [0],
                        "origin_offset": [0, 0, 0],
                    }
                ],
            }
        )
    )

    with pytest.raises(ValueError, match="malformed connector direction"):
        load_catalog(path)


def test_analyze_cli_uses_derived_report_path(tmp_path, capsys):
    source = tmp_path / "heart.ldr"
    source.write_bytes(_HEART.read_bytes())

    code = main(["analyze", str(source), "--no-repair"])

    assert code == 0
    report = source.with_suffix(".analysis.json")
    assert json.loads(report.read_text())["verdict"] == "feasible"
    assert "analysis: FEASIBLE" in capsys.readouterr().out
    assert source.read_bytes() == _HEART.read_bytes()


def test_analyze_cli_writes_validated_repair_but_returns_infeasible(tmp_path):
    layout = Layout(catalog=load_catalog())
    layout.add("brick_1x1", 0, 0, 3, 0, 4)
    source = tmp_path / "floating.ldr"
    write_model(layout, source)
    original = source.read_bytes()

    code = main(
        [
            "analyze",
            str(source),
            "--preserve-origin",
            "--time-budget",
            "10",
        ]
    )

    report_path = source.with_suffix(".analysis.json")
    repair_path = source.with_suffix(".repaired.ldr")
    report = json.loads(report_path.read_text())
    assert code == 2
    assert report["verdict"] == "infeasible"
    assert report["repair"]["status"] == "found"
    assert report["repair"]["before_metrics"]["feasible"] is False
    assert report["repair"]["after_metrics"]["feasible"] is True
    repaired = layout_from_ldraw(repair_path)
    assert analyze(repaired, _PARITY).stable
    assert analyze(repaired, _STRICT).stable
    assert source.read_bytes() == original


def test_analyze_cli_import_failure_still_writes_report(tmp_path):
    source = tmp_path / "unknown.ldr"
    source.write_text("0 unknown\n1 4 0 -24 0 1 0 0 0 1 0 0 0 1 9999.dat\n")

    code = main(["analyze", str(source), "--no-repair"])

    report = json.loads(source.with_suffix(".analysis.json").read_text())
    assert code == 1
    assert report["verdict"] == "indeterminate"
    assert report["problems"] == [
        {
            "message": "part not in the catalog",
            "occurrence": 1,
            "part_reference": "9999",
            "source_line": 2,
            "source_model": "unknown.ldr",
        }
    ]


def test_empty_model_is_invalid_and_indeterminate(tmp_path):
    source = tmp_path / "empty.ldr"
    source.write_text("0 empty\n")

    result = analyze_ldraw(source, AnalysisConfig(repair=False))

    assert result.report.status == "error"
    assert result.report.verdict == "indeterminate"
    assert result.report.errors == ("model contains no supported pieces",)


def test_long_structural_parts_roundtrip_without_entering_legacy_placement(
    tmp_path,
):
    keys = (
        "plate_1x12",
        "plate_2x10",
        "plate_2x12",
        "plate_2x16",
        "brick_1x10_import",
        "brick_1x12",
        "brick_1x16",
        "brick_2x10_import",
        "plate_1x10_import",
        "tile_1x3",
        "tile_1x6",
        "tile_1x8",
        "tile_2x4",
        "tile_2x6",
    )
    layout = Layout(catalog=load_catalog())
    for index, key in enumerate(keys):
        part = layout.catalog[key]
        assert part.category is Category.SPECIAL
        assert part.replaceable_geometry
        if key.startswith("tile_"):
            assert not part.top_connectors
        layout.add(key, 20 * index, 0, 0, 0, 4)
    path = tmp_path / "long-parts.ldr"
    write_model(layout, path)

    imported = layout_from_ldraw(path)

    assert [brick.part_key for brick in imported] == list(keys)


def _interior_fixture() -> tuple[Layout, int]:
    layout = Layout(catalog=load_catalog())
    layout.add("brick_2x3", 0, 0, 0, 0, 4)
    layout.add("brick_1x3", 0, 2, 0, 0, 4)
    layout.add("brick_1x3", 0, 0, 3, 90, 4)
    layout.add("brick_1x3", 2, 0, 3, 90, 4)
    layout.add("brick_1x1", 1, 0, 3, 0, 4)
    layout.add("brick_1x1", 1, 2, 3, 0, 4)
    top = layout.add("brick_1x1", 1, 1, 6, 0, 4)
    return layout, top.brick_id


def test_interior_support_candidate_fills_only_enclosed_cells(tmp_path):
    original, seed_id = _interior_fixture()
    holes = _enclosed_cells(original)
    candidates = list(_support_candidates(original, (seed_id,), enclosed_only=True))

    assert holes == {(1, 1, 3), (1, 1, 4), (1, 1, 5)}
    assert candidates
    validated = _validate_candidate(
        original,
        candidates[0],
        tier="interior-support",
        parity_solver=_PARITY,
        strict_solver=_STRICT,
    )
    assert validated is not None
    assert validated.visible_added_cells == 0
    path = tmp_path / "interior.ldr"
    write_repair_model(candidates[0], path)
    assert (
        _validate_candidate(
            original,
            layout_from_ldraw(path),
            tier="interior-support",
            parity_solver=_PARITY,
            strict_solver=_STRICT,
        )
        is not None
    )


def test_exterior_support_candidate_is_reimportable(tmp_path):
    original = Layout(catalog=load_catalog())
    floater = original.add("brick_1x1", 0, 0, 3, 0, 4)
    candidate = next(iter(_exterior_candidates(original, (floater.brick_id,))))
    validated = _validate_candidate(
        original,
        candidate,
        tier="exterior-support",
        parity_solver=_PARITY,
        strict_solver=_STRICT,
    )
    assert validated is not None

    path = tmp_path / "repair.ldr"
    write_repair_model(candidate, path)
    reimported = layout_from_ldraw(path)
    assert analyze(reimported, _PARITY).stable
    assert analyze(reimported, _STRICT).stable


def test_envelope_retile_can_repair_bad_bridge(bad_bridge, tmp_path):
    original, _ = bad_bridge
    seeds = tuple(sorted(analyze(original, _STRICT).unstable_ids))
    candidates = _envelope_candidates(
        original,
        seed=0,
        deadline=time.monotonic() + 30.0,
        strict_solver=_STRICT,
    )

    validated_layout = next(
        (
            candidate
            for candidate in candidates
            if _validate_candidate(
                original,
                candidate,
                tier="envelope-retile",
                parity_solver=_PARITY,
                strict_solver=_STRICT,
            )
            is not None
        ),
        None,
    )
    assert validated_layout is not None
    path = tmp_path / "envelope.ldr"
    write_repair_model(validated_layout, path)
    assert (
        _validate_candidate(
            original,
            layout_from_ldraw(path),
            tier="envelope-retile",
            parity_solver=_PARITY,
            strict_solver=_STRICT,
        )
        is not None
    )
    assert seeds


def test_hard_budget_can_terminate_repair_worker():
    original = Layout(catalog=load_catalog())
    floater = original.add("brick_1x1", 0, 0, 9, 0, 4)

    result = search_repair(
        original,
        physics_seed_ids=(floater.brick_id,),
        parity_solver=_PARITY,
        strict_solver=_STRICT,
        time_budget_s=0.001,
        seed=0,
    )

    assert result.timed_out
    assert not result.complete


def test_repair_timeout_marks_analysis_report_partial(tmp_path):
    layout = Layout(catalog=load_catalog())
    layout.add("brick_1x1", 0, 0, 3, 0, 4)
    path = tmp_path / "floating.ldr"
    write_model(layout, path)

    result = analyze_ldraw(
        path,
        AnalysisConfig(
            auto_ground=False,
            repair_time_budget_s=0.0001,
        ),
    )

    assert result.report.status == "partial"
    assert result.report.verdict == "infeasible"
    assert result.report.repair["timed_out"] is True


def test_candidate_validation_rejects_changed_original_geometry():
    original = Layout(catalog=load_catalog())
    original.add("brick_1x1", 0, 0, 0, 0, 4)
    changed = Layout(catalog=original.catalog)
    changed.add("brick_1x1", 0, 0, 0, 0, 1)

    assert (
        _validate_candidate(
            original,
            changed,
            tier="envelope-retile",
            parity_solver=_PARITY,
            strict_solver=_STRICT,
        )
        is None
    )


def test_repair_worker_failure_keeps_infeasible_verdict(tmp_path, monkeypatch):
    layout = Layout(catalog=load_catalog())
    layout.add("brick_1x1", 0, 0, 3, 0, 4)
    source = tmp_path / "floating.ldr"
    write_model(layout, source)

    def boom(*args: object, **kwargs: object) -> Never:
        message = "simulated worker start failure"
        raise RuntimeError(message)

    monkeypatch.setattr("legolization.redesign.search_repair", boom)

    code = main(["analyze", str(source), "--preserve-origin"])

    report = json.loads(source.with_suffix(".analysis.json").read_text())
    assert code == 2
    assert report["status"] == "partial"
    assert report["verdict"] == "infeasible"
    assert report["repair"]["status"] == "error"
    assert "simulated worker start failure" in report["repair"]["error"]


def test_repair_write_failure_keeps_infeasible_exit_code(tmp_path, monkeypatch):
    layout = Layout(catalog=load_catalog())
    layout.add("brick_1x1", 0, 0, 3, 0, 4)
    source = tmp_path / "floating.ldr"
    write_model(layout, source)
    real = analyze_ldraw(source, AnalysisConfig(auto_ground=False, repair=False))
    assert real.imported is not None
    forged = AnalysisResult(
        report=real.report,
        imported=real.imported,
        repaired_layout=real.imported.layout,
    )
    monkeypatch.setattr(
        "legolization.analyze_cli.analyze_ldraw",
        lambda *args, **kwargs: forged,
    )
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied")

    code = main(["analyze", str(source), "-o", str(blocker / "repair.ldr")])

    report = json.loads(source.with_suffix(".analysis.json").read_text())
    assert code == 2
    assert report["status"] == "partial"
    assert report["verdict"] == "infeasible"
    assert any("repair write failed" in error for error in report["errors"])


def test_analyze_cli_stdout_report_keeps_summary_on_stderr(tmp_path, capsys):
    source = tmp_path / "heart.ldr"
    source.write_bytes(_HEART.read_bytes())

    code = main(["analyze", str(source), "--no-repair", "--report", "-"])

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["verdict"] == "feasible"
    assert "analysis: FEASIBLE" in captured.err
    assert not source.with_suffix(".analysis.json").exists()


def test_analyze_cli_rejects_unsafe_artifact_paths(tmp_path):
    source = tmp_path / "heart.ldr"
    source.write_bytes(_HEART.read_bytes())

    with pytest.raises(SystemExit):
        main(["analyze", str(source), "--report", str(tmp_path / "evidence.txt")])
    with pytest.raises(SystemExit):
        main(["analyze", str(source), "-o", str(tmp_path / "repair.mpd")])
    with pytest.raises(SystemExit):
        main(["analyze", str(source), "-o", str(source)])


def test_catalog_extension_cannot_shadow_builtin_key(tmp_path):
    path = tmp_path / "parts.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "parts": [
                    {
                        "key": "brick_1x1",
                        "ldraw_part": "9990",
                        "category": "brick",
                        "size": [1, 1],
                        "height_plates": 3,
                        "mass_g": 0.43,
                    }
                ],
            }
        )
    )

    with pytest.raises(ValueError, match="duplicates keys"):
        load_catalog(path)
