"""Telemetry span recording tests, including the behaviour-invariance guard."""

import json
import time

import numpy as np
import pytest

from legolization import telemetry
from legolization.grid import VoxelGrid
from legolization.pipeline import PipelineConfig, run
from legolization.telemetry import _Bucket


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
