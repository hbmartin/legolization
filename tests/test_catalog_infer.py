"""Catalog inference: geometry, sources, estimates, gates, activation."""

import json
from pathlib import Path

import pytest

from legolization.catalog import (
    CATALOG_ESTIMATES_SCHEMA,
    CATALOG_VALIDATION_SCHEMA,
    SUPPORT_ESTIMATES_FILENAME,
    SUPPORT_EXTENSION_FILENAME,
    SUPPORT_VALIDATION_FILENAME,
    resolve_catalog,
)
from legolization.catalog_infer.estimates import (
    ABS_DENSITY_G_PER_CM3,
    CELL_VOLUME_CM3,
    draft_mass_estimate,
    volumetric_mass_g,
)
from legolization.catalog_infer.geometry import (
    infer_geometry,
    merge_cell_boxes,
    normalize_part_id,
)
from legolization.catalog_infer.sources import (
    ENV_BRICKOWL_KEY,
    ENV_REBRICKABLE_KEY,
    SourceLookup,
    SourceReport,
    lookup_sources,
)
from legolization.catalog_infer.validate import (
    GATE_NAMES,
    GateResult,
    validate_extension,
)
from legolization.cli import main
from legolization.errors import ConfigurationError
from legolization.physical import LduBox, box_for_cell

_STUD_DAT = "0 Stud\n0 Name: stud.dat\n4 16 -6 0 -6 6 0 -6 6 0 6 -6 0 6\n"

_BRICK_1X2_DAT = """\
0 Test Brick 1 x 2
0 Name: 9901.dat
1 16 -10 0 0 1 0 0 0 1 0 0 0 1 stud.dat
1 16 10 0 0 1 0 0 0 1 0 0 0 1 stud.dat
4 16 -20 0 -10 20 0 -10 20 0 10 -20 0 10
4 16 -20 24 -10 20 24 -10 20 24 10 -20 24 10
4 16 -20 0 -10 -20 24 -10 -20 24 10 -20 0 10
4 16 20 0 -10 20 24 -10 20 24 10 20 0 10
4 16 -20 0 -10 20 0 -10 20 24 -10 -20 24 -10
4 16 -20 0 10 20 0 10 20 24 10 -20 24 10
"""

_DUMP_HEADER = (
    "Category ID\tCategory Name\tNumber\tName\tAlternate Item Number\t"
    "Weight (in Grams)\tDimensions\n"
)


def _mini_tree(root: Path) -> Path:
    """Fabricate a miniature LDraw library with one 1x2 test brick."""
    tree = root / "ldraw"
    (tree / "parts").mkdir(parents=True)
    (tree / "p").mkdir()
    (tree / "p" / "stud.dat").write_text(_STUD_DAT)
    (tree / "parts" / "9901.dat").write_text(_BRICK_1X2_DAT)
    return tree


@pytest.fixture
def mini_tree(tmp_path: Path) -> Path:
    return _mini_tree(tmp_path)


def _write_dump(path: Path, *rows: str) -> Path:
    path.write_text(_DUMP_HEADER + "".join(rows))
    return path


def _dump_row(number: str, mass: str, *, alternates: str = "") -> str:
    return f"5\tBricks\t{number}\tTest Brick 1 x 2\t{alternates}\t{mass}\t2 x 1\n"


# --- geometry -------------------------------------------------------------


def test_infer_geometry_expands_mini_part(mini_tree: Path):
    geometry = infer_geometry("9901", ldraw_dir=mini_tree)
    assert geometry.part_id == "9901"
    assert geometry.height_plates == 3
    assert geometry.occupied_cells == frozenset(
        (dx, 0, dz) for dx in range(2) for dz in range(3)
    )
    assert geometry.origin_offset == (0.0, 0.0, 0.0)
    assert [c.cell for c in geometry.top_connectors] == [(0, 0, 2), (1, 0, 2)]
    assert all(c.direction == (0, 0, 1) for c in geometry.top_connectors)
    assert [c.cell for c in geometry.bottom_connectors] == [(0, 0, 0), (1, 0, 0)]
    assert all(c.direction == (0, 0, -1) for c in geometry.bottom_connectors)
    assert geometry.collision_boxes == (
        LduBox(minimum=(-10, -10, 0), maximum=(30, 10, 24)),
    )
    assert geometry.confident
    assert geometry.notes == ()


