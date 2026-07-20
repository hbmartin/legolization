"""Instruction-checker script tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest

from legolization.catalog import default_catalog
from legolization.instructions.bom import bill_of_materials
from legolization.instructions.sequencer import BuildStep, InstructionPlan
from legolization.layout import Layout

_SCRIPT = Path(__file__).parent.parent / "scripts" / "check_instructions.py"
_HEART = Path(__file__).parent.parent / "data" / "examples" / "heart.vox"


class _CheckerModule(Protocol):
    """Typed surface of the dynamically loaded checker script."""

    def check_steps(
        self,
        result_layout: Layout,
        plan: InstructionPlan,
        max_step_size: int,
        *,
        insertion_mass_kg: float | None = None,
    ) -> list[dict]:
        """Audit the instruction steps."""
        ...

    def main(self, argv: list[str] | None = None) -> int:
        """Run the checker CLI."""
        ...


def _load_checker() -> _CheckerModule:
    spec = importlib.util.spec_from_file_location("check_instructions", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast("_CheckerModule", module)


@pytest.fixture(scope="module")
def checker() -> _CheckerModule:
    return _load_checker()


def test_check_steps_flags_floating_prefix(checker: _CheckerModule) -> None:
    # Two stacked bricks sequenced upper-first: the first prefix dangles.
    layout = Layout(catalog=default_catalog())
    lower = layout.add(
        part_key="brick_1x2",
        x=0,
        y=0,
        layer=0,
        yaw=0,
        colour_code=4,
    ).brick_id
    upper = layout.add(
        part_key="brick_1x2",
        x=0,
        y=0,
        layer=3,
        yaw=0,
        colour_code=4,
    ).brick_id
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
    rows = checker.check_steps(
        result_layout=layout,
        plan=plan,
        max_step_size=10,
    )
    assert rows[0]["floating_after"] == 1
    assert "floating" in rows[0]["flags"]
    assert rows[1]["floating_after"] == 0
    assert rows[1]["flags"] == []


def test_check_steps_flags_oversized(checker: _CheckerModule) -> None:
    layout = Layout(catalog=default_catalog())
    ids = tuple(
        layout.add(
            part_key="brick_1x1",
            x=x,
            y=0,
            layer=0,
            yaw=0,
            colour_code=4,
        ).brick_id
        for x in range(4)
    )
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
    rows = checker.check_steps(
        result_layout=layout,
        plan=plan,
        max_step_size=3,
    )
    assert rows[0]["flags"] == ["oversized"]


def test_end_to_end_heart(
    checker: _CheckerModule,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_end_to_end_json_stdout(
    checker: _CheckerModule,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = checker.main([str(_HEART), "--json", "-"])
    assert exit_code in (0, 2)
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["input"].endswith("heart.vox")


def test_insertion_check_flags_press_fragile_steps(checker: _CheckerModule) -> None:
    # The single-stud cantilever holds statically but collapses under a
    # 1 kg press on the beam (Liu et al. 2024's virtual-brick model).
    layout = Layout(catalog=default_catalog())
    tower = layout.add(
        part_key="brick_1x1", x=0, y=0, layer=0, yaw=0, colour_code=4
    ).brick_id
    beam = layout.add(
        part_key="brick_1x4", x=0, y=0, layer=3, yaw=0, colour_code=4
    ).brick_id
    plan = InstructionPlan(
        steps=(
            BuildStep(
                index=1, brick_ids=(tower,), prefix_stable=True, prefix_max_score=0.0
            ),
            BuildStep(
                index=2, brick_ids=(beam,), prefix_stable=True, prefix_max_score=0.02
            ),
        ),
        warnings=(),
        bom=bill_of_materials(layout),
    )
    plain = checker.check_steps(result_layout=layout, plan=plan, max_step_size=10)
    assert all("insertion-fragile" not in row["flags"] for row in plain)
    audited = checker.check_steps(
        result_layout=layout, plan=plan, max_step_size=10, insertion_mass_kg=1.0
    )
    assert "insertion-fragile" not in audited[0]["flags"]  # pressing on ground
    assert "insertion-fragile" in audited[1]["flags"]


def test_unsupported_ratio_measures_overhang() -> None:
    import numpy as np

    from legolization.grid import VoxelGrid

    spec = importlib.util.spec_from_file_location(
        "eval_corpus_for_cs",
        Path(__file__).parent.parent / "scripts" / "eval_corpus.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    codes = np.full((2, 1, 2), -1, dtype=np.int16)
    codes[0, 0, 0] = 4  # grounded column
    codes[0, 0, 1] = 4  # supported
    codes[1, 0, 1] = 4  # overhang (nothing below)
    grid = VoxelGrid(codes=codes)
    assert module.unsupported_ratio(grid) == pytest.approx(1 / 3, abs=1e-4)
    assert module.unsupported_ratio(None) is None


_PRESS_TOWER = (
    Path(__file__).parent.parent / "data" / "corpus" / "synthetic" / "press-tower.npy"
)


def test_press_tower_pins_the_insertion_audit(
    checker: _CheckerModule,
    tmp_path: Path,
) -> None:
    # The corpus model built for the audit: statically clean end to end,
    # but the two-knob cantilever arms tear under Liu's 1 kg press —
    # --insertion-check must flag arm steps the plain audit passes.
    plain = tmp_path / "plain.json"
    assert checker.main([str(_PRESS_TOWER), "--json", str(plain)]) == 0
    plain_rows = json.loads(plain.read_text())["steps"]
    assert not any("unstable" in row["flags"] for row in plain_rows)
    assert not any("insertion-fragile" in row["flags"] for row in plain_rows)

    pressed = tmp_path / "pressed.json"
    # Exit 2: flags present without violations — exactly the point.
    assert (
        checker.main([str(_PRESS_TOWER), "--json", str(pressed), "--insertion-check"])
        == 2
    )
    pressed_rows = json.loads(pressed.read_text())["steps"]
    assert not any("unstable" in row["flags"] for row in pressed_rows)
    fragile = [r for r in pressed_rows if "insertion-fragile" in r["flags"]]
    assert len(fragile) >= 1  # measured: 4 of 18 steps


def test_attach_step_press_is_audited(checker: _CheckerModule) -> None:
    # PR #20 review (severity 2): attach steps place no direct bricks,
    # so the press audit silently skipped them; the whole seated unit
    # must be pressed. Fixture: a slim column whose subassembly (a
    # cantilevered arm unit) is statically seatable but press-fragile.
    from legolization.instructions.sequencer import (
        BuildStep,
        InstructionPlan,
        Subassembly,
    )

    catalog = default_catalog()
    layout = Layout(catalog=catalog)
    column = layout.add("brick_1x2", 0, 0, 0, 0, 1)
    arm = layout.add("plate_1x6", 0, 0, 3, 0, 4)
    plan = InstructionPlan(
        steps=(
            BuildStep(
                index=1,
                brick_ids=(column.brick_id,),
                prefix_stable=True,
                prefix_max_score=0.0,
            ),
            BuildStep(
                index=2,
                brick_ids=(arm.brick_id,),
                prefix_stable=True,
                prefix_max_score=0.0,
                submodel="sub-1",
            ),
            BuildStep(
                index=3,
                brick_ids=(),
                prefix_stable=True,
                prefix_max_score=0.01,
                attaches="sub-1",
            ),
        ),
        warnings=(),
        bom=bill_of_materials(layout),
        subassemblies=(
            Subassembly(name="sub-1", brick_ids=(arm.brick_id,), anchor_layer=3),
        ),
    )
    rows = checker.check_steps(layout, plan, 10, insertion_mass_kg=1.0)
    attach_row = next(row for row in rows if row["index"] == 3)
    assert "insertion-fragile" in attach_row["flags"]
