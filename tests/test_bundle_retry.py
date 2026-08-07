"""The material-ladder retry: order, fair budget, rollforward, promotion."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from legolization.bundle import retry
from legolization.bundle.orchestrator import BundleRequest
from legolization.bundle.record import read_record
from legolization.cli.envelope import ResultEnvelope
from legolization.configuration import ProjectConfig


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _box(path: Path) -> Path:
    codes = np.full((3, 3, 2), -1, dtype=np.int16)
    codes[:2, :, :] = 4
    np.save(path, codes)
    return path


def _scripted_runner(script, clock, calls) -> retry.BundleRunner:
    """Record each rung invocation and return the scripted exit code."""

    def _run(request: BundleRequest) -> ResultEnvelope:
        rung = request.output_dir.name if request.output_dir is not None else "?"
        calls.append(
            {
                "rung": rung,
                "duration": request.duration_s,
                "hollow": request.config.geometry.hollow,
                "shell_plates": request.config.geometry.shell_plates,
                "started_at": clock(),
            }
        )
        elapsed, exit_code = script[rung]
        clock.now += elapsed
        if exit_code == 0 and request.output_dir is not None:
            model_dir = request.output_dir / "model"
            model_dir.mkdir(parents=True, exist_ok=True)
            (model_dir / "model.mpd").write_text("0 rung winner\n")
            bom_dir = request.output_dir / "bom"
            bom_dir.mkdir(parents=True, exist_ok=True)
            (bom_dir / "bom.json").write_text("{}\n")
        return ResultEnvelope(command="bundle", exit_code=exit_code)

    return _run


def test_ladder_stops_at_first_buildable_and_promotes(tmp_path):
    box = _box(tmp_path / "box.npy")
    clock = _FakeClock()
    calls: list[dict] = []
    runner = _scripted_runner(
        {"four-plate": (50.0, 2), "six-plate": (30.0, 0)},
        clock,
        calls,
    )
    envelope = retry.run_retry(
        BundleRequest(input_path=box, config=ProjectConfig(), clock=clock),
        total_budget_s=300.0,
        runner=runner,
    )
    assert [call["rung"] for call in calls] == ["four-plate", "six-plate"]
    assert calls[0]["duration"] == pytest.approx(100.0)
    assert calls[1]["duration"] == pytest.approx(150.0)
    assert envelope.exit_code == 0
    bundle_dir = tmp_path / "box-legolization"
    assert (bundle_dir / "model" / "model.mpd").is_file()
    assert (bundle_dir / "bom" / "bom.json").is_file()
    record = read_record(bundle_dir)
    assert record is not None
    assert record.verdicts["winner"]["rung"] == "six-plate"
    rungs = record.stages["retry"].detail["rungs"]
    assert [rung["name"] for rung in rungs] == ["four-plate", "six-plate"]
    assert rungs[0]["status"] == "failed"
    assert rungs[1]["status"] == "buildable"


def test_ladder_configs_change_only_geometry(tmp_path):
    box = _box(tmp_path / "box.npy")
    clock = _FakeClock()
    calls: list[dict] = []
    runner = _scripted_runner(
        {"four-plate": (1.0, 2), "six-plate": (1.0, 2), "solid": (1.0, 2)},
        clock,
        calls,
    )
    base = ProjectConfig()
    envelope = retry.run_retry(
        BundleRequest(input_path=box, config=base, clock=clock),
        total_budget_s=300.0,
        runner=runner,
    )
    assert envelope.exit_code == 2
    assert [call["rung"] for call in calls] == ["four-plate", "six-plate", "solid"]
    assert [call["shell_plates"] for call in calls[:2]] == [4, 6]
    assert calls[2]["hollow"] is False
    record = read_record(tmp_path / "box-legolization")
    assert record is not None
    assert record.status == "unbuildable"
    assert record.verdicts["buildable"] is False
    for call in calls:
        assert call["hollow"] in {True, False}


def test_overrun_eats_the_next_rung_share(tmp_path):
    box = _box(tmp_path / "box.npy")
    clock = _FakeClock()
    calls: list[dict] = []
    runner = _scripted_runner(
        {"four-plate": (250.0, 2), "six-plate": (100.0, 2), "solid": (10.0, 2)},
        clock,
        calls,
    )
    envelope = retry.run_retry(
        BundleRequest(input_path=box, config=ProjectConfig(), clock=clock),
        total_budget_s=300.0,
        runner=runner,
    )
    assert envelope.exit_code == 2
    assert [call["rung"] for call in calls] == ["four-plate", "solid"]
    record = read_record(tmp_path / "box-legolization")
    assert record is not None
    rungs = record.stages["retry"].detail["rungs"]
    assert rungs[1] == {
        "name": "six-plate",
        "status": "skipped",
        "reason": "budget exhausted",
    }
    assert calls[1]["duration"] == pytest.approx(50.0)


def test_retry_identity_is_distinct_from_the_original(tmp_path):
    from legolization.bundle.identity import bundle_identity

    box = _box(tmp_path / "box.npy")
    config = ProjectConfig()
    plain = bundle_identity(box, config)
    retried = bundle_identity(box, config, invocation={"retry": "material-ladder"})
    assert plain != retried


def test_cli_retry_requires_duration(tmp_path, capsys):
    from legolization.cli import main

    box = _box(tmp_path / "box.npy")
    assert main(["bundle", str(box), "--retry-materials", "--json"]) == 1
    assert "--duration" in capsys.readouterr().err