def test_infer_geometry_records_stud_confidence(mini_tree: Path):
    geometry = infer_geometry("9901", ldraw_dir=mini_tree)
    assert all(c.confidence > 0.9 for c in geometry.top_connectors)
    assert all(0 < c.confidence < 0.9 for c in geometry.bottom_connectors)
    assert all("heuristic" in c.basis for c in geometry.bottom_connectors)


def test_infer_geometry_unknown_part_errors(mini_tree: Path):
    with pytest.raises(ConfigurationError, match="not available"):
        infer_geometry("31337", ldraw_dir=mini_tree)


def test_infer_geometry_requires_a_library(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="parts sync"):
        infer_geometry("9901", ldraw_dir=tmp_path)


def test_normalize_part_id_accepts_real_part_codes():
    assert normalize_part_id(" 3001.DAT ") == "3001"
    assert normalize_part_id("973c00") == "973c00"
    assert normalize_part_id("2454b") == "2454b"


@pytest.mark.parametrize(
    "part_id",
    ["", "   ", "../../x", "a/b", "a\\b", "a..b", "a&b=c", "a#b", ".dat", "-3001"],
)
def test_normalize_part_id_rejects_unsafe_ids(part_id: str):
    with pytest.raises(ConfigurationError, match="invalid LDraw part id"):
        normalize_part_id(part_id)


def test_merge_cell_boxes_covers_an_l_shape():
    cells = frozenset({(0, 0, 0), (1, 0, 0), (0, 1, 0)})
    boxes = merge_cell_boxes(cells)
    assert len(boxes) == 2
    for cell in sorted(cells):
        target = box_for_cell(cell)
        assert any(
            all(
                box.minimum[axis] <= target.minimum[axis]
                and target.maximum[axis] <= box.maximum[axis]
                for axis in range(3)
            )
            for box in boxes
        ), f"cell {cell} is not covered by any merged box"


# --- estimates ------------------------------------------------------------


def test_volumetric_estimate_math():
    assert pytest.approx(0.2048) == CELL_VOLUME_CM3
    assert volumetric_mass_g(24) == round(24 * 0.2048 * ABS_DENSITY_G_PER_CM3, 4)


def _report(*lookups: SourceLookup) -> SourceReport:
    return SourceReport(part_id="9901", lookups=tuple(lookups))


def _measured_lookup(mass: float) -> SourceLookup:
    return SourceLookup(
        source="bricklink-catalog-dump",
        url="file:///dump/Parts.txt",
        status="ok",
        reason=None,
        retrieved_at="2026-08-06T00:00:00+00:00",
        fields={"number": "9901", "name": "Test Brick", "mass_g": mass},
        license="BrickLink catalog data",
    )


def test_draft_estimate_prefers_measured_and_warns_on_disagreement():
    estimate = draft_mass_estimate("part_9901", 6, _report(_measured_lookup(0.5)))
    assert estimate.mass_g == 0.5
    assert estimate.method == "catalog-measured"
    assert estimate.measured
    assert len(estimate.warnings) == 1
    assert "disagree" in estimate.warnings[0]


def test_draft_estimate_measured_within_tolerance_has_no_warning():
    volumetric = volumetric_mass_g(6)
    estimate = draft_mass_estimate(
        "part_9901", 6, _report(_measured_lookup(volumetric * 1.1))
    )
    assert estimate.method == "catalog-measured"
    assert estimate.warnings == ()


def test_draft_estimate_falls_back_to_volumetric():
    estimate = draft_mass_estimate("part_9901", 6, _report())
    assert estimate.method == "volumetric"
    assert not estimate.measured
    assert estimate.mass_g == volumetric_mass_g(6)
    record = estimate.to_record()
    assert record.provenance.method == "volumetric"
    assert record.fields["mass_g"] == estimate.mass_g


# --- sources --------------------------------------------------------------


def _no_network(url: str, headers: dict[str, str], timeout_s: float) -> bytes:
    msg = "network must not be touched"
    raise AssertionError(msg)


