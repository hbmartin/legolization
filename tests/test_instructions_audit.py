"""Auditing existing step-annotated models without re-running placement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legolization.instructions.audit import audit_model, reconstruct_plan
from legolization.instructions.sequencer import automatic_target_step_size
from legolization.ldraw_in import import_ldraw

GOLDEN = Path(__file__).parent / "data" / "golden" / "simple.ldr"
EXAMPLES = Path(__file__).parent.parent / "data" / "examples"


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, 3), (49, 3), (50, 5), (149, 5), (150, 7), (399, 7), (400, 10)],
)
def test_automatic_step_density_curve(count, expected):
    assert automatic_target_step_size(count) == expected


def test_reconstruct_plan_groups_by_source_steps():
    imported = import_ldraw(GOLDEN)
    plan = reconstruct_plan(imported)
    assert len(plan.steps) == 2
    assert sorted(plan.order) == sorted(brick.brick_id for brick in imported.layout)
    assert [step.index for step in plan.steps] == [1, 2]


def _sturdy_model(path: Path) -> Path:
    path.write_text(
        "0 tower\n"
        "0 Name: tower.ldr\n"
        "1 4 0 -24 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        "0 STEP\n"
        "1 14 0 -48 0 1 0 0 0 1 0 0 0 1 3001.dat\n"
        "0 STEP\n"
    )
    return path


def test_audit_certifies_a_sound_model(tmp_path):
    payload = audit_model(_sturdy_model(tmp_path / "tower.ldr"))
    assert payload["schema"] == "legolization.instructions-audit/v1"
    assert payload["verdict"] == "certified"
    assert payload["certification"]["valid"] is True
    assert payload["input"]["step_count"] == 2
    assert all(row["flags"] == [] for row in payload["steps"])


def test_audit_flags_insertion_fragile_steps():
    payload = audit_model(GOLDEN)
    assert payload["verdict"] == "findings"
    assert "insertion-fragile" in payload["steps"][1]["flags"]


def test_audit_flags_a_reordered_model(tmp_path):
    reordered = tmp_path / "reordered.ldr"
    lines = GOLDEN.read_text().splitlines()
    placements = [line for line in lines if line.startswith("1 ")]
    header = [line for line in lines if not line.startswith(("1 ", "0 STEP"))]
    # The plate goes first, floating above where the brick will be.
    reordered.write_text(
        "\n".join([*header, placements[1], "0 STEP", placements[0], "0 STEP"]) + "\n"
    )
    payload = audit_model(reordered)
    assert payload["verdict"] in {"findings", "infeasible"}
    first = payload["steps"][0]
    assert first["floating_after"] > 0 or first["components_after"] > 1


def test_audit_cli_writes_report_and_exit_codes(tmp_path, capsys):
    from legolization.cli import main

    model = _sturdy_model(tmp_path / "tower.ldr")
    code = main(["instructions", "audit", str(model), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["command"] == "instructions audit"
    assert payload["data"]["verdict"] == "certified"
    report = tmp_path / "tower.audit.json"
    assert report.is_file()
    assert json.loads(report.read_text())["verdict"] == "certified"


def test_audit_cli_reports_findings_as_partial(tmp_path, capsys):
    from legolization.cli import main

    lines = GOLDEN.read_text().splitlines()
    placements = [line for line in lines if line.startswith("1 ")]
    header = [line for line in lines if not line.startswith(("1 ", "0 STEP"))]
    model = tmp_path / "reordered.ldr"
    model.write_text(
        "\n".join([*header, placements[1], "0 STEP", placements[0], "0 STEP"]) + "\n"
    )
    code = main(["instructions", "audit", str(model), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code in {2, 3}
    assert payload["exit_code"] == code
