"""Profiling script tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pstats
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from legolization.telemetry import Telemetry

_SCRIPT = Path(__file__).parent.parent / "scripts" / "profile_pipeline.py"
_HEART = Path(__file__).parent.parent / "data" / "examples" / "heart.vox"


def _load_profiler() -> ModuleType:
    spec = importlib.util.spec_from_file_location("profile_pipeline_script", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def profiler() -> ModuleType:
    return _load_profiler()


def test_smoke_heart(
    profiler: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = profiler.main([str(_HEART), "--out", str(tmp_path)])
    assert exit_code == 0
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert payload["schema"] == 1
    assert payload["run"]["model"] == "heart"
    # File inputs: CLI mesh flags are the effective identity, and the
    # input bytes are pinned (PR #18 review).
    assert payload["run"]["input_source"] == "file"
    assert payload["run"]["target_studs"] == 32
    assert (
        payload["run"]["input_hash"] == hashlib.sha256(_HEART.read_bytes()).hexdigest()
    )
    assert payload["result"]["brick_count"] > 0
    assert payload["total_seconds"] > 0
    assert payload["spans"]["stability.analyze"]["calls"] >= 1
    assert payload["cprofile_active"] is False
    out = capsys.readouterr().out
    assert "stability.analyze" in out


def test_cprofile_writes_pstats(profiler: ModuleType, tmp_path: Path) -> None:
    exit_code = profiler.main([str(_HEART), "--out", str(tmp_path), "--cprofile"])
    assert exit_code == 0
    pstats_files = list(tmp_path.glob("*.pstats"))
    assert len(pstats_files) == 1
    stats = pstats.Stats(str(pstats_files[0]))
    assert getattr(stats, "total_calls", 0) > 0
    payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert payload["cprofile_active"] is True
    assert payload["cprofile_path"].endswith(".pstats")


def test_git_sha_in_repo_and_bare_dir(profiler: ModuleType, tmp_path: Path) -> None:
    sha = profiler.git_sha()
    assert sha is not None
    assert len(sha) == 40
    assert set(sha) <= set("0123456789abcdef")
    assert profiler.git_sha(tmp_path) is None


def test_unknown_corpus_name_exits(profiler: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="neither an existing input file"):
        profiler.main(["no-such-model", "--out", str(tmp_path)])


def test_corpus_synthetic_name_resolves(profiler: ModuleType, tmp_path: Path) -> None:
    # --target-studs 64 must NOT leak into the identity: corpus names
    # resolve from the manifest, synthetics have no mesh options at all
    # (PR #18 review: the payload recorded the ignored CLI flag).
    exit_code = profiler.main(
        ["letter-t", "--out", str(tmp_path), "--target-studs", "64"]
    )
    assert exit_code == 0
    payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert payload["run"]["model"] == "letter-t"
    assert payload["run"]["input_source"] == "synthetic"
    assert payload["run"]["input_hash"] == "generator:letter_t"
    assert payload["run"]["target_studs"] is None
    assert payload["result"]["brick_count"] > 0


def test_stage_transition_resets_watchdog_checkpoint(
    profiler: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / "running.json"
    lifecycle = profiler._Lifecycle.create(path, "fixture")  # noqa: SLF001
    session = Telemetry()
    lifecycle("start", "phase.voxelize", 10.0, session)
    assert json.loads(path.read_text())["stage_started"] == 10.0
    lifecycle("end", "phase.voxelize", 12.0, session)
    lifecycle("start", "place.tile", 20.0, session)
    payload = json.loads(path.read_text())
    assert payload["active_stage"] == "place.tile"
    assert payload["stage_started"] == 20.0


def test_enclosing_repair_stage_owns_nested_stability(
    profiler: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / "running.json"
    lifecycle = profiler._Lifecycle.create(path, "fixture")  # noqa: SLF001
    session = Telemetry()
    lifecycle("start", "phase.place", 10.0, session)
    lifecycle("start", "stability.analyze", 15.0, session)
    payload = json.loads(path.read_text())
    assert payload["active_stage"] == "phase.place"
    assert payload["stage_started"] == 10.0
    lifecycle("end", "stability.analyze", 16.0, session)
    lifecycle("start", "place.connectivity", 20.0, session)
    lifecycle("start", "stability.analyze", 30.0, session)
    payload = json.loads(path.read_text())
    assert payload["active_stage"] == "place.connectivity"
    assert payload["stage_started"] == 20.0
    lifecycle("end", "stability.analyze", 40.0, session)
    payload = json.loads(path.read_text())
    assert payload["active_stage"] == "place.connectivity"
    assert payload["stage_started"] == 20.0

    lifecycle("end", "place.connectivity", 50.0, session)
    lifecycle("end", "phase.place", 51.0, session)
    lifecycle("start", "phase.repair", 60.0, session)
    lifecycle("start", "stability.analyze", 70.0, session)
    payload = json.loads(path.read_text())
    assert payload["active_stage"] == "phase.repair"
    assert payload["stage_started"] == 60.0


def test_monitor_terminates_child_and_recovers_timeout_artifact(
    profiler: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = tmp_path / "profile.running.json"
    base = tmp_path / "profile"
    code = (
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({"
        "'active_stage':'place.connectivity',"
        "'stage_started':time.monotonic(),"
        "'spans':{'place.tile':{'calls':1,'seconds':0.01,'buckets':{}}}}))\n"
        "print('worker checkpoint', flush=True)\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(events)],
        stdout=subprocess.PIPE,
        text=True,
    )
    args = SimpleNamespace(
        heartbeat=0.01,
        stage_timeout=0.05,
        model="fixture",
        strategy="greedy",
        seed=0,
        steps="layer",
    )
    assert (
        profiler._monitor(  # noqa: SLF001
            process,
            args=args,
            base=base,
            events_path=events,
        )
        == 124
    )
    assert process.poll() is not None
    artifact = json.loads(base.with_suffix(".json").read_text())
    assert artifact["status"] == "timed_out"
    assert artifact["active_stage"] == "place.connectivity"
    assert artifact["spans"]["place.tile"]["calls"] == 1
    captured = capsys.readouterr()
    assert "stage: place.connectivity" in captured.err
    assert "timed out place.connectivity" in captured.err
    assert "worker checkpoint" in captured.out


def test_monitor_times_out_worker_that_hangs_during_startup(
    profiler: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events = tmp_path / "profile.running.json"
    base = tmp_path / "profile"
    code = "import time; print('starting', flush=True); time.sleep(30)"
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
    )
    args = SimpleNamespace(
        heartbeat=0.01,
        stage_timeout=0.05,
        model="fixture",
        strategy="greedy",
        seed=0,
        steps="layer",
    )

    assert (
        profiler._monitor(  # noqa: SLF001
            process,
            args=args,
            base=base,
            events_path=events,
        )
        == 124
    )
    artifact = json.loads(base.with_suffix(".json").read_text())
    assert artifact["active_stage"] == "startup"
    captured = capsys.readouterr()
    assert "stage: startup" in captured.err
    assert "timed out startup" in captured.err
    assert "starting" in captured.out