def test_source_ladder_cites_every_offline_and_keyless_rung(tmp_path: Path):
    report = lookup_sources(
        "9901",
        offline=True,
        dump_path=tmp_path / "missing.txt",
        env={},
        fetch=_no_network,
    )
    assert [lookup.source for lookup in report.lookups] == [
        "bricklink-catalog-dump",
        "rebrickable-api",
        "brickowl-api",
    ]
    assert all(lookup.status == "skipped" for lookup in report.lookups)
    assert all(lookup.reason for lookup in report.lookups)
    assert report.measured is None


def test_source_ladder_keyless_rungs_skip_online_too(tmp_path: Path):
    report = lookup_sources(
        "9901",
        dump_path=tmp_path / "missing.txt",
        env={},
        fetch=_no_network,
    )
    statuses = {lookup.source: lookup.reason for lookup in report.lookups}
    assert "REBRICKABLE_API_KEY" in str(statuses["rebrickable-api"])
    assert "BRICKOWL_API_KEY" in str(statuses["brickowl-api"])


@pytest.mark.parametrize(
    ("dump_number", "queried"),
    [
        ("9901", "9901"),
        ("9901", "9901a"),  # revision suffix stripped
    ],
)
def test_source_ladder_reads_the_local_dump(
    tmp_path: Path, dump_number: str, queried: str
):
    dump = _write_dump(tmp_path / "Parts.txt", _dump_row(dump_number, "2.4"))
    report = lookup_sources(queried, offline=True, dump_path=dump, env={})
    bricklink = report.lookups[0]
    assert bricklink.status == "ok"
    assert bricklink.fields["mass_g"] == 2.4
    assert bricklink.fields["name"] == "Test Brick 1 x 2"
    assert report.measured is bricklink
    assert bricklink.license is not None
    assert "BrickLink" in bricklink.license


def test_source_ladder_dump_without_weight_is_identity_only(tmp_path: Path):
    dump = _write_dump(tmp_path / "Parts.txt", _dump_row("9901", "?"))
    report = lookup_sources("9901", offline=True, dump_path=dump, env={})
    bricklink = report.lookups[0]
    assert bricklink.status == "ok"
    assert "mass_g" not in bricklink.fields
    assert report.measured is None


def test_source_ladder_uses_fake_fetchers_for_keyed_apis(tmp_path: Path):
    calls: list[tuple[str, dict[str, str], float]] = []

    def fake_fetch(url: str, headers: dict[str, str], timeout_s: float) -> bytes:
        calls.append((url, headers, timeout_s))
        if "rebrickable" in url:
            return json.dumps({"part_num": "9901", "name": "Test Brick"}).encode()
        return json.dumps(
            {"results": [{"name": "Test Brick", "weight": "2.5"}]}
        ).encode()

    report = lookup_sources(
        "9901",
        dump_path=tmp_path / "missing.txt",
        env={ENV_REBRICKABLE_KEY: "rb-key", ENV_BRICKOWL_KEY: "bo-key"},
        fetch=fake_fetch,
        now=lambda: "2026-08-06T00:00:00+00:00",
    )
    rebrickable, brickowl = report.lookups[1], report.lookups[2]
    assert rebrickable.status == "ok"
    assert rebrickable.fields == {"part_num": "9901", "name": "Test Brick"}
    assert brickowl.status == "ok"
    assert brickowl.fields["mass_g"] == 2.5
    assert report.measured is brickowl
    assert all(timeout <= 5.0 for _, _, timeout in calls)
    assert calls[0][1] == {"Authorization": "key rb-key"}
    assert "bo-key" in calls[1][0]
    assert brickowl.url is not None
    assert "bo-key" not in brickowl.url
    assert "REDACTED" in brickowl.url


def test_source_ladder_records_fetch_errors(tmp_path: Path):
    def failing_fetch(url: str, headers: dict[str, str], timeout_s: float) -> bytes:
        msg = "HTTP 503"
        raise OSError(msg)

    report = lookup_sources(
        "9901",
        dump_path=tmp_path / "missing.txt",
        env={ENV_REBRICKABLE_KEY: "rb-key"},
        fetch=failing_fetch,
    )
    rebrickable = report.lookups[1]
    assert rebrickable.status == "error"
    assert rebrickable.reason == "HTTP 503"


