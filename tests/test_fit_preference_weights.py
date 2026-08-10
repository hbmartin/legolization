"""Synthetic ground-truth recovery for the preference-weight fitter.

The claim under test: pairs sampled from known latent scores let the
Bradley-Terry fit recover the score ordering, and the regression recovers
the SIGN of each term's true influence. Sign is the load-bearing part - a
flipped sign would recommend weighting a term that makes output uglier.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).parent.parent


def _load_fit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fit_preference_weights", _REPO / "scripts" / "fit_preference_weights.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fit_preference_weights"] = module
    spec.loader.exec_module(module)
    return module


def _synthetic_world(
    module: ModuleType, *, models: int = 20, pairs: int = 400, seed: int = 5
) -> tuple[list, dict[str, dict[str, float]], np.ndarray]:
    """Terms drawn at random; true prettiness = -2*symmetry - 1*speckle."""
    rng = np.random.default_rng(seed)
    names = [f"m{index:02d}" for index in range(models)]
    terms = {
        name: {
            "perpendicularity": float(rng.uniform()),
            "symmetry": float(rng.uniform()),
            "speckle": float(rng.uniform()),
            "profile": float(rng.uniform()),
        }
        for name in names
    }
    truth = np.array(
        [
            -2.0 * terms[name]["symmetry"] - 1.0 * terms[name]["speckle"]
            for name in names
        ]
    )
    judged = []
    for _ in range(pairs):
        a, b = rng.choice(models, size=2, replace=False)
        margin = truth[a] - truth[b]
        p_a = 1.0 / (1.0 + np.exp(-4.0 * margin))
        winner = "a" if rng.uniform() < p_a else "b"
        judged.append(
            module.Pair(model_a=names[a], model_b=names[b], winner=winner, weight=1.0)
        )
    return judged, terms, truth


def test_bt_recovers_the_latent_ordering():
    module = _load_fit_module()
    pairs, _, truth = _synthetic_world(module)
    fit = module.fit_bradley_terry(pairs)
    assert fit.converged
    fitted = np.array([fit.scores[fit.models.index(f"m{i:02d}")] for i in range(20)])
    correlation = np.corrcoef(
        np.argsort(np.argsort(truth)), np.argsort(np.argsort(fitted))
    )[0, 1]
    assert correlation > 0.9


def test_regression_recovers_signs_and_recommends_accordingly():
    module = _load_fit_module()
    pairs, terms, _ = _synthetic_world(module)
    fit = module.fit_bradley_terry(pairs)
    report = module.regress_terms(
        fit, terms, pairs=pairs, rng=np.random.default_rng(0), bootstrap=200
    )
    position = {term: index for index, term in enumerate(report.terms)}
    # The two real drivers come back negative (lower error = prettier) and
    # weighted, symmetry strongest; the two noise terms get no weight.
    assert report.beta[position["symmetry"]] < 0
    assert report.beta[position["speckle"]] < 0
    assert report.recommended["symmetry"] == 0.5  # anchored to w_ref
    assert 0 < report.recommended["speckle"] < 0.5
    assert report.recommended["perpendicularity"] == 0.0
    assert report.recommended["profile"] == 0.0
    assert report.r_squared > 0.5


def test_fit_is_deterministic_given_seed():
    module = _load_fit_module()
    pairs, terms, _ = _synthetic_world(module)
    first = module.regress_terms(
        module.fit_bradley_terry(pairs),
        terms,
        pairs=pairs,
        rng=np.random.default_rng(7),
        bootstrap=100,
    )
    second = module.regress_terms(
        module.fit_bradley_terry(pairs),
        terms,
        pairs=pairs,
        rng=np.random.default_rng(7),
        bootstrap=100,
    )
    assert np.array_equal(first.beta, second.beta)
    assert first.recommended == second.recommended


def test_small_n_marks_the_report_advisory():
    module = _load_fit_module()
    pairs, terms, _ = _synthetic_world(module, models=6, pairs=10)
    report = module.regress_terms(
        module.fit_bradley_terry(pairs),
        terms,
        pairs=pairs,
        rng=np.random.default_rng(0),
        bootstrap=50,
    )
    assert any("ADVISORY" in caveat for caveat in report.caveats)


def test_ties_and_disconnection_are_survivable():
    module = _load_fit_module()
    pairs = [
        module.Pair(model_a="x1", model_b="x2", winner="tie", weight=1.0),
        module.Pair(model_a="x1", model_b="x2", winner="a", weight=0.5),
        # A second component, larger, wins the component selection.
        module.Pair(model_a="y1", model_b="y2", winner="a", weight=1.0),
        module.Pair(model_a="y2", model_b="y3", winner="b", weight=1.0),
        module.Pair(model_a="y1", model_b="y3", winner="a", weight=1.0),
    ]
    fit = module.fit_bradley_terry(pairs)
    assert set(fit.models) == {"y1", "y2", "y3"}
    assert fit.converged


def test_effective_pairs_prefers_human_and_weights_low_confidence():
    module = _load_fit_module()
    rows = [
        {
            "id": "p1",
            "sha256_a": "A",
            "sha256_b": "B",
            "winner": "a",
            "judge": "claude",
            "confidence": "low",
        },
        {
            "id": "p1",
            "sha256_a": "A",
            "sha256_b": "B",
            "winner": "b",
            "judge": "human",
            "confidence": "high",
        },
        {
            "id": "p2",
            "sha256_a": "A",
            "sha256_b": "C",
            "winner": "a",
            "judge": "claude",
            "confidence": "low",
        },
    ]
    pairs = {pair.model_b: pair for pair in module.effective_pairs(rows)}
    assert pairs["B"].winner == "b"  # the human superseded the escalation
    assert pairs["B"].weight == 1.0
    assert pairs["C"].weight == 0.5  # low confidence weighs half
