"""Golden pins for the shipped examples (audit F12: stale-example guard).

These pin exact brick counts at seed 0 — numpy's ``default_rng`` is
deterministic across platforms, so any drift means placement behaviour
changed. When a change is intentional, regenerate the goldens AND the
shipped ``data/examples/*.ldr`` files together:

    uv run legolization data/examples/pyramid.npy -o data/examples/pyramid.ldr
    uv run legolization data/examples/arch.npy -o data/examples/arch.ldr
    uv run legolization data/examples/heart.vox -o data/examples/heart.ldr
"""

from pathlib import Path

import pytest

from legolization.pipeline import PipelineConfig, run_file

_EXAMPLES = Path(__file__).parent.parent / "data" / "examples"

_GOLDEN = {
    "pyramid.npy": 124,
    "arch.npy": 32,
    "heart.vox": 12,
}


@pytest.mark.parametrize(("name", "bricks"), sorted(_GOLDEN.items()))
def test_example_brick_counts(name, bricks, tmp_path):
    result = run_file(
        _EXAMPLES / name,
        tmp_path / "out.ldr",
        PipelineConfig(seed=0),
    )
    assert result.buildable
    assert result.brick_count == bricks


@pytest.mark.parametrize("name", sorted(_GOLDEN))
def test_shipped_ldr_matches_current_code(name, tmp_path):
    # The checked-in .ldr files must be regenerable byte-for-byte (module
    # the embedded model name) — this is exactly the staleness the audit
    # caught (F12: shipped heart.ldr had 31 bricks, the code produced 40).
    source = _EXAMPLES / name
    shipped = _EXAMPLES / f"{source.stem}.ldr"
    fresh = tmp_path / shipped.name
    run_file(source, fresh, PipelineConfig(seed=0))
    assert fresh.read_text() == shipped.read_text()
