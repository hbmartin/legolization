"""Instruction-checker script tests."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from legolization.catalog import default_catalog
from legolization.instructions.bom import bill_of_materials
from legolization.instructions.sequencer import BuildStep, InstructionPlan
from legolization.layout import Layout

_SCRIPT = Path(__file__).parent.parent / "scripts" / "check_instructions.py"
_HEART = Path(__file__).parent.parent / "data" / "examples" / "heart.vox"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_instructions", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    return _load_checker()


def test_check_steps_flags_floating_prefix(checker):
    # Two stacked bricks sequenced upper-first: the first prefix dangles.
    layout = Layout(catalog=default_catalog())
    lower = layout.add("brick_1x2", 0, 0, 0, 0, 4).brick_id
    upper = layout.add("brick_1x2", 0, 0, 3, 0, 4).brick_id
    plan = InstructionPlan(
        steps=(
            BuildStep(
                index=1,
                brick_ids=(upper,),
                prefix_stable=False,
                prefix_max_score=1.0,
            ),
            BuildStep(
                index=2,
                brick_ids=(lower,),
                prefix_stable=True,
                prefix_max_score=0.0,
            ),
        ),
        warnings=(),
        bom=bill_of_materials(layout),
    )
    rows = checker.check_steps(layout, plan, max_step_size=10)
    assert rows[0]["floating_after"] == 1
    assert "floating" in rows[0]["flags"]
    assert rows[1]["floating_after"] == 0
    assert rows[1]["flags"] == []


def test_check_steps_flags_oversized(checker):
    layout = Layout(catalog=default_catalog())
    ids = tuple(layout.add("brick_1x1", x, 0, 0, 0, 4).brick_id for x in range(4))
    plan = InstructionPlan(
        steps=(
            BuildStep(
                index=1,
                brick_ids=ids,
                prefix_stable=True,
                prefix_max_score=0.0,
            ),
        ),
        warnings=(),
        bom=bill_of_materials(layout),
    )
    rows = checker.check_steps(layout, plan, max_step_size=3)
    assert rows[0]["flags"] == ["oversized"]


def test_end_to_end_heart(checker, tmp_path, capsys):
    json_path = tmp_path / "report.json"
    exit_code = checker.main([str(_HEART), "--json", str(json_path)])
    # The heart's lobes start as floating islands: warnings, not violations.
    assert exit_code in (0, 2)
    payload = json.loads(json_path.read_text())
    assert payload["violations"] == []
    assert payload["brick_count"] > 0
    assert payload["quality"]["step_count"] == len(payload["steps"])
    for row in payload["steps"]:
        assert ("floating" in row["flags"]) == (row["floating_after"] > 0)
    out = capsys.readouterr().out
    assert "steps" in out


def test_end_to_end_json_stdout(checker, capsys):
    exit_code = checker.main([str(_HEART), "--json", "-"])
    assert exit_code in (0, 2)
    stdout = capsys.readouterr().out
    payload, _end = json.JSONDecoder().raw_decode(stdout[stdout.index("{") :])
    assert payload["input"].endswith("heart.vox")
