"""Geometry-first arbitrary LDraw assembly analysis regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ldraw import analyze_model, load_model
from ldraw.geometry import Matrix
from ldraw.pieces import Piece

from legolization.analyze_cli import main as analyze_main
from legolization.assembly import (
    AssemblyAnalysisConfig,
    analyze_assembly,
    write_counterfactual_candidate,
)
from legolization.assembly_connections import ConnectionConfig, analyze_connections
from legolization.assembly_registry import build_registry
from legolization.catalog import load_catalog
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
    assert broken_analysis.inspection is not None
    registry = build_registry()
    broken = analyze_connections(
        broken_analysis.inspection,
        registry=registry,
        config=ConnectionConfig(infer_surface_contacts=False),
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
    corrected_analysis = analyze_model(loaded.model, parts=prepared.parts)
    assert corrected_analysis.inspection is not None
    corrected = analyze_connections(
        corrected_analysis.inspection,
        registry=registry,
        config=ConnectionConfig(infer_surface_contacts=False),
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


def _stack_source(tmp_path: Path) -> Path:
    source = tmp_path / "stack.ldr"
    source.write_text(
        "0 stack\n"
        "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        "1 1 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
    )
    return source


def test_strict_contacts_produce_one_edge_per_stud(tmp_path: Path) -> None:
    source = _stack_source(tmp_path)
    prepared = prepare_analysis_catalog()
    assert prepared.parts is not None
    analysis = load_model(source).analyze(prepared.parts)
    assert analysis is not None
    assert analysis.inspection is not None

    result = analyze_connections(
        analysis.inspection,
        registry=build_registry(),
        config=ConnectionConfig(infer_surface_contacts=False),
    )

    assert len(result.connections) == 8
    assert {edge.kind for edge in result.connections} == {"stud"}
    assert {edge.status.value for edge in result.connections} == {"confirmed"}
    assert len({edge.first.key for edge in result.connections}) == 8
    assert all(
        edge.capacity.shear_n == pytest.approx(0.98) for edge in result.connections
    )


def test_connection_analysis_to_dict_is_deterministic_json(tmp_path: Path) -> None:
    source = _stack_source(tmp_path)
    prepared = prepare_analysis_catalog()
    assert prepared.parts is not None
    analysis = load_model(source).analyze(prepared.parts)
    assert analysis is not None
    assert analysis.inspection is not None
    registry = build_registry()

    first = analyze_connections(analysis.inspection, registry=registry)
    second = analyze_connections(analysis.inspection, registry=registry)

    first_text = json.dumps(first.to_dict(), sort_keys=True)
    assert first_text == json.dumps(second.to_dict(), sort_keys=True)
    assert json.loads(first_text)["occurrence_count"] == 2


def test_heart_unmatched_endpoints_are_reported_by_key() -> None:
    prepared = prepare_analysis_catalog()
    assert prepared.parts is not None
    analysis = load_model(_HEART).analyze(prepared.parts)
    assert analysis is not None
    assert analysis.inspection is not None

    result = analyze_connections(
        analysis.inspection,
        registry=build_registry(),
        config=ConnectionConfig(infer_surface_contacts=False),
    )

    assert len(result.confirmed_connections) == 46
    assert len(result.endpoints) == 138
    assert len(result.unmatched_endpoints) == 46
    matched_keys = {
        endpoint.key
        for edge in result.connections
        for endpoint in (edge.first, edge.second)
    }
    assert not matched_keys & set(result.unmatched_endpoints)


def test_cli_registers_ldcad_shadow_sources(tmp_path: Path) -> None:
    source = tmp_path / "shadowed.ldr"
    source.write_text(
        "0 shadowed\n"
        "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        "1 1 10 -8 10 1 0 0 0 1 0 0 0 1 3024.dat\n"
    )
    shadow_dir = tmp_path / "shadow" / "parts"
    shadow_dir.mkdir(parents=True)
    (shadow_dir / "3024.dat").write_text(
        "0 Plate 1 x 1 shadow\n"
        "0 !LDCAD SNAP_CYL [gender=F] [caps=one] [secs=R 6 4] [pos=0 4 0] "
        "[ori=1 0 0 0 -1 0 0 0 -1]\n"
    )

    code = analyze_main(
        [
            str(source),
            "--no-repair",
            "--topology-only",
            "--ldcad-metadata",
            str(tmp_path / "shadow"),
        ]
    )

    assert code == 0
    graph = json.loads(source.with_suffix(".connections.json").read_text())
    providers = {
        evidence["provider"]
        for edge in graph["connections"]
        for evidence in edge["evidence"]
    }
    assert "pyldraw3:ldcad_shadow" in providers
    confirmed_pairs = {
        tuple(edge["occurrence_ids"])
        for edge in graph["connections"]
        if edge["status"] == "confirmed"
    }
    assert (1, 2) in confirmed_pairs


def test_cli_registers_studio_metadata_sources(tmp_path: Path) -> None:
    source = tmp_path / "studio.ldr"
    source.write_text(
        "0 studio\n"
        "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        "1 1 10 -8 10 1 0 0 0 1 0 0 0 1 3024.dat\n"
    )
    studio = tmp_path / "studio.json"
    studio.write_text(
        json.dumps(
            {
                "parts": [
                    {
                        "part_id": "3024",
                        "connections": [
                            {
                                "id": "sock",
                                "type": "stud",
                                "position": [0, 4, 0],
                                "axis": [0, 1, 0],
                                "gender": "female",
                            }
                        ],
                    }
                ]
            }
        )
    )

    code = analyze_main(
        [
            str(source),
            "--no-repair",
            "--topology-only",
            "--studio-metadata",
            str(studio),
        ]
    )

    assert code == 0
    graph = json.loads(source.with_suffix(".connections.json").read_text())
    providers = {
        evidence["provider"]
        for edge in graph["connections"]
        for evidence in edge["evidence"]
    }
    assert "pyldraw3:studio" in providers


def test_cli_rejects_unreadable_connection_sources(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "model.ldr"
    source.write_text("0 model\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n")

    missing = analyze_main([str(source), "--ldcad-metadata", str(tmp_path / "missing")])
    assert missing == 1
    assert "error:" in capsys.readouterr().err

    not_zip = tmp_path / "not-a-shadow.txt"
    not_zip.write_text("plain text\n")
    bad_archive = analyze_main([str(source), "--ldcad-metadata", str(not_zip)])
    assert bad_archive == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("male_end", ["top", "bottom"])
def test_physics_axial_capacity_is_orientation_not_order_dependent(
    tmp_path: Path,
    male_end: str,
) -> None:
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
                                "kind": "test:pillar",
                                "role": "male" if male_end == "top" else "female",
                                "point_ldu": [0, 0, 0],
                                "axis": [0, -1, 0],
                                "capacity": {
                                    "pull_n": 0.0,
                                    "compression_n": 100.0,
                                    "shear_n": 100.0,
                                    "torque_nm": 1.0,
                                    "friction_coefficient": 1.0,
                                },
                            },
                            {
                                "id": "bottom",
                                "kind": "test:pillar",
                                "role": "female" if male_end == "top" else "male",
                                "point_ldu": [0, 8, 0],
                                "axis": [0, 1, 0],
                                "capacity": {
                                    "pull_n": 0.0,
                                    "compression_n": 100.0,
                                    "shear_n": 100.0,
                                    "torque_nm": 1.0,
                                    "friction_coefficient": 1.0,
                                },
                            },
                        ],
                    }
                ],
            }
        )
    )
    expectations = {"-8": True, "8": False}
    for offset, expected_feasible in expectations.items():
        source = tmp_path / f"pillar{offset}.ldr"
        source.write_text(
            "0 pillar\n"
            "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3068b.dat\n"
            f"1 1 0 {offset} 0 1 0 0 0 1 0 0 0 1 3068b.dat\n"
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
        assert result.physics is not None
        scenario = result.physics.scenarios[0]
        assert scenario.unknown_capacity_variables == 0, (offset, male_end)
        assert scenario.known_capacity_feasible is expected_feasible, (
            offset,
            male_end,
        )
