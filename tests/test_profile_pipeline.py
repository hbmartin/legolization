"""Profiling script tests."""

import importlib.util
import json
import pstats
import sys
from pathlib import Path
from types import ModuleType

import pytest

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


def test_smoke_heart(profiler, tmp_path, capsys):
    exit_code = profiler.main([str(_HEART), "--out", str(tmp_path)])
    assert exit_code == 0
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert payload["schema"] == 1
    assert payload["run"]["model"] == "heart"
    assert payload["result"]["brick_count"] > 0
    assert payload["total_seconds"] > 0
    assert payload["spans"]["stability.analyze"]["calls"] >= 1
    assert payload["cprofile_active"] is False
    out = capsys.readouterr().out
    assert "stability.analyze" in out


def test_cprofile_writes_pstats(profiler, tmp_path):
    exit_code = profiler.main([str(_HEART), "--out", str(tmp_path), "--cprofile"])
    assert exit_code == 0
    pstats_files = list(tmp_path.glob("*.pstats"))
    assert len(pstats_files) == 1
    stats = pstats.Stats(str(pstats_files[0]))
    assert getattr(stats, "total_calls", 0) > 0
    payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert payload["cprofile_active"] is True
    assert payload["cprofile_path"].endswith(".pstats")


def test_git_sha_in_repo_and_bare_dir(profiler, tmp_path):
    sha = profiler.git_sha()
    assert sha is not None
    assert len(sha) == 40
    assert set(sha) <= set("0123456789abcdef")
    assert profiler.git_sha(tmp_path) is None


def test_unknown_corpus_name_exits(profiler, tmp_path):
    with pytest.raises(SystemExit, match="neither an existing input file"):
        profiler.main(["no-such-model", "--out", str(tmp_path)])


def test_corpus_synthetic_name_resolves(profiler, tmp_path):
    exit_code = profiler.main(["letter-t", "--out", str(tmp_path)])
    assert exit_code == 0
    payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert payload["run"]["model"] == "letter-t"
    assert payload["result"]["brick_count"] > 0
