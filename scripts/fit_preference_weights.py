"""Fit beauty-weight recommendations from the judged preference log.

The pipeline is Dev's methodology (arXiv:2505.12373) on our own data: the
forced-choice pairs in ``references/aesthetic-preferences/pairs.jsonl``
become Bradley-Terry latent aesthetic scores per model, and those scores are
regressed onto the layout-pure beauty terms
(:mod:`legolization.placement.aesthetics`), computed directly from the
judged model files so any judged model participates — the grid-conditioned
terms (cost, colour) need a voxel target the log does not carry. A term
whose (standardized) coefficient is reliably *negative* predicts prettier
models as its error falls — exactly what an objective weight claims — so the
recommendation scales the negative coefficients into weights, anchored so
the largest gets the current ``aesthetics`` default (0.5) and stability's
4.0 stays dominant.

Everything here is **reported, never applied**: the output is a
recommendation table (`eval/datasets/aesthetic-preferences/<stamp>/`), and
changing `ObjectiveWeights` defaults stays a reviewed, human decision. Below
15 distinct models or ``5 x n_terms`` pairs the whole report is stamped
advisory — with a handful of judgements the bootstrap will happily produce
confident-looking noise.

Verdict selection: for each pair id, ``human`` rows beat ``claude`` rows and
later rows beat earlier ones; ``low``-confidence rows weigh half. Ties add
half a win to both sides. Models are identified by content hash; a file
whose hash no longer matches its log row is skipped with a warning rather
than silently scored as something it is not.

Usage::

    uv run python scripts/fit_preference_weights.py [--log PATH]
        [--seed 0] [--bootstrap 1000] [--out DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from ldraw import load_model

from legolization.catalog import default_catalog
from legolization.ldraw_in import import_occurrences
from legolization.model_render import resolve_model
from legolization.placement.aesthetics import (
    colour_speckle_error,
    perpendicularity_error,
    profile_roughness,
    symmetry_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from legolization.layout import Layout

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_LOG = _REPO / "references" / "aesthetic-preferences" / "pairs.jsonl"
_DEFAULT_OUT = _REPO / "eval" / "datasets" / "aesthetic-preferences"

_TERM_FUNCTIONS: dict[str, Callable[[Layout], float]] = {
    "perpendicularity": perpendicularity_error,
    "symmetry": symmetry_error,
    "speckle": colour_speckle_error,
    "profile": profile_roughness,
}
_TERMS = tuple(_TERM_FUNCTIONS)
_W_REF = 0.5  # the current aesthetics default; stability (4.0) stays dominant
_MIN_MODELS = 15
_MIN_PAIRS_PER_TERM = 5
_SIGN_CONSISTENCY_FLOOR = 0.9
_SMOOTHING = 0.1  # pseudo-wins per compared pair; keeps the BT MLE finite


@dataclass(frozen=True, slots=True, kw_only=True)
class Pair:
    """One effective verdict after row selection."""

    model_a: str
    model_b: str
    winner: str
    weight: float


@dataclass(frozen=True, slots=True, kw_only=True)
class BtFit:
    """Bradley-Terry latent scores over the comparison graph's largest part."""

    models: tuple[str, ...]
    scores: np.ndarray
    iterations: int
    converged: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightReport:
    """The regression, its reliability, and the resulting recommendation."""

    terms: tuple[str, ...]
    beta: np.ndarray
    r_squared: float
    sign_consistency: dict[str, float]
    n_models: int
    n_pairs: int
    recommended: dict[str, float]
    caveats: tuple[str, ...]


def effective_pairs(rows: Sequence[Mapping[str, object]]) -> list[Pair]:
    """Reduce log rows to one weighted verdict per pair id."""
    latest: dict[str, Mapping[str, object]] = {}
    for row in rows:  # file order; later rows supersede within a judge class
        pair_id = str(row["id"])
        held = latest.get(pair_id)
        if held is None or row["judge"] == "human" or held["judge"] == "claude":
            latest[pair_id] = row
    return [
        Pair(
            model_a=str(row["sha256_a"]),
            model_b=str(row["sha256_b"]),
            winner=str(row["winner"]),
            weight=1.0 if row["confidence"] == "high" else 0.5,
        )
        for row in latest.values()
    ]


def _win_matrix(
    pairs: Sequence[Pair],
) -> tuple[tuple[str, ...], np.ndarray]:
    models = tuple(
        sorted({model for pair in pairs for model in (pair.model_a, pair.model_b)})
    )
    index = {model: position for position, model in enumerate(models)}
    wins = np.zeros((len(models), len(models)))
    for pair in pairs:
        a, b = index[pair.model_a], index[pair.model_b]
        if pair.winner == "a":
            wins[a, b] += pair.weight
        elif pair.winner == "b":
            wins[b, a] += pair.weight
        else:  # a tie is half a win each way
            wins[a, b] += pair.weight / 2
            wins[b, a] += pair.weight / 2
    return models, wins


