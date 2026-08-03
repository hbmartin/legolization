"""Geometry-first arbitrary LDraw assembly analysis regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ldraw import analyze_model, load_model
from ldraw.geometry import Matrix
from ldraw.pieces import Piece
from pyldcad import ConnectivityConfig, analyze_connectivity

from legolization.analyze_cli import main as analyze_main
from legolization.assembly import (
    AssemblyAnalysisConfig,
    analyze_assembly,
    write_counterfactual_candidate,
)
from legolization.catalog import load_catalog
from legolization.connectivity import normalize_connectivity
from legolization.layout import Layout
from legolization.ldraw_in import layout_from_ldraw
from legolization.ldraw_out import write_model
from legolization.ldraw_report import prepare_analysis_catalog

_SCOUT = Path(__file__).parent / "data" / "scout.mpd"
_HEART = Path(__file__).parent.parent / "data" / "examples" / "heart.ldr"


def test_scout_tolerant_analysis_resolves_every_occurrence_without_strict_layout() -> (
    None
):
    result = analyze_assembly(
        _SCOUT,
        AssemblyAnalysisConfig(topology_only=True, repair=False),
    )

    assert result.report.status == "complete"
    assert result.report.geometry["occurrence_count"] == 360
    assert result.report.geometry["resolved_count"] == 360
    assert result.report.geometry["skipped_occurrence_ids"] == []
    assert result.report.connectivity["strict_layout_compatible"] is False
    assert result.report.connectivity["confirmed_connection_count"] > 0
    assert result.report.topology_verdict == "disconnected"
    assert result.report.physics_verdict == "not_run"


def test_scout_foundation_rotation_changes_target_contacts_from_zero_to_sixteen() -> (
    None
):
    prepared = prepare_analysis_catalog()
    assert prepared.parts is not None
    loaded = load_model(_SCOUT)
    assert loaded.model is not None
    broken_analysis = loaded.analyze(prepared.parts)
    assert broken_analysis is not None
    broken = analyze_connectivity(
        broken_analysis,
        ConnectivityConfig(infer_surface_contacts=False),
    )

    target_pairs = {(1, 2), (1, 3)}
    assert [
        edge
        for edge in broken.confirmed_connections
        if edge.occurrence_ids in target_pairs
    ] == []

    foundation = next(
        item
        for item in loaded.model.objects
        if isinstance(item, Piece) and item.part.casefold() == "3035"
    )
    foundation.matrix = Matrix([[0, 0, -1], [0, 1, 0], [1, 0, 0]])
    corrected = normalize_connectivity(
        analyze_connectivity(
            analyze_model(loaded.model, parts=prepared.parts),
            ConnectivityConfig(infer_surface_contacts=False),
        )
    )
    contacts = [
        edge
        for edge in corrected.confirmed_connections
        if edge.occurrence_ids in target_pairs
    ]

    assert len([edge for edge in contacts if edge.occurrence_ids == (1, 2)]) == 8
    assert len([edge for edge in contacts if edge.occurrence_ids == (1, 3)]) == 8


def test_scout_counterfactual_finds_and_writes_bom_preserving_ry90(
    tmp_path: Path,
) -> None:
    result = analyze_assembly(
        _SCOUT,
        AssemblyAnalysisConfig(
            topology_only=True,
            repair=True,
            repair_time_budget_s=20,
        ),
    )

    assert result.candidate is not None
    assert result.candidate.occurrence_id == 1
    assert result.candidate.part_id == "3035"
    assert result.candidate.description == "rotate local Y by 90 degrees"
    assert result.candidate.confirmed_connection_gain >= 16
    output = tmp_path / "scout.repaired.mpd"
    assert write_counterfactual_candidate(result, output)
    assert "1 0 0 0 0 0 0 -1 0 1 0 1 0 0 3035.dat" in output.read_text()
    assert output.read_text().count(" 3035.dat") == _SCOUT.read_text().count(
        " 3035.dat"
    )


def test_global_half_stud_phase_is_fitted_before_strict_layout_decode(
    tmp_path: Path,
) -> None:
    layout = Layout(catalog=load_catalog())
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 5, 0, 0, 0, 1)
    source = tmp_path / "shifted.ldr"
    write_model(layout, source)
    shifted_lines = []
    for raw_line in source.read_text().splitlines():
        fields = raw_line.split()
        output_line = raw_line
        if fields[:1] == ["1"]:
            fields[2] = str(float(fields[2]) + 10)
            fields[4] = str(float(fields[4]) + 10)
            output_line = " ".join(fields)
        shifted_lines.append(output_line)
    source.write_text("\n".join(shifted_lines) + "\n")

    imported = layout_from_ldraw(source)

    assert [(item.x, item.y, item.layer) for item in imported] == [(0, 0, 0), (5, 0, 0)]


def test_unknown_load_bearing_capacity_produces_indeterminate_physics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unknown-capacity.ldr"
    source.write_text(
        "0 unknown capacity\n"
        "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3068b.dat\n"
        "1 1 0 -8 0 1 0 0 0 1 0 0 0 1 3068b.dat\n"
    )
    catalog = tmp_path / "connectors.json"
    catalog.write_text(
        json.dumps(
            {
                "schema": 1,
                "parts": [
                    {
                        "part_id": "3068b",
                        "connectors": [
                            {
                                "id": "top",
                                "kind": "test:fixed",
                                "role": "male",
                                "point_ldu": [0, 0, 0],
                                "axis": [0, -1, 0],
                            },
                            {
                                "id": "bottom",
                                "kind": "test:fixed",
                                "role": "female",
                                "point_ldu": [0, 8, 0],
                                "axis": [0, 1, 0],
                            },
                        ],
                    }
                ],
            }
        )
    )

    result = analyze_assembly(
        source,
        AssemblyAnalysisConfig(
            support="selected:1",
            scenarios=("rest",),
            connector_catalog_paths=(catalog,),
            infer_surface_contacts=False,
            repair=False,
        ),
    )

    assert result.report.topology_verdict == "connected"
    assert result.report.physics_verdict == "indeterminate"
    assert result.physics is not None
    assert result.physics.scenarios[0].optimistic_feasible is True
    assert result.physics.scenarios[0].known_capacity_feasible is False


def test_region_path_reports_connector_instance_minimum_cut(tmp_path: Path) -> None:
    source = tmp_path / "stack.ldr"
    source.write_text(
        "0 stack\n"
        "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        "1 1 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
    )

    result = analyze_assembly(
        source,
        AssemblyAnalysisConfig(
            topology_only=True,
            path_between=(("occurrences:1", "occurrences:2"),),
            repair=False,
        ),
    )

    path = result.report.load_paths[0]
    assert path["verdict"] == "connected"
    assert path["confirmed_minimum_cut"] == 8
    assert path["confirmed_path"] == (1, 2)


def test_free_support_reports_topology_without_static_verdict() -> None:
    result = analyze_assembly(
        _HEART,
        AssemblyAnalysisConfig(support="free", repair=False),
    )

    assert result.report.topology_verdict == "connected"
    assert result.report.physics_verdict == "not_run"
    assert result.report.verdict == "connected"


def test_cli_writes_parallel_reports_and_default_data_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "heart.ldr"
    source.write_bytes(_HEART.read_bytes())

    code = analyze_main([str(source), "--no-repair"])

    assert code == 0
    manifest = json.loads(source.with_name("heart.manifest.json").read_text())
    assert manifest["schema"] == "legolization.assembly-manifest"
    assert manifest["version"] == 1
    assert not source.with_suffix(".analysis.json").exists()
    assert not source.with_suffix(".assembly.json").exists()
    assert source.with_suffix(".connections.json").is_file()
    assert source.with_suffix(".components.mpd").is_file()
    assert source.with_suffix(".floating.mpd").is_file()


def test_cli_artifact_directory_adds_html_and_callout(tmp_path: Path) -> None:
    source = tmp_path / "heart.ldr"
    source.write_bytes(_HEART.read_bytes())
    artifacts = tmp_path / "artifacts"

    code = analyze_main([str(source), "--no-repair", "--artifact-dir", str(artifacts)])

    assert code == 0
    assert (artifacts / "heart.analysis.html").is_file()
    assert (artifacts / "heart.callouts" / "missing-connections.svg").is_file()


def test_topology_only_rejects_explicit_support(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        analyze_main(
            [
                str(_HEART),
                "--topology-only",
                "--support",
                "free",
            ]
        )
    assert "cannot specify support" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ("--manifest", "{source}"),
        ("--manifest", "{output}", "--report", "{output}"),
    ],
)
def test_cli_rejects_manifest_path_collisions(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    source = tmp_path / "input.ldr"
    source.write_text("0 input\n", encoding="utf-8")
    output = tmp_path / "shared.json"
    rendered = tuple(value.format(source=source, output=output) for value in arguments)

    with pytest.raises(SystemExit):
        analyze_main([str(source), *rendered])
