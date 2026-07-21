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
    rows = {(row["phase"], row["occurrence"]): row for row in payload["trajectory"]}
    # The engine phases carry actual component values now, not just
    # labels (PR #18 review: the smoke test must assert the gauges).
    tiled = rows[("tiled (per-layer minimum)", 1)]
    assert tiled["bricks"] > 0
    assert tiled["components"] is not None
    assert tiled["components"] >= 1
    compacted = rows[("compact_vertical", 1)]
    assert compacted["components"] is not None
    remerged = rows[("final_remerge", 1)]
    assert remerged["stable"] is not None
    assert remerged["components"] == payload["result"]["components"]
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


def test_rephase_ablation_is_recorded(
    script: ModuleType,
    tmp_path: Path,
) -> None:
    exit_code = script.main(
        [
            "letter-t",
            "--strategy",
            "bond",
            "--bridge-rephase",
            "--out",
            str(tmp_path),
        ]
    )
    assert exit_code == 0
    written = next(iter(tmp_path.glob("*-rephase-trajectory.json")))
    payload = json.loads(written.read_text())
    assert payload["run"]["bridge_rephase"] is True


def test_rows_follow_emission_order(script: ModuleType) -> None:
    # PR #18 P2: repair is emitted before placed inside _place_and_repair,
    # and hollow-restore interleaves pass-two tiling — rows must follow
    # the global event sequence, not per-name grouping.
    events = [
        ("place.tiled.bricks", 100.0),
        ("place.compacted.bricks", 100.0),
        ("place.connected.bricks", 120.0),
        ("pipeline.repaired.bricks", 118.0),
        ("pipeline.repaired.components", 1.0),
        ("pipeline.repaired.stable", 0.0),
        ("pipeline.placed.bricks", 118.0),
        ("pipeline.placed.components", 1.0),
        ("pipeline.placed.stable", 0.0),
        ("pipeline.hollow_restore.round", 1.0),
        ("place.tiled.bricks", 130.0),
        ("place.compacted.bricks", 130.0),
        ("place.connected.bricks", 140.0),
        ("pipeline.restored.bricks", 140.0),
        ("pipeline.restored.components", 1.0),
        ("pipeline.restored.stable", 1.0),
        ("pipeline.remerged.bricks", 135.0),
        ("pipeline.remerged.components", 1.0),
        ("pipeline.remerged.stable", 1.0),
    ]
    rows = script._rows(events)  # noqa: SLF001 - script-internal audit
    sequence = [(row.phase, row.occurrence, row.bricks) for row in rows]
    assert sequence == [
        ("tiled (per-layer minimum)", 1, 100),
        ("compact_vertical", 1, 100),
        ("improve_connectivity", 1, 120),
        ("post-repair", 1, 118),  # BEFORE placed: true emission order
        ("post-place (incl. repair)", 1, 118),
        ("tiled (per-layer minimum)", 2, 130),
        ("compact_vertical", 2, 130),
        ("improve_connectivity", 2, 140),
        ("hollow-restore round", 1, 140),
        ("final_remerge", 1, 135),
    ]
    restored = next(row for row in rows if row.phase == "hollow-restore round")
    assert restored.stable is True
    assert restored.components == 1


def test_cli_rejects_invalid_inputs(script: ModuleType) -> None:
    # PR #18 P3: an unknown strategy or nonsensical numeric flag ran a
    # whole pipeline before failing (or never failed); argparse now
    # rejects them up front.
    for argv in (
        ["heart", "--strategy", "no-such-strategy"],
        ["heart", "--target-studs", "0"],
        ["heart", "--fail-max", "-1"],
    ):
        with pytest.raises(SystemExit):
            script.main(argv)
