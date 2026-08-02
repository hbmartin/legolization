"""BrickLink ``Parts.txt`` mass-ingestion tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).parent.parent / "scripts" / "ingest_bricklink_masses.py"
_HEADER = (
    "Category ID\tCategory Name\tNumber\tName\tAlternate Item Number\t"
    "Weight (in Grams)\tDimensions\r\n"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ingest_bricklink_masses", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def importer() -> ModuleType:
    return _load_script()


def _write_parts(path: Path, rows: list[str]) -> None:
    path.write_bytes((_HEADER + "\r\n".join(rows) + "\r\n").encode())


def _catalog(path: Path, parts: list[dict]) -> None:
    path.write_text(json.dumps({"schema": 1, "comment": "fixture", "parts": parts}))


def test_parses_crlf_export_and_missing_mass(
    importer: ModuleType,
    tmp_path: Path,
) -> None:
    source = tmp_path / "Parts.txt"
    _write_parts(
        source,
        [
            "1\tBrick\t3001\tBrick 2 x 4\told3001, 3001a\t2.32\t2 x 4 x 1",
            "2\tSlope\t3040\tSlope 45 2 x 1\t6270\t?\t2 x 1 x 1",
        ],
    )
    parts = importer.load_parts(source)
    assert parts[0].mass_g == pytest.approx(2.32)
    assert parts[0].alternate_numbers == ("old3001", "3001a")
    assert parts[1].mass_g is None


def test_match_order_exact_alternate_and_revision(
    importer: ModuleType,
    tmp_path: Path,
) -> None:
    source = tmp_path / "Parts.txt"
    _write_parts(
        source,
        [
            "1\tBrick\t3001\tBrick 2 x 4\t3001old\t2.32\t2 x 4 x 1",
            "2\tSlope\t3040\tSlope 45 2 x 1\t\t0.69\t2 x 1 x 1",
        ],
    )
    index = importer.build_index(importer.load_parts(source))
    assert importer.resolve_part("3001", index)[0] == "exact"
    assert importer.resolve_part("3001old", index)[0] == "alternate"
    assert importer.resolve_part("3040b.dat", index)[0] == "revision"
    assert importer.resolve_part("missing", index) == ("not_found", ())


def test_audit_and_apply_update_only_resolved_masses(
    importer: ModuleType,
    tmp_path: Path,
) -> None:
    source = tmp_path / "Parts.txt"
    _write_parts(
        source,
        [
            "1\tBrick\t3001\tBrick 2 x 4\t\t2.32\t2 x 4 x 1",
            "2\tSlope\t3040\tSlope 45 2 x 1\t\t?\t2 x 1 x 1",
        ],
    )
    catalog_path = tmp_path / "catalog.json"
    _catalog(
        catalog_path,
        [
            {"key": "brick", "ldraw_part": "3001", "mass_g": 2.16},
            {"key": "slope", "ldraw_part": "3040b", "mass_g": 0.9},
            {"key": "unknown", "ldraw_part": "9999", "mass_g": 1.0},
        ],
    )
    document = importer.load_catalog_document(catalog_path)
    audit = importer.audit_catalog(
        document,
        importer.build_index(importer.load_parts(source)),
    )
    assert audit.resolved_count == 1
    assert audit.changed_count == 1
    assert audit.unresolved_count == 2
    assert audit.rows[0].delta_percent == pytest.approx(7.407)
    assert audit.rows[1].match == "missing_mass"
    assert audit.rows[2].match == "not_found"

    assert importer.apply_masses(document, audit) == 1
    assert document["parts"][0]["mass_g"] == pytest.approx(2.32)
    assert document["parts"][1]["mass_g"] == pytest.approx(0.9)


def test_main_dry_run_then_atomic_write(
    importer: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "Parts.txt"
    _write_parts(source, ["1\tBrick\t3001\tBrick 2 x 4\t\t2.32\t2 x 4 x 1"])
    catalog_path = tmp_path / "catalog.json"
    original_text = (
        '{\n  "schema": 1,\n  "parts": [\n'
        '    {"key": "brick", "ldraw_part": "3001", '
        '"size": [2, 4], "mass_g": 2.16}\n  ]\n}\n'
    )
    original = json.loads(original_text)
    catalog_path.write_text(original_text)
    report_path = tmp_path / "audit" / "report.json"

    assert importer.main([str(source), "--catalog", str(catalog_path)]) == 0
    assert json.loads(catalog_path.read_text()) == original
    assert "resolved=1" in capsys.readouterr().out

    assert (
        importer.main(
            [
                str(source),
                "--catalog",
                str(catalog_path),
                "--write",
                "--json",
                str(report_path),
            ]
        )
        == 0
    )
    assert json.loads(catalog_path.read_text())["parts"][0]["mass_g"] == 2.32
    assert '"size": [2, 4]' in catalog_path.read_text()
    assert json.loads(report_path.read_text())["summary"]["resolved"] == 1


def test_rejects_wrong_header_and_invalid_mass(
    importer: ModuleType,
    tmp_path: Path,
) -> None:
    wrong_header = tmp_path / "wrong.txt"
    wrong_header.write_text("Number\tWeight (in Grams)\n3001\t2.32\n")
    with pytest.raises(ValueError, match="missing columns"):
        importer.load_parts(wrong_header)

    invalid_mass = tmp_path / "invalid.txt"
    _write_parts(
        invalid_mass,
        ["1\tBrick\t3001\tBrick 2 x 4\t\tnan\t2 x 4 x 1"],
    )
    with pytest.raises(ValueError, match="finite and positive"):
        importer.load_parts(invalid_mass)


def test_main_refuses_partial_write(
    importer: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "Parts.txt"
    _write_parts(source, ["1\tBrick\t3001\tBrick 2 x 4\t\t2.32\t2 x 4 x 1"])
    catalog_path = tmp_path / "catalog.json"
    original = {
        "schema": 1,
        "parts": [{"key": "unknown", "ldraw_part": "9999", "mass_g": 1.0}],
    }
    catalog_path.write_text(json.dumps(original))

    assert importer.main([str(source), "--catalog", str(catalog_path), "--write"]) == 2
    assert json.loads(catalog_path.read_text()) == original
    assert "refusing a partial catalog update" in capsys.readouterr().err
