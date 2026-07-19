"""Count-trajectory diagnostic script."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "count_trajectory.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("count_trajectory_script", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return _load()


def test_smoke_letter_t(
    script: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = script.main(["letter-t", "--strategy", "bond", "--out", str(tmp_path)])
    assert exit_code == 0
    written = list(tmp_path.glob("*-trajectory.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert payload["schema"] == 1
    assert payload["run"]["strategy"] == "bond"
    assert payload["result"]["brick_count"] > 0
    labels = [row["phase"] for row in payload["trajectory"]]
    assert "tiled (per-layer minimum)" in labels
    assert "final_remerge" in labels
    out = capsys.readouterr().out
    assert "final_remerge" in out


def test_fail_max_zero_disables_connectivity(
    script: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = script.main(
        ["letter-t", "--strategy", "bond", "--fail-max", "0", "--out", str(tmp_path)]
    )
    assert exit_code == 0
    payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert payload["connectivity"]["attempts"] == 0
    assert payload["run"]["fail_max"] == 0
