"""Golden pins for the shipped examples (audit F12: stale-example guard).

These pin exact brick counts under the CLI's default restart policy
(race seeds 0..2 without instructions, re-run the winner) — numpy's
``default_rng`` is deterministic across platforms, so any drift means
placement or selection behaviour changed. When a change is
intentional, regenerate the goldens AND the shipped
``data/examples/*.ldr`` files together:

    uv run legolization data/examples/pyramid.npy -o data/examples/pyramid.ldr
    uv run legolization data/examples/arch.npy -o data/examples/arch.ldr
    uv run legolization data/examples/heart.vox -o data/examples/heart.ldr
"""

from pathlib import Path

import pytest

from legolization.compare import restart_race
from legolization.pipeline import PipelineConfig, load_grid, run_file

_EXAMPLES = Path(__file__).parent.parent / "data" / "examples"
_RESTART_SEEDS = (0, 1, 2)

_GOLDEN = {
    "pyramid.npy": (0, 124),
    "arch.npy": (1, 15),
    "heart.vox": (2, 12),
}


def _raced_config(source: Path) -> PipelineConfig:
    """Mirror the CLI's default policy: race the seeds, keep the winner."""
    config = PipelineConfig(seed=0)
    grid = load_grid(source, config)
    winner_seed, _report = restart_race(grid, config, seeds=_RESTART_SEEDS)
    return PipelineConfig(seed=winner_seed)


@pytest.mark.parametrize(("name", "golden"), sorted(_GOLDEN.items()))
def test_example_brick_counts(name, golden, tmp_path):
    winner_seed, bricks = golden
    config = _raced_config(_EXAMPLES / name)
    assert config.seed == winner_seed
    result = run_file(_EXAMPLES / name, tmp_path / "out.ldr", config)
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
    run_file(source, fresh, _raced_config(source))
    assert fresh.read_text() == shipped.read_text()