def test_source_ladder_respects_the_total_budget(tmp_path: Path):
    report = lookup_sources(
        "9901",
        dump_path=tmp_path / "missing.txt",
        env={ENV_REBRICKABLE_KEY: "rb-key", ENV_BRICKOWL_KEY: "bo-key"},
        fetch=_no_network,
        budget_s=-1.0,
    )
    assert all(
        lookup.status == "skipped" and "budget" in str(lookup.reason)
        for lookup in report.lookups[1:]
    )


# --- validation gates -----------------------------------------------------


def _explicit_spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "key": "part_9902",
        "ldraw_part": "9902",
        "category": "special",
        "height_plates": 1,
        "occupied_cells": [[0, 0, 0], [1, 0, 0]],
        "filled_cells": [[0, 0, 0], [1, 0, 0]],
        "top_connectors": [
            {"cell": [0, 0, 0], "direction": [0, 0, 1]},
            {"cell": [1, 0, 0], "direction": [0, 0, 1]},
        ],
        "bottom_connectors": [
            {"cell": [0, 0, 0], "direction": [0, 0, -1]},
            {"cell": [1, 0, 0], "direction": [0, 0, -1]},
        ],
        "orientations": [0, 90, 180, 270],
        "origin_offset": [0.0, 0.0, 0.0],
        "collision_boxes_ldu": [
            {"minimum": [-10, -10, 0], "maximum": [30, 10, 8]},
        ],
    }
    spec.update(overrides)
    return spec


def _write_extension(path: Path, spec: dict[str, object]) -> Path:
    path.write_text(json.dumps({"schema": 2, "parts": [spec]}))
    return path


def _statuses(gates: tuple[GateResult, ...]) -> dict[str, str]:
    return {gate.gate: gate.status for gate in gates}


def test_all_gates_pass_for_a_correct_explicit_spec(tmp_path: Path):
    extension = _write_extension(tmp_path / "ext.json", _explicit_spec())
    gates = validate_extension(extension)
    assert [gate.gate for gate in gates] == list(GATE_NAMES)
    assert _statuses(gates) == dict.fromkeys(GATE_NAMES, "passed")


def test_collision_gate_fails_when_boxes_do_not_cover_cells(tmp_path: Path):
    extension = _write_extension(
        tmp_path / "ext.json",
        _explicit_spec(
            collision_boxes_ldu=[{"minimum": [-10, -10, 0], "maximum": [0, 0, 8]}]
        ),
    )
    statuses = _statuses(validate_extension(extension))
    assert statuses["collision"] == "failed"
    assert statuses["import"] == "passed"
    assert statuses["round-trip"] == "passed"


def test_connector_and_topology_gates_fail_without_connectors(tmp_path: Path):
    extension = _write_extension(
        tmp_path / "ext.json",
        _explicit_spec(top_connectors=[], bottom_connectors=[]),
    )
    statuses = _statuses(validate_extension(extension))
    assert statuses["connector"] == "failed"
    assert statuses["topology"] == "failed"
    assert statuses["collision"] == "passed"


def test_mate_gates_report_an_unplaceable_part():
    from dataclasses import replace

    from legolization.catalog import Catalog, default_catalog
    from legolization.catalog_infer.validate import _mate_above, _mate_below

    catalog = default_catalog()
    sunken = replace(
        catalog["plate_1x1"],
        key="part_sunken",
        occupied_cells=frozenset({(0, 0, -2), (0, 0, 0)}),
    )
    merged = Catalog(parts={**catalog.parts, "part_sunken": sunken})
    assert "cannot place in an empty layout" in str(_mate_above(merged, sunken))
    assert "cannot place in an empty layout" in str(_mate_below(merged, sunken))


def test_import_gate_failure_skips_the_remaining_gates(tmp_path: Path):
    extension = tmp_path / "ext.json"
    extension.write_text(json.dumps({"schema": 99, "parts": []}))
    gates = validate_extension(extension)
    assert gates[0].gate == "import"
    assert gates[0].status == "failed"
    assert all(gate.status == "skipped" for gate in gates[1:])


