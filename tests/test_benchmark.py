"""Benchmark harness smoke tests (full sweeps are marked slow)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from legolization.grid import VoxelGrid
from legolization.pipeline import PipelineConfig, PipelineResult, run
from legolization.placement.registry import strategy_names

_SCRIPT = Path(__file__).parent.parent / "scripts" / "benchmark.py"


def _load_benchmark() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_strategies_build_the_pyramid():
    codes = np.full((5, 5, 2), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    for strategy in strategy_names():
        result = run(grid, PipelineConfig(strategy=strategy, seed=0))
        assert result.buildable, f"{strategy} produced an unbuildable box"


def test_markdown_rendering():
    module = _load_benchmark()
    rows = [{"model": "m", "strategy": "s", "bricks": 3}]
    table = module.to_markdown(rows)
    assert table.splitlines()[0] == "| model | strategy | bricks |"
    assert "| m | s | 3 |" in table


def test_empty_markdown_rendering():
    module = _load_benchmark()
    assert module.to_markdown([]) == ""


def test_benchmark_isolates_strategy_failures(monkeypatch, capsys):
    module = _load_benchmark()
    grid = VoxelGrid(codes=np.full((1, 1, 3), 4, dtype=np.int16))
    real_run = module.run

    def selective_run(grid, config) -> PipelineResult:
        if config.strategy == "broken":
            msg = "synthetic failure"
            raise RuntimeError(msg)
        return real_run(grid, config)

    monkeypatch.setattr(module, "_example_grids", lambda: {"tiny": grid})
    monkeypatch.setattr(module, "strategy_names", lambda: ("broken", "greedy"))
    monkeypatch.setattr(module, "run", selective_run)

    rows = module.benchmark(seed=0)

    assert [row["strategy"] for row in rows] == ["greedy"]
    assert "broken" in capsys.readouterr().err


@pytest.mark.slow
def test_full_benchmark_sweep():
    module = _load_benchmark()
    rows = module.benchmark(seed=0)
    assert len(rows) == len(strategy_names()) * 4
    assert all(row["buildable"] for row in rows)
