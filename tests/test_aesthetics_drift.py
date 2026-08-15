"""Operator invariants and determinism for the permutation-drift harness.

The harness's claim is that every trajectory step keeps the layout a valid
brick assembly (no cell collisions, never newly disconnected, brick count
moving by at most one per step). If an operator can silently break that, the
drift statistics measure corruption artifacts rather than the metrics.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.layout import Layout

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).parent.parent


def _load_drift_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aesthetics_drift", _REPO / "scripts" / "aesthetics_drift.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["aesthetics_drift"] = module
    spec.loader.exec_module(module)
    return module


def _tower() -> Layout:
    layout = Layout(catalog=default_catalog())
    layout.add("brick_2x4", 0, 0, 0, 0, 4)
    layout.add("brick_2x4", 0, 0, 3, 90, 1)
    layout.add("brick_2x2", 0, 0, 6, 0, 4)
    layout.add("brick_1x2", 2, 0, 6, 0, 2)
    return layout


def test_perturbation_preserves_layout_invariants():
    drift = _load_drift_module()
    layout = _tower()
    palette = sorted(brick.colour_code for brick in layout)
    components = ConnectionGraph.from_layout(layout).component_count()
    rng = np.random.default_rng(7)
    for _ in range(120):
        before = len(layout)
        drift.perturb(layout, rng, palette=palette, max_tries=20)
        # The occupancy index stays exact: every cell of every brick, no more.
        cells = {cell for brick in layout for cell in layout.cells_of(brick)}
        assert set(layout.occupancy) == cells
        assert ConnectionGraph.from_layout(layout).component_count() <= components
        assert abs(len(layout) - before) <= 1
        assert all(brick.colour_code in palette for brick in layout)


def test_trajectory_is_deterministic_per_seed():
    drift = _load_drift_module()
    config = drift.DriftConfig(steps=40, every=2, seeds=(0,), max_tries=10)
    first = drift.trajectory(_tower(), config, seed=3)
    second = drift.trajectory(_tower(), config, seed=3)
    assert first == second
    # The 4-brick tower drifts min(40, 2 x 4) = 8 steps, measured every 2.
    assert len(first["symmetry"]) == 1 + 8 // 2


def test_delete_never_disconnects_a_two_brick_tower():
    drift = _load_drift_module()
    rng = np.random.default_rng(0)
    for _ in range(30):
        layout = Layout(catalog=default_catalog())
        layout.add("brick_1x2", 0, 0, 0, 0, 4)
        layout.add("brick_1x2", 0, 0, 3, 0, 4)
        drift._op_delete(layout, rng, [4])  # noqa: SLF001
        assert len(layout) >= 1
        assert ConnectionGraph.from_layout(layout).component_count() == 1


def test_colour_operators_reject_visual_noops():
    drift = _load_drift_module()
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    before = [(brick.brick_id, brick.colour_code) for brick in layout]

    assert not drift._op_recolour(layout, np.random.default_rng(0), [4])  # noqa: SLF001
    assert not drift._op_swap(layout, np.random.default_rng(0), [4])  # noqa: SLF001
    assert [(brick.brick_id, brick.colour_code) for brick in layout] == before


def test_constant_series_scores_zero_rho():
    drift = _load_drift_module()
    assert drift._rho([0.25, 0.25, 0.25, 0.25]) == 0.0  # noqa: SLF001
    assert drift._rho([0.0, 0.1, 0.2, 0.3]) == 1.0  # noqa: SLF001