def _largest_component(models: tuple[str, ...], wins: np.ndarray) -> list[int]:
    adjacency = (wins + wins.T) > 0
    unseen = set(range(len(models)))
    best: list[int] = []
    while unseen:
        frontier = [unseen.pop()]
        component = list(frontier)
        while frontier:
            node = frontier.pop()
            linked = [other for other in tuple(unseen) if adjacency[node, other]]
            for other in linked:
                unseen.remove(other)
            component.extend(linked)
            frontier.extend(linked)
        if len(component) > len(best):
            best = component
    return sorted(best)


def fit_bradley_terry(
    pairs: Sequence[Pair], *, tol: float = 1e-10, max_iter: int = 10_000
) -> BtFit:
    """Zermelo/Hunter minorization-maximization, numpy only, deterministic.

    Every compared pair gets ``_SMOOTHING`` pseudo-wins in both directions:
    the unsmoothed MLE does not exist when some model is undefeated (its
    strength diverges), and small logs make undefeated models the norm.
    """
    all_models, wins = _win_matrix(pairs)
    keep = _largest_component(all_models, wins)
    if len(keep) < len(all_models):
        print(
            f"comparison graph is disconnected; fitting the largest of its "
            f"components ({len(keep)} of {len(all_models)} models)",
            file=sys.stderr,
        )
    wins = wins[np.ix_(keep, keep)]
    models = tuple(all_models[position] for position in keep)
    count = len(models)
    compared = (wins + wins.T) > 0
    wins = wins + _SMOOTHING * compared
    totals = wins + wins.T
    strengths = np.ones(count)
    converged = False
    sweeps = 0
    while sweeps < max_iter and not converged:
        sweeps += 1
        shared = totals / np.add.outer(strengths, strengths)
        np.fill_diagonal(shared, 0.0)
        updated = wins.sum(axis=1) / np.maximum(shared.sum(axis=1), 1e-300)
        updated /= np.exp(np.mean(np.log(np.maximum(updated, 1e-300))))
        converged = bool(np.max(np.abs(updated - strengths)) < tol)
        strengths = updated
    return BtFit(
        models=models,
        scores=np.log(np.maximum(strengths, 1e-300)),
        iterations=sweeps,
        converged=converged,
    )


def judged_model_terms(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, float]]:
    """Compute the beauty terms for every judged model, keyed by content hash.

    Each unique hash in the log is resolved through its recorded path (a
    model file or a bundle directory), re-hashed to confirm identity, then
    imported and scored with the same pure functions the objective uses.
    """
    catalog = default_catalog()
    paths: dict[str, Path] = {}
    for row in rows:
        for side in ("a", "b"):
            paths.setdefault(
                str(row[f"sha256_{side}"]), Path(str(row[f"model_{side}"]))
            )
    by_hash: dict[str, dict[str, float]] = {}
    for digest, recorded in sorted(paths.items()):
        try:
            model = resolve_model(recorded)
            actual = hashlib.sha256(model.read_bytes()).hexdigest()
        except (OSError, ValueError) as error:
            print(f"skipping {recorded}: {error}", file=sys.stderr)
            continue
        if actual != digest:
            print(
                f"skipping {recorded}: content changed since it was judged",
                file=sys.stderr,
            )
            continue
        try:
            loaded = load_model(model)
            analysis = loaded.analyze(None)
            if loaded.model is None or analysis is None:
                continue
            imported = import_occurrences(
                analysis.occurrences,
                catalog=catalog,
                ground=False,
                default_model=model.name,
            )
        except Exception as error:  # noqa: BLE001 - unreadable model, not a crash
            print(f"skipping {recorded}: {error}", file=sys.stderr)
            continue
        by_hash[digest] = {
            term: function(imported.layout)
            for term, function in _TERM_FUNCTIONS.items()
        }
    return by_hash