def test_import_gate_validates_sidecars(tmp_path: Path):
    extension = _write_extension(tmp_path / "ext.json", _explicit_spec())
    sidecar = tmp_path / "estimates.json"
    sidecar.write_text(json.dumps({"schema": "wrong"}))
    gates = validate_extension(extension, (sidecar,))
    assert gates[0].status == "failed"


# --- support-bundle activation in resolve_catalog -------------------------


def _passed_validation(**statuses: str) -> dict[str, object]:
    by_gate = dict.fromkeys(GATE_NAMES, "passed") | statuses
    return {
        "schema": CATALOG_VALIDATION_SCHEMA,
        "extension": SUPPORT_EXTENSION_FILENAME,
        "generated_at": "2026-08-06T00:00:00+00:00",
        "gates": [
            {"gate": name, "status": by_gate[name], "detail": "ok"}
            for name in GATE_NAMES
        ],
    }


def _support_dir(tmp_path: Path, validation: dict[str, object] | None) -> Path:
    directory = tmp_path / "part_9902-legolization-support"
    directory.mkdir()
    _write_extension(directory / SUPPORT_EXTENSION_FILENAME, _explicit_spec())
    (directory / SUPPORT_ESTIMATES_FILENAME).write_text(
        json.dumps(
            {
                "schema": CATALOG_ESTIMATES_SCHEMA,
                "estimates": [
                    {
                        "part": "part_9902",
                        "fields": {"mass_g": 3.3},
                        "provenance": {
                            "method": "volumetric",
                            "basis": "occupied-cell volume",
                        },
                    }
                ],
            }
        )
    )
    if validation is not None:
        (directory / SUPPORT_VALIDATION_FILENAME).write_text(json.dumps(validation))
    return directory


def test_resolve_catalog_activates_a_validated_support_dir(tmp_path: Path):
    directory = _support_dir(tmp_path, _passed_validation())
    resolved = resolve_catalog((directory,), ())
    part = resolved.catalog["part_9902"]
    assert part.mass_g == 3.3
    assert resolved.provenance.extensions == (directory / SUPPORT_EXTENSION_FILENAME,)
    assert resolved.provenance.estimate_sidecars == (
        directory / SUPPORT_ESTIMATES_FILENAME,
    )


def test_resolve_catalog_rejects_a_failed_gate(tmp_path: Path):
    directory = _support_dir(tmp_path, _passed_validation(collision="failed"))
    with pytest.raises(ConfigurationError, match="catalog validate"):
        resolve_catalog((directory,), ())


def test_resolve_catalog_rejects_a_recorded_gate_subset(tmp_path: Path):
    validation = _passed_validation()
    validation["gates"] = [
        {"gate": name, "status": "passed", "detail": "ok"}
        for name in GATE_NAMES
        if name != "topology"
    ]
    directory = _support_dir(tmp_path, validation)
    with pytest.raises(ConfigurationError, match=r"topology \(missing\)"):
        resolve_catalog((directory,), ())


def test_resolve_catalog_rejects_a_missing_validation_record(tmp_path: Path):
    directory = _support_dir(tmp_path, None)
    with pytest.raises(ConfigurationError, match="catalog validate"):
        resolve_catalog((directory,), ())


def test_resolve_catalog_rejects_a_directory_without_extension(tmp_path: Path):
    directory = tmp_path / "something-legolization-support"
    directory.mkdir()
    with pytest.raises(ConfigurationError, match="not a support bundle"):
        resolve_catalog((directory,), ())


def test_resolve_catalog_still_accepts_plain_extension_files(tmp_path: Path):
    extension = _write_extension(tmp_path / "ext.json", _explicit_spec(mass_g=1.5))
    resolved = resolve_catalog((extension,), ())
    assert resolved.catalog["part_9902"].mass_g == 1.5


# --- CLI envelopes --------------------------------------------------------


