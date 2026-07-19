"""Telemetry span recording tests, including the behaviour-invariance guard."""

import json
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from legolization import telemetry
from legolization.grid import VoxelGrid
from legolization.pipeline import PipelineConfig, run
from legolization.telemetry import _Bucket, git_sha


def test_span_is_noop_when_disabled() -> None:
    assert telemetry.current() is None
    with telemetry.span("anything", n=5):
        time.sleep(0)
    assert telemetry.current() is None


def test_record_accumulates_calls_and_seconds() -> None:
    with telemetry.record() as session:
        with telemetry.span("a"):
            pass
        with telemetry.span("a"):
            pass
        with telemetry.span("b", n=10):
            pass
    assert session.spans["a"].calls == 2
    assert session.spans["a"].seconds >= 0.0
    assert session.spans["b"].calls == 1
    assert session.spans["b"].buckets == {
        16: _Bucket(calls=1, seconds=session.spans["b"].seconds)
    }


def test_nested_spans_both_record() -> None:
    with (
        telemetry.record() as session,
        telemetry.span("outer"),
        telemetry.span("inner"),
    ):
        pass
    assert session.spans["outer"].calls == 1
    assert session.spans["inner"].calls == 1


def _raise_inside_span() -> None:
    with telemetry.span("failing"):
        msg = "boom"
        raise ValueError(msg)


def test_exception_still_records_and_propagates() -> None:
    with telemetry.record() as session, pytest.raises(ValueError, match="boom"):
        _raise_inside_span()
    assert session.spans["failing"].calls == 1


def test_sequential_sessions_are_independent() -> None:
    with telemetry.record() as first, telemetry.span("x"):
        pass
    with telemetry.record() as second:
        pass
    assert "x" in first.spans
    assert second.spans == {}
    assert telemetry.current() is None


def test_bucket_boundaries_are_powers_of_two() -> None:
    with telemetry.record() as session:
        for n in (1, 2, 3, 100, 128, 129):
            with telemetry.span("s", n=n):
                pass
    assert set(session.spans["s"].buckets) == {1, 2, 4, 128, 256}


def test_to_dict_is_json_safe() -> None:
    with telemetry.record() as session, telemetry.span("s", n=7):
        pass
    payload = json.loads(json.dumps(session.to_dict()))
    assert payload["s"]["calls"] == 1
    assert payload["s"]["buckets"]["8"][0] == 1


def _placements(config: PipelineConfig) -> list[tuple[str, int, int, int, int, int]]:
    codes = np.full((5, 5, 2), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    result = run(grid, config)
    return sorted(
        (b.part_key, b.x, b.y, b.layer, b.yaw, b.colour_code)
        for b in result.layout.bricks.values()
    )


def test_recording_never_changes_behaviour() -> None:
    # The golden guard: identical placements with and without recording.
    config = PipelineConfig(seed=0)
    plain = _placements(config)
    with telemetry.record() as session:
        recorded = _placements(config)
    assert plain == recorded
    assert session.spans["stability.analyze"].calls >= 1
    assert (
        session.spans["stability.build_model"].calls
        >= session.spans["stability.analyze"].calls
    )
    assert "phase.place" in session.spans
    assert "phase.sequencing" in session.spans


def test_value_gauge_is_noop_when_disabled() -> None:
    telemetry.value("anything", 42.0)  # must not raise or leak
    assert telemetry.current() is None


def test_value_gauge_accumulates_in_order() -> None:
    with telemetry.record() as session:
        telemetry.value("g", 3.0)
        telemetry.value("g", 1.0)
        telemetry.value("other", 7.5)
    assert session.values_dict() == {"g": [3.0, 1.0], "other": [7.5]}
    assert "values" not in session.to_dict()  # span schema untouched


def test_value_gauge_sessions_are_independent() -> None:
    with telemetry.record() as first:
        telemetry.value("x", 1.0)
    with telemetry.record() as second:
        pass
    assert first.values == {"x": [1.0]}
    assert second.values == {}


def test_git_sha_resolves_linked_worktrees(tmp_path: Path) -> None:
    # PR #18 P2: in a linked worktree .git is a FILE with a gitdir:
    # indirection; the sha must resolve through it and through
    # commondir for refs.
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "x"], check=True, env=env
    )
    main_sha = git_sha(repo)
    assert main_sha is not None
    assert len(main_sha) == 40

    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)],
        check=True,
        env=env,
    )
    assert (worktree / ".git").is_file()  # the indirection under test
    assert git_sha(worktree) == main_sha

    # Packed refs still resolve through commondir.
    subprocess.run(["git", "-C", str(repo), "pack-refs", "--all"], check=True, env=env)
    assert git_sha(worktree) == main_sha


def test_bucket_legacy_sequence_interface() -> None:
    # Buckets were [calls, seconds] lists before the typed dataclass;
    # index/len/iter must keep that contract (PR #18 review).
    with telemetry.record() as session, telemetry.span("s", n=7):
        pass
    bucket = session.spans["s"].buckets[8]
    assert bucket[0] == 1
    assert bucket[1] == bucket.seconds
    assert len(bucket) == 2
    calls, seconds = bucket
    assert (calls, seconds) == (bucket.calls, bucket.seconds)
    assert list(bucket) == [bucket.calls, bucket.seconds]
