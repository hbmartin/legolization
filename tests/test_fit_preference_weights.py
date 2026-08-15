"""Synthetic ground-truth recovery for the preference-weight fitter.

The claim under test: pairs sampled from known latent scores let the
Bradley-Terry fit recover the score ordering, and the regression recovers
the SIGN of each term's true influence. Sign is the load-bearing part - a
flipped sign would recommend weighting a term that makes output uglier.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
import pytest

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).parent.parent


class _WeightedPair(Protocol):
    weight: float


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


def test_bt_recovers_the_latent_ordering() -> None:
    module = _load_fit_module()
    pairs, _, truth = _synthetic_world(module)
    fit = module.fit_bradley_terry(pairs)
    assert fit.converged
    fitted = np.array([fit.scores[fit.models.index(f"m{i:02d}")] for i in range(20)])
    correlation = np.corrcoef(
        np.argsort(np.argsort(truth)), np.argsort(np.argsort(fitted))
    )[0, 1]
    assert correlation > 0.9


def test_regression_recovers_signs_and_recommends_accordingly() -> None:
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


def test_fit_is_deterministic_given_seed() -> None:
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


def test_small_n_marks_the_report_advisory() -> None:
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


def test_rank_deficient_design_suppresses_recommendations() -> None:
    module = _load_fit_module()
    pairs = [
        module.Pair(model_a="a", model_b="b", winner="a", weight=1.0),
        module.Pair(model_a="b", model_b="c", winner="a", weight=1.0),
        module.Pair(model_a="a", model_b="c", winner="a", weight=1.0),
    ]
    terms = {
        name: {
            "perpendicularity": value,
            "symmetry": 2 * value,
            "speckle": 3 * value,
            "profile": 4 * value,
        }
        for name, value in (("a", 0.0), ("b", 1.0), ("c", 2.0))
    }
    fit = module.fit_bradley_terry(pairs)
    report = module.regress_terms(
        fit,
        terms,
        pairs=pairs,
        rng=np.random.default_rng(0),
        bootstrap=10,
    )

    assert set(report.recommended.values()) == {0.0}
    assert any("design rank" in caveat for caveat in report.caveats)


def test_ties_and_disconnection_are_survivable() -> None:
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


def test_regression_counts_only_pairs_from_the_fitted_component() -> None:
    module = _load_fit_module()
    pairs = [
        module.Pair(model_a="x1", model_b="x2", winner="a", weight=1.0),
        module.Pair(model_a="x1", model_b="x2", winner="b", weight=1.0),
        module.Pair(model_a="y1", model_b="y2", winner="a", weight=1.0),
        module.Pair(model_a="y2", model_b="y3", winner="b", weight=1.0),
        module.Pair(model_a="y1", model_b="y3", winner="a", weight=1.0),
    ]
    terms = {
        name: {
            "perpendicularity": float(index),
            "symmetry": float(index % 2),
            "speckle": float(index + 1),
            "profile": float(2 - index),
        }
        for index, name in enumerate(("y1", "y2", "y3"))
    }
    fit = module.fit_bradley_terry(pairs)
    report = module.regress_terms(
        fit, terms, pairs=pairs, rng=np.random.default_rng(0), bootstrap=10
    )
    assert report.n_pairs == 3


def test_bootstrap_refits_bradley_terry_for_each_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_fit_module()
    pairs, terms, _ = _synthetic_world(module)
    fit = module.fit_bradley_terry(pairs)
    original = module.fit_bradley_terry
    sampled_weights: list[tuple[float, ...]] = []

    def capture(sample: list[_WeightedPair], **kwargs: object) -> object:
        sampled_weights.append(tuple(pair.weight for pair in sample))
        return original(sample, **kwargs)

    monkeypatch.setattr(module, "fit_bradley_terry", capture)
    module.regress_terms(
        fit, terms, pairs=pairs, rng=np.random.default_rng(1), bootstrap=7
    )
    assert len(sampled_weights) == 7
    assert any(
        weights != tuple(pair.weight for pair in pairs) for weights in sampled_weights
    )


def test_stale_model_paths_return_a_controlled_cli_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_fit_module()
    log = tmp_path / "pairs.jsonl"
    log.write_text(
        f"{
            json.dumps(
                {
                    'id': 'stale',
                    'model_a': '/missing/a.ldr',
                    'model_b': '/missing/b.ldr',
                    'sha256_a': 'a',
                    'sha256_b': 'b',
                    'winner': 'a',
                    'judge': 'human',
                    'confidence': 'high',
                }
            )
        }\n",
        encoding="utf-8",
    )
    assert (
        module.main(["--log", str(log), "--bootstrap", "1", "--out", str(tmp_path)])
        == 1
    )
    assert "no judged comparison has two models" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("contents", "diagnostic"),
    [
        ('{"id":', "invalid JSON"),
        ('{"id": "incomplete"}\n', "is missing"),
    ],
)
def test_malformed_log_rows_return_a_controlled_cli_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contents: str,
    diagnostic: str,
) -> None:
    module = _load_fit_module()
    log = tmp_path / "pairs.jsonl"
    log.write_text(contents, encoding="utf-8")

    assert module.main(["--log", str(log), "--bootstrap", "1"]) == 1
    captured = capsys.readouterr().err
    assert diagnostic in captured
    assert "Traceback" not in captured


def test_effective_pairs_prefers_human_and_weights_low_confidence() -> None:
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
