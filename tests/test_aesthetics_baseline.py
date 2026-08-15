"""Sampling behaviour of the population-comparison script.

``algorithmic_scores`` oversamples, drops ineligible rows, then caps at
``limit``. ``load_selected`` returns rows in ascending global-index order, so
capping without a reshuffle would silently keep the lowest indices - a fact
about parquet shard order, exactly what the seeded cross-shard draw exists to
avoid.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

    import pytest

_REPO = Path(__file__).parent.parent


def _load_baseline_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aesthetics_baseline", _REPO / "scripts" / "aesthetics_baseline.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["aesthetics_baseline"] = module
    spec.loader.exec_module(module)
    return module


def test_algorithmic_sample_is_not_biased_to_the_lowest_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # All rows valid and eligible: with the seeded oversample (2 * limit rows)
    # loaded in ascending order, a plain first-`limit` cut would select exactly
    # the lowest-index half. The seeded reshuffle must prevent that.
    baseline = _load_baseline_module()
    limit = 8
    total = 100
    loaded_ids: list[int] = []

    def fake_load_selected(
        *, paths: Sequence[Path], counts: Sequence[int], indices: Sequence[int]
    ) -> list[Any]:
        del paths, counts
        loaded_ids.extend(int(index) for index in indices)
        return [
            SimpleNamespace(structure_id=f"{index:08d}", bricks=index)
            for index in indices
        ]

    def fake_score_layout(layout: object, *, population: str, name: str) -> object:
        del layout
        return baseline.Score(
            population=population,
            name=name,
            bricks=50,
            perpendicularity=0.0,
            symmetry=0.0,
            speckle=0.0,
            profile=0.0,
        )

    monkeypatch.setattr(baseline.s2b, "shard_paths", lambda root: [root])
    monkeypatch.setattr(baseline.s2b, "shard_row_counts", lambda paths: [total])
    monkeypatch.setattr(baseline.s2b, "load_selected", fake_load_selected)
    monkeypatch.setattr(baseline.s2b, "layout_from_bricks", lambda bricks: bricks)
    monkeypatch.setattr(baseline, "score_layout", fake_score_layout)

    scores = baseline.algorithmic_scores(
        Path("unused.parquet"), limit=limit, min_bricks=1, seed=0
    )
    chosen = [int(score.name) for score in scores]

    assert len(loaded_ids) == 2 * limit
    assert loaded_ids == sorted(loaded_ids)
    assert len(chosen) == limit
    assert set(chosen) <= set(loaded_ids)
    # The old truncation kept exactly the lowest-index half, in order.
    assert chosen != loaded_ids[:limit]

    rerun = baseline.algorithmic_scores(
        Path("unused.parquet"), limit=limit, min_bricks=1, seed=0
    )
    assert [score.name for score in rerun] == [score.name for score in scores]


def test_promotion_gate_aggregates_strategy_candidates_by_source_model():
    baseline = _load_baseline_module()

    def score(population: str, name: str, symmetry: float) -> object:
        return baseline.Score(
            population=population,
            name=name,
            bricks=50,
            perpendicularity=symmetry,
            symmetry=symmetry,
            speckle=symmetry,
            profile=symmetry,
        )

    scores = [
        score("human", "human-1", 0.1),
        score("human", "human-2", 0.2),
        score("ours", "source-a/beauty", 0.3),
        score("ours", "source-a/bond", 0.5),
        score("ours", "source-b/beauty", 0.6),
        score("ours", "source-b/bond", 0.8),
        score("algorithmic", "algorithmic-1", 0.9),
        score("algorithmic", "algorithmic-2", 1.0),
    ]

    values = baseline._gate_values(  # noqa: SLF001
        scores, population="ours", field="symmetry"
    )
    assert np.array_equal(values, np.array([0.4, 0.7]))
    gate = baseline.promotion_gates(scores)["symmetry"]
    medians = gate["medians"]
    assert isinstance(medians, dict)
    assert medians["ours"] == 0.55