@pytest.fixture
def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tree = _mini_tree(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("LDRAWDIR", str(tree))
    for name in ("LEGOLIZATION_BRICKLINK_DUMP", ENV_REBRICKABLE_KEY, ENV_BRICKOWL_KEY):
        monkeypatch.delenv(name, raising=False)
    return work


def test_infer_cli_offline_writes_a_partial_support_bundle(
    cli_workspace: Path, capsys: pytest.CaptureFixture[str]
):
    assert main(["catalog", "infer", "9901", "--offline", "--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "catalog infer"
    assert payload["status"] == "partial"
    assert payload["data"]["validated"] is True
    assert payload["data"]["estimate"]["method"] == "volumetric"
    assert {row["status"] for row in payload["data"]["sources"]} == {"skipped"}
    directory = cli_workspace / "part_9901-legolization-support"
    for name in (
        "bundle.json",
        SUPPORT_EXTENSION_FILENAME,
        SUPPORT_ESTIMATES_FILENAME,
        "sources.json",
        SUPPORT_VALIDATION_FILENAME,
        "geometry/9901.dat",
        "geometry/occupancy.json",
    ):
        assert (directory / name).is_file()
    extension = json.loads((directory / SUPPORT_EXTENSION_FILENAME).read_text())
    assert "mass_g" not in extension["parts"][0]


def test_infer_cli_with_local_dump_is_complete(
    cli_workspace: Path, capsys: pytest.CaptureFixture[str]
):
    _write_dump(cli_workspace / "Parts.txt", _dump_row("9901", "2.4"))
    assert main(["catalog", "infer", "9901", "--offline", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert payload["data"]["estimate"]["method"] == "catalog-measured"
    assert payload["data"]["estimate"]["mass_g"] == 2.4
    directory = cli_workspace / "part_9901-legolization-support"
    record = json.loads((directory / "bundle.json").read_text())
    assert record["status"] == "complete"
    assert record["quality"] == "direct"
    sidecar = json.loads((directory / SUPPORT_ESTIMATES_FILENAME).read_text())
    assert sidecar["estimates"][0]["provenance"]["method"] == "catalog-measured"
    resolved = resolve_catalog((directory,), ())
    assert resolved.catalog["part_9901"].mass_g == 2.4


def test_infer_cli_reruns_reuse_the_identity_matched_bundle(cli_workspace: Path):
    assert main(["catalog", "infer", "9901", "--offline"]) == 3
    assert main(["catalog", "infer", "9901", "--offline"]) == 3
    assert (cli_workspace / "part_9901-legolization-support").is_dir()
    assert not (cli_workspace / "part_9901-legolization-support-2").exists()


def test_infer_cli_honours_key_and_output(cli_workspace: Path):
    target = cli_workspace / "bundles"
    target.mkdir()
    argv = ["catalog", "infer", "9901", "--key", "my_brick", "-o", str(target)]
    assert main([*argv, "--offline"]) == 3
    assert (target / "my_brick-legolization-support" / "bundle.json").is_file()


def test_infer_cli_unknown_part_is_operational(
    cli_workspace: Path, capsys: pytest.CaptureFixture[str]
):
    assert main(["catalog", "infer", "31337", "--offline", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "ConfigurationError"


def test_validate_cli_rewrites_validation_in_a_support_dir(
    cli_workspace: Path, capsys: pytest.CaptureFixture[str]
):
    assert main(["catalog", "infer", "9901", "--offline"]) == 3
    capsys.readouterr()
    directory = cli_workspace / "part_9901-legolization-support"
    validation_path = directory / SUPPORT_VALIDATION_FILENAME
    validation_path.write_text("garbage")
    assert main(["catalog", "validate", str(directory), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "catalog validate"
    assert payload["data"]["all_passed"] is True
    restored = json.loads(validation_path.read_text())
    assert restored["schema"] == CATALOG_VALIDATION_SCHEMA
    assert {gate["status"] for gate in restored["gates"]} == {"passed"}


def test_validate_cli_reports_failures_as_partial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    extension = _write_extension(
        tmp_path / "ext.json",
        _explicit_spec(top_connectors=[], bottom_connectors=[]),
    )
    assert main(["catalog", "validate", str(extension), "--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "partial"
    statuses = {gate["gate"]: gate["status"] for gate in payload["data"]["gates"]}
    assert statuses["connector"] == "failed"


def test_validate_cli_missing_target_is_operational(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert main(["catalog", "validate", str(tmp_path / "nope.json"), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["type"] == "ConfigurationError"
