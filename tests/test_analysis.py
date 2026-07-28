"""Existing-model analysis reports, source provenance, and redesigns."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never, cast

import pytest

from legolization import AnalysisConfig, AnalysisResult, analyze_ldraw
from legolization.catalog import Category, load_catalog
from legolization.layout import Layout
from legolization.ldraw_in import import_ldraw, layout_from_ldraw
from legolization.ldraw_out import write_model
from legolization.main import main
from legolization.redesign import (
    _BridgeStud,
    _eligible_bridge_pairs,
    _enclosed_cells,
    _envelope_candidates,
    _exterior_candidates,
    _search_body,
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


def test_import_tracks_source_lines_steps_and_ground_offset(tmp_path: Path):
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


def test_auto_ground_accepts_an_origin_below_layer_zero(tmp_path: Path):
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


def test_incremental_prefix_topology_matches_full_graph_rebuilds():
    analysis_module = import_module("legolization.analysis")
    graph_module = import_module("legolization.graph")
    layout = Layout(catalog=load_catalog())
    floater = layout.add("brick_1x1", 0, 0, 3, 0, 4)
    support = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    grounded = layout.add("brick_1x1", 4, 0, 0, 0, 4)
    topology = analysis_module._PrefixTopology.from_layout(layout)  # noqa: SLF001
    present: set[int] = set()

    for chunk in (
        (floater.brick_id,),
        (grounded.brick_id,),
        (support.brick_id,),
    ):
        present.update(chunk)
        topology.extend(chunk)
        graph = graph_module.ConnectionGraph.from_layout(layout.subset(present))
        assert topology.component_count == graph.component_count()
        assert topology.floating_count == len(graph.floating_ids())


def test_pathological_source_step_count_is_skipped_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    layout = Layout(catalog=load_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", 0, 0, 3, 0, 4)
    path = tmp_path / "two-steps.ldr"
    write_model(layout, path)
    analysis_module = import_module("legolization.analysis")
    monkeypatch.setattr(analysis_module, "_MAX_SOURCE_STEPS", 1)

    result = analyze_ldraw(path, AnalysisConfig(repair=False))

    assert result.report.source_steps == ()
    assert result.report.warnings == (
        "source-step analysis skipped: 2 steps exceeds the 1-step safety limit",
    )


def test_strict_profile_failure_prevents_a_five_dof_only_pass(
    monkeypatch: pytest.MonkeyPatch,
):
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


def test_solver_failure_is_indeterminate_instead_of_a_false_pass(
    monkeypatch: pytest.MonkeyPatch,
):
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


def test_analysis_import_failure_is_a_structured_report(tmp_path: Path):
    path = tmp_path / "unknown.ldr"
    path.write_text("0 unknown\n1 4 0 -24 0 1 0 0 0 1 0 0 0 1 9999.dat\n")

    result = analyze_ldraw(path, AnalysisConfig(repair=False))

    assert result.report.status == "error"
    assert result.report.verdict == "indeterminate"
    assert "part not in the catalog" in result.report.errors[0]


def test_catalog_loading_failure_has_catalog_context(tmp_path: Path):
    catalog_path = tmp_path / "bad-catalog.json"
    catalog_path.write_text(json.dumps({"parts": []}))

    result = analyze_ldraw(
        _HEART,
        AnalysisConfig(catalog_paths=(catalog_path,), repair=False),
    )

    assert result.report.status == "error"
    assert result.report.repair["reason"] == "catalog loading failed"
    assert result.report.errors[0].startswith("catalog loading failed:")
    assert str(catalog_path) in result.report.catalog["extensions"]


def test_analysis_does_not_hide_internal_import_type_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    analysis_module = import_module("legolization.analysis")

    def fail_import(*args: object, **kwargs: object) -> Never:
        message = "simulated internal signature defect"
        raise TypeError(message)

    monkeypatch.setattr(analysis_module, "import_ldraw", fail_import)

    with pytest.raises(TypeError, match="simulated internal signature defect"):
        analyze_ldraw(_HEART, AnalysisConfig(repair=False))


def test_versioned_catalog_extension_is_merged(tmp_path: Path):
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


def test_custom_nonrect_part_requires_explicit_physics_geometry(tmp_path: Path):
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


def test_catalog_extension_rejects_ambiguous_ldraw_decode(tmp_path: Path):
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


def test_catalog_rejects_overlapping_decode_tolerance_windows(tmp_path: Path):
    path = tmp_path / "parts.json"
    cells = [[0, 0, layer] for layer in range(3)]
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "parts": [
                    {
                        "key": "near_ambiguous_1x1",
                        "ldraw_part": "3005",
                        "category": "special",
                        "height_plates": 3,
                        "mass_g": 0.43,
                        "occupied_cells": cells,
                        "filled_cells": cells,
                        "top_connectors": [],
                        "bottom_connectors": [],
                        "orientations": [0],
                        "origin_offset": [0.3, 0, 0],
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
        (
            {
                "key": "bad_replaceable_geometry",
                "ldraw_part": "9994",
                "category": "brick",
                "size": [1, 5],
                "height_plates": 3,
                "mass_g": 1.0,
                "replaceable_geometry": "yes",
            },
            "replaceable_geometry must be boolean",
        ),
    ],
)
def test_catalog_extension_rejects_invalid_core_metadata(
    tmp_path: Path,
    part,
    message,
):
    parts = [part, part] if message == "duplicate key" else [part]
    path = tmp_path / "parts.json"
    path.write_text(json.dumps({"schema": 1, "parts": parts}))

    with pytest.raises(ValueError, match=message):
        load_catalog(path)


@pytest.mark.parametrize(
    ("document", "error", "message"),
    [
        ({"parts": []}, ValueError, "must declare schema"),
        ({"schema": 2, "parts": []}, ValueError, "must declare schema"),
        ({"schema": 1, "parts": {}}, TypeError, "must contain a parts list"),
    ],
)
def test_catalog_rejects_malformed_documents(
    tmp_path: Path,
    document: dict[str, object],
    error: type[Exception],
    message: str,
):
    path = tmp_path / "parts.json"
    path.write_text(json.dumps(document))

    with pytest.raises(error, match=message):
        load_catalog(path)


def test_catalog_extension_rejects_malformed_connector_geometry(tmp_path: Path):
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


def test_analyze_cli_uses_derived_report_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "heart.ldr"
    source.write_bytes(_HEART.read_bytes())

    code = main(["analyze", str(source), "--no-repair"])

    assert code == 0
    report = source.with_suffix(".analysis.json")
    assert json.loads(report.read_text())["verdict"] == "feasible"
    assert "analysis: FEASIBLE" in capsys.readouterr().out
    assert source.read_bytes() == _HEART.read_bytes()


def test_analyze_cli_writes_validated_repair_but_returns_infeasible(tmp_path: Path):
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
            "60",
        ]
    )

    report_path = source.with_suffix(".analysis.json")
    repair_path = source.with_suffix(".repaired.ldr")
    report = json.loads(report_path.read_text())
    assert code == 2
    assert report["verdict"] == "infeasible"
    assert report["repair"]["timed_out"] is False
    assert report["repair"]["status"] == "found"
    assert report["repair"]["before_metrics"]["feasible"] is False
    assert report["repair"]["after_metrics"]["feasible"] is True
    repaired = layout_from_ldraw(repair_path)
    assert analyze(repaired, _PARITY).stable
    assert analyze(repaired, _STRICT).stable
    assert source.read_bytes() == original


def test_analyze_cli_import_failure_still_writes_report(tmp_path: Path):
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


def test_empty_model_is_invalid_and_indeterminate(tmp_path: Path):
    source = tmp_path / "empty.ldr"
    source.write_text("0 empty\n")

    result = analyze_ldraw(source, AnalysisConfig(repair=False))

    assert result.report.status == "error"
    assert result.report.verdict == "indeterminate"
    assert result.report.errors == ("model contains no supported pieces",)


def test_long_structural_parts_roundtrip_without_entering_legacy_placement(
    tmp_path: Path,
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


@pytest.mark.parametrize(
    "key",
    [
        "brick_corner_2x2",
        "plate_corner_2x2",
        "plate_quarter_cutout_4x4",
        "plate_1x8_half_offset",
        "brick_1x2_legacy",
        "plate_1x2_legacy",
        "brick_1x1_headlight",
        "dish_3x3_inverted_snot",
    ],
)
@pytest.mark.parametrize("yaw", [0, 90, 180, 270])
def test_offset_and_outward_special_parts_roundtrip(
    tmp_path: Path,
    key: str,
    yaw: int,
):
    layout = Layout(catalog=load_catalog())
    layout.add(
        part_key=key,
        x=0,
        y=0,
        layer=0,
        yaw=yaw,
        colour_code=4,
    )
    path = tmp_path / f"{key}-{yaw}.ldr"
    write_model(layout, path)

    imported = layout_from_ldraw(path)

    assert [
        (brick.part_key, brick.x, brick.y, brick.layer, brick.yaw) for brick in imported
    ] == [(key, 0, 0, 0, yaw)]


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


def test_interior_support_candidate_fills_only_enclosed_cells(tmp_path: Path):
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


def test_exterior_support_candidate_is_reimportable(tmp_path: Path):
    original = Layout(catalog=load_catalog())
    floater = original.add("brick_1x1", 0, 0, 3, 0, 4)
    candidate = next(
        iter(
            _exterior_candidates(
                original,
                (floater.brick_id,),
                deadline=time.monotonic() + 30.0,
            )
        )
    )
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


def test_exterior_bridge_discovery_stops_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    original = Layout(catalog=load_catalog())
    for x in range(0, 200, 2):
        original.add("brick_1x1", x, 0, 0, 0, 4)
    clock_checks = 0

    def advancing_clock() -> float:
        nonlocal clock_checks
        clock_checks += 1
        return 0.0 if clock_checks < 110 else 2.0

    class FakeTime:
        monotonic = staticmethod(advancing_clock)

    redesign_module = import_module("legolization.redesign")
    monkeypatch.setattr(redesign_module, "time", FakeTime)

    candidates = list(_exterior_candidates(original, (), deadline=1.0))

    assert not candidates
    assert 110 <= clock_checks < len(original.bricks) ** 2


def test_exterior_bridge_discovery_buckets_only_aligned_studs(
    monkeypatch: pytest.MonkeyPatch,
):
    studs = [
        _BridgeStud(brick_id=index, component=index, x=index, y=index, z=0)
        for index in range(1_000)
    ]
    clock_checks = 0

    def counting_clock() -> float:
        nonlocal clock_checks
        clock_checks += 1
        return 0.0

    class FakeTime:
        monotonic = staticmethod(counting_clock)

    redesign_module = import_module("legolization.redesign")
    monkeypatch.setattr(redesign_module, "time", FakeTime)

    assert list(_eligible_bridge_pairs(studs, deadline=1.0)) == []
    assert clock_checks == len(studs)


def test_exterior_bridge_discovery_keeps_aligned_cross_component_pairs():
    first = _BridgeStud(brick_id=0, component=0, x=0, y=0, z=2)
    shared_x = _BridgeStud(brick_id=1, component=1, x=0, y=3, z=2)
    shared_y = _BridgeStud(brick_id=2, component=2, x=4, y=0, z=2)

    pairs = list(
        _eligible_bridge_pairs(
            [first, shared_x, shared_y],
            deadline=time.monotonic() + 1.0,
        )
    )

    assert pairs == [(3, first, shared_x), (4, first, shared_y)]


def test_expired_envelope_round_does_not_start_solver(
    monkeypatch: pytest.MonkeyPatch,
):
    original = Layout(catalog=load_catalog())
    original.add("brick_1x1", 0, 0, 0, 0, 4)

    def unexpected_analyze(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("expired retile round started the stability solver")

    monkeypatch.setattr("legolization.redesign.analyze", unexpected_analyze)

    assert (
        list(
            _envelope_candidates(
                original,
                seed=0,
                deadline=0.0,
                strict_solver=_STRICT,
            )
        )
        == []
    )


def test_search_body_emits_terminal_timeout_message():
    original = Layout(catalog=load_catalog())
    floater = original.add("brick_1x1", 0, 0, 3, 0, 4)

    class Updates:
        def __init__(self) -> None:
            self.messages: list[tuple[str, object]] = []

        def put(self, message: tuple[str, object]) -> None:
            self.messages.append(message)

    updates = Updates()
    _search_body(
        original,
        physics_seed_ids=(floater.brick_id,),
        parity_solver=_PARITY,
        strict_solver=_STRICT,
        deadline=0.0,
        seed=0,
        updates=cast("Any", updates),
    )

    assert updates.messages == [("timeout", 0)]


def test_envelope_retile_can_repair_bad_bridge(bad_bridge, tmp_path: Path):
    original, _ = bad_bridge
    seeds = tuple(sorted(analyze(original, _STRICT).unstable_ids))
    assert seeds
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


def test_repair_timeout_marks_analysis_report_partial(tmp_path: Path):
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


def test_repair_worker_failure_keeps_infeasible_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
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


def test_repair_write_failure_keeps_infeasible_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
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


def test_analyze_cli_stdout_report_keeps_summary_on_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "heart.ldr"
    source.write_bytes(_HEART.read_bytes())

    code = main(["analyze", str(source), "--no-repair", "--report", "-"])

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["verdict"] == "feasible"
    assert "analysis: FEASIBLE" in captured.err
    assert not source.with_suffix(".analysis.json").exists()


def test_analyze_cli_rejects_unsafe_artifact_paths(tmp_path: Path):
    source = tmp_path / "heart.ldr"
    source.write_bytes(_HEART.read_bytes())

    with pytest.raises(SystemExit):
        main(["analyze", str(source), "--report", str(tmp_path / "evidence.txt")])
    with pytest.raises(SystemExit):
        main(["analyze", str(source), "-o", str(tmp_path / "repair.mpd")])
    with pytest.raises(SystemExit):
        main(["analyze", str(source), "-o", str(source)])


def test_catalog_extension_cannot_shadow_builtin_key(tmp_path: Path):
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