def regress_terms(
    fit: BtFit,
    terms_by_model: Mapping[str, Mapping[str, float]],
    *,
    pairs: Sequence[Pair],
    rng: np.random.Generator,
    bootstrap: int = 1_000,
) -> WeightReport:
    """OLS of latent scores on z-scored terms, with bootstrap sign checks."""
    scored = [model for model in fit.models if model in terms_by_model]
    matrix = np.array(
        [[terms_by_model[model][term] for term in _TERMS] for model in scored]
    )
    scores = np.array([fit.scores[fit.models.index(model)] for model in scored])
    spread = matrix.std(axis=0)
    keep = [position for position, sigma in enumerate(spread) if sigma > 0]
    terms = tuple(_TERMS[position] for position in keep)
    zscored = (matrix[:, keep] - matrix[:, keep].mean(axis=0)) / spread[keep]
    design = np.column_stack([np.ones(len(scored)), zscored])
    beta_full, *_ = np.linalg.lstsq(design, scores, rcond=None)
    beta = beta_full[1:]
    predicted = design @ beta_full
    residual = scores - predicted
    total = scores - scores.mean()
    r_squared = (
        1.0 - float(residual @ residual) / float(total @ total)
        if float(total @ total) > 0
        else 0.0
    )

    signs = np.zeros((bootstrap, len(terms)))
    for draw in range(bootstrap):
        chosen = rng.integers(len(scored), size=len(scored))
        sample_design = design[chosen]
        sample_scores = scores[chosen]
        try:
            sample_beta, *_ = np.linalg.lstsq(sample_design, sample_scores, rcond=None)
        except np.linalg.LinAlgError:
            continue
        signs[draw] = np.sign(sample_beta[1:])
    consistency = {
        term: float(np.mean(signs[:, position] == np.sign(beta[position])))
        if beta[position] != 0
        else 0.0
        for position, term in enumerate(terms)
    }

    raw = np.clip(-beta, 0.0, None)
    caveats: list[str] = []
    recommended: dict[str, float] = {}
    reliable = [
        position
        for position, term in enumerate(terms)
        if consistency[term] >= _SIGN_CONSISTENCY_FLOOR and beta[position] < 0
    ]
    # Anchor on the strongest RELIABLE coefficient; scaling against a term
    # that is about to be zeroed would shrink every real recommendation.
    anchor = raw[reliable].max() if reliable else 0.0
    scale = _W_REF / anchor if anchor > 0 else 0.0
    for position, term in enumerate(terms):
        if consistency[term] < _SIGN_CONSISTENCY_FLOOR:
            recommended[term] = 0.0
            caveats.append(
                f"{term}: bootstrap sign consistency "
                f"{consistency[term]:.0%} < {_SIGN_CONSISTENCY_FLOOR:.0%}"
            )
        elif beta[position] >= 0:
            recommended[term] = 0.0
            caveats.append(
                f"{term}: coefficient points the wrong way "
                f"(higher error predicts prettier)"
            )
        else:
            recommended[term] = round(float(raw[position] * scale), 2)

    n_pairs = len(pairs)
    if len(scored) < _MIN_MODELS:
        caveats.append(f"ADVISORY ONLY: {len(scored)} scored models < {_MIN_MODELS}")
    if n_pairs < _MIN_PAIRS_PER_TERM * len(terms):
        caveats.append(
            f"ADVISORY ONLY: {n_pairs} pairs < "
            f"{_MIN_PAIRS_PER_TERM} x {len(terms)} terms"
        )
    return WeightReport(
        terms=terms,
        beta=beta,
        r_squared=r_squared,
        sign_consistency=consistency,
        n_models=len(scored),
        n_pairs=n_pairs,
        recommended=recommended,
        caveats=tuple(caveats),
    )


def to_markdown(report: WeightReport, fit: BtFit) -> str:
    """Render the recommendation table."""
    lines = [
        "# Preference-fitted weight recommendations",
        "",
        f"Bradley-Terry over {report.n_pairs} pairs, {report.n_models} scored",
        f"models (MM converged: {fit.converged}, {fit.iterations} sweeps);",
        f"OLS on z-scored terms, R^2 = {report.r_squared:.3f}. A negative",
        "coefficient means the (lower-better) term predicts prettier models.",
        "",
        "| term | beta (z) | sign consistency | recommended weight |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {term} | {report.beta[position]:+.3f} | "
        f"{report.sign_consistency[term]:.0%} | "
        f"{report.recommended[term]:.2f} |"
        for position, term in enumerate(report.terms)
    )
    lines += ["", "## Caveats", ""]
    lines += (
        [f"- {caveat}" for caveat in report.caveats] if report.caveats else ["- none"]
    )
    lines += [
        "",
        "Recommendations are inputs to a reviewed `ObjectiveWeights` change,",
        "never applied automatically; the audition gates in",
        "`scripts/aesthetics_baseline.py` and the drift harness still apply.",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--log", type=Path, default=_DEFAULT_LOG)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--bootstrap", type=int, default=1_000)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Fit, report, and write; never touch any weight default."""
    args = parse_args(argv)
    if not args.log.exists():
        print(f"no preference log at {args.log}", file=sys.stderr)
        return 1
    rows = [
        json.loads(line)
        for line in args.log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pairs = effective_pairs(rows)
    if not pairs:
        print("the preference log has no judged pairs yet", file=sys.stderr)
        return 1
    fit = fit_bradley_terry(pairs)
    report = regress_terms(
        fit,
        judged_model_terms(rows),
        pairs=pairs,
        rng=np.random.default_rng(args.seed),
        bootstrap=args.bootstrap,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(
            {
                "generated": stamp,
                "seed": args.seed,
                "bootstrap": args.bootstrap,
                "n_pairs": report.n_pairs,
                "n_models": report.n_models,
                "converged": fit.converged,
                "r_squared": report.r_squared,
                "beta": dict(zip(report.terms, report.beta.tolist(), strict=True)),
                "sign_consistency": report.sign_consistency,
                "recommended": report.recommended,
                "caveats": list(report.caveats),
                "bt_scores": dict(zip(fit.models, fit.scores.tolist(), strict=True)),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown = to_markdown(report, fit)
    (out / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
