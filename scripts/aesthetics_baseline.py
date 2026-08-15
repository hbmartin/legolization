"""Compare our beauty scalar across human-authored, algorithmic, and our layouts.

This is the population half of the beauty-term validation programme (the
trajectory half is ``scripts/aesthetics_drift.py``; standing verdicts in
``docs/reports/aesthetics-validation.md``). Its first run measured
perpendicularity *inverted* against official sets — the finding that demoted
the term to weight 0.0 — and it remains the standing instrument: every term
change reruns it, and the audition gates below decide whether a reported-only
term may ever carry weight.

Three populations, scored by the **same pure functions the objective uses**
(``perpendicularity_error``, global-plane ``symmetry_error``, and the audition
terms ``colour_speckle_error`` and ``profile_roughness``) so nothing new is
being measured - only the same metrics on different authors. The report also
carries the audition **promotion gates**: an audition term may only gain a
non-zero default weight when the population medians order
``human < ours < algorithmic`` strictly AND a one-sided Mann-Whitney U test
(human < ours) clears p < 0.01. Our candidates are first averaged per source
model, so alternate strategies for one source are not treated as independent
observations - see ``docs/reports/aesthetics-validation.md``:

===============  ==========================================================
``human``        LDraw OMR - official LEGO sets, designed by LEGO
``algorithmic``  StableText2Brick - the delete-and-rebuild contrast class
``ours``         our own generated layouts, read from the committed baseline
                 scorecard (which already records both terms per candidate),
                 so no placement is re-run
===============  ==========================================================

**Methodological limit, stated up front.** No OMR model imports completely -
our catalog covers ~14% of real part occurrences (see
``scripts/ldraw_coverage.py``), so a human layout here is its *basic-brick
skeleton*, not the whole set. That subset is exactly the vocabulary our
generator uses, so the comparison is meaningful, but it is a comparison of
how the basic bricks are arranged, not of the finished models. Models whose
imported skeleton falls below ``--min-bricks`` are dropped rather than scored
on noise.

Usage::

    uv run python scripts/aesthetics_baseline.py [--omr DIR] [--sample N]
        [--baseline PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from ldraw import load_model
from scipy.stats import mannwhitneyu

from legolization.catalog import default_catalog
from legolization.datasets import stabletext2brick as s2b
from legolization.ldraw_in import import_occurrences
from legolization.placement.aesthetics import (
    colour_speckle_error,
    perpendicularity_error,
    profile_roughness,
    symmetry_error,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from legolization.layout import Layout

_REPO = Path(__file__).parent.parent
_DEFAULT_OMR = _REPO / "datasets" / "omr" / "ldraw"
_DEFAULT_S2B = _REPO / "datasets" / "stabletext2brick"
_DEFAULT_BASELINE = _REPO / "eval" / "baselines" / "scorecard.json"
_DEFAULT_OUT = _REPO / "eval" / "datasets" / "aesthetics"

# Every term the report compares, in table order. All are errors in [0, 1].
_FIELDS = ("perpendicularity", "symmetry", "speckle", "profile")
_GATE_P_THRESHOLD = 0.01


@dataclass(frozen=True, slots=True, kw_only=True)
class Score:
    """One layout's beauty terms. All are errors: lower is 'prettier'."""

    population: str
    name: str
    bricks: int
    perpendicularity: float
    symmetry: float
    speckle: float
    profile: float


def score_layout(layout: Layout, *, population: str, name: str) -> Score | None:
    """Score one layout, or ``None`` when it is too small to mean anything."""
    if not (bricks := sum(1 for _ in layout)):
        return None
    return Score(
        population=population,
        name=name,
        bricks=bricks,
        perpendicularity=perpendicularity_error(layout),
        symmetry=symmetry_error(layout),
        speckle=colour_speckle_error(layout),
        profile=profile_roughness(layout),
    )


def human_layouts(
    models: Path, *, limit: int, min_bricks: int
) -> Iterator[tuple[str, Layout]]:
    """Yield each OMR model's importable skeleton, largest-vocabulary first.

    ``limit`` caps *eligible* layouts, not attempted files: unreadable models
    and skeletons below ``min_bricks`` do not consume the cap, so ``--sample``
    means the same thing for every population. Shared with
    ``scripts/aesthetics_drift.py``, which perturbs these same skeletons.
    """
    catalog = default_catalog()
    paths = sorted(models.glob("*.mpd")) + sorted(models.glob("*.ldr"))
    yielded = 0
    for path in paths:
        try:
            loaded = load_model(path)
            analysis = loaded.analyze(None)
            if loaded.model is None or analysis is None:
                continue
            imported = import_occurrences(
                analysis.occurrences,
                catalog=catalog,
                ground=False,
                default_model=path.name,
            )
        except Exception:  # noqa: BLE001, S112 - a model we cannot read is not a failure here
            continue
        if sum(1 for _ in imported.layout) < min_bricks:
            continue
        yield path.name, imported.layout
        yielded += 1
        if limit and yielded >= limit:
            return


def human_scores(models: Path, *, limit: int, min_bricks: int) -> list[Score]:
    """Score the importable skeleton of each OMR model."""
    return [
        score
        for name, layout in human_layouts(models, limit=limit, min_bricks=min_bricks)
        if (score := score_layout(layout, population="human", name=name)) is not None
    ]


def algorithmic_scores(
    root: Path, *, limit: int, min_bricks: int, seed: int
) -> list[Score]:
    """Score a seeded sample of StableText2Brick structures across all shards.

    Reading only the first shard would make the reported distribution a fact
    about parquet shard order rather than about the release, so rows are drawn
    deterministically over every shard with the sweep's sampling convention.
    The draw is oversampled because parse failures and structures below
    ``min_bricks`` are dropped after the fact; ``limit`` caps eligible scores.
    ``load_selected`` returns rows in ascending global-index order, so the
    loaded rows are re-shuffled with the same seed before capping - otherwise
    the cap would keep the lowest indices and re-introduce shard-order bias.
    """
    if not (paths := s2b.shard_paths(root)):
        return []
    counts = s2b.shard_row_counts(paths)
    indices = s2b.sample_indices(counts, sample=2 * limit if limit else 0, seed=seed)
    loaded = s2b.load_selected(paths=paths, counts=counts, indices=indices)
    order = np.random.default_rng(seed).permutation(len(loaded))
    scores: list[Score] = []
    for item in (loaded[position] for position in order):
        if isinstance(item, str):
            continue
        try:
            layout = s2b.layout_from_bricks(item.bricks)
        except (KeyError, ValueError):
            continue
        score = score_layout(
            layout, population="algorithmic", name=item.structure_id[:8]
        )
        if score and score.bricks >= min_bricks:
            scores.append(score)
        if limit and len(scores) >= limit:
            break
    return scores


def our_scores(baseline: Path, *, limit: int, min_bricks: int) -> list[Score]:
    """Read our own layouts' terms out of the committed baseline scorecard.

    The scorecard already records ``perpendicularity`` and ``symmetry`` per
    candidate, so this costs nothing and - more importantly - scores exactly the
    layouts the project's own regression tracks, rather than fresh ones.
    """
    if not baseline.exists():
        return []
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    scores: list[Score] = []
    for model in payload.get("models", []):
        for candidate in model.get("candidates", []):
            metrics = candidate.get("metrics") or {}
            if not metrics.get("buildable"):
                continue
            if (bricks := metrics.get("brick_count", 0)) < min_bricks:
                continue
            extracted: dict[str, float] = {}
            for field in _FIELDS:
                if isinstance(value := metrics.get(field), int | float):
                    extracted[field] = float(value)
            if len(extracted) != len(_FIELDS):
                continue  # a pre-audition scorecard; regenerate the baseline
            scores.append(
                Score(
                    population="ours",
                    name=f"{model['model']}/{candidate['strategy']}",
                    bricks=int(bricks),
                    perpendicularity=extracted["perpendicularity"],
                    symmetry=extracted["symmetry"],
                    speckle=extracted["speckle"],
                    profile=extracted["profile"],
                )
            )
            if limit and len(scores) >= limit:
                return scores
    return scores


def distribution(scores: Sequence[Score], *, field: str) -> dict[str, float]:
    """Summarize one term's distribution over one population."""
    values = np.array([getattr(score, field) for score in scores])
    if not values.size:
        return {}
    return {
        "n": int(values.size),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "p25": float(np.percentile(values, 25)),
        "p75": float(np.percentile(values, 75)),
        "min": float(values.min()),
        "max": float(values.max()),
    }


def _gate_values(scores: Sequence[Score], *, population: str, field: str) -> np.ndarray:
    """Return independent values for one promotion-gate population.

    A single source model produces several ``ours`` candidates (one per
    strategy), so those candidates are correlated alternatives rather than
    independent observations. Average them before the Mann-Whitney test;
    human and algorithmic rows each originate from one source model already.
    """
    members = [score for score in scores if score.population == population]
    if population != "ours":
        return np.array([getattr(score, field) for score in members])
    by_model: dict[str, list[float]] = {}
    for score in members:
        model, _, _ = score.name.rpartition("/")
        by_model.setdefault(model or score.name, []).append(getattr(score, field))
    return np.array([float(np.mean(values)) for _, values in sorted(by_model.items())])


def promotion_gates(scores: Sequence[Score]) -> dict[str, dict[str, object]]:
    """Compute the mechanical promotion gate per term.

    A term may gain a non-zero default weight only when the medians order
    ``human < ours < algorithmic`` strictly and a one-sided Mann-Whitney U
    test (human < ours) clears ``p < 0.01``. The drift harness
    (``scripts/aesthetics_drift.py``) is the third, separate condition.
    """
    gates: dict[str, dict[str, object]] = {}
    for field in _FIELDS:
        values = {
            population: _gate_values(scores, population=population, field=field)
            for population in ("human", "algorithmic", "ours")
        }
        if any(not array.size for array in values.values()):
            gates[field] = {"computed": False, "reason": "a population is empty"}
            continue
        medians = {
            population: float(np.median(array)) for population, array in values.items()
        }
        ordering_ok = medians["human"] < medians["ours"] < medians["algorithmic"]
        p_value = float(
            mannwhitneyu(values["human"], values["ours"], alternative="less").pvalue
        )
        gates[field] = {
            "computed": True,
            "medians": medians,
            "ordering_ok": ordering_ok,
            "p_value": p_value,
            "passed": ordering_ok and p_value < _GATE_P_THRESHOLD,
        }
    return gates


def to_markdown(
    summary: dict[str, dict[str, dict[str, float]]],
    gates: dict[str, dict[str, object]],
) -> str:
    """Render the comparison."""
    lines = [
        "# Beauty scalar: human vs algorithmic vs ours",
        "",
        "All terms are **errors**, normalized to [0, 1], lower is better",
        "(`placement/aesthetics.py`). `perpendicularity` is the fraction of",
        "rectangular support pairs whose long axes are parallel - SM-GA's n_p,",
        "where crossing bricks bond layers like plywood; it is reported but",
        "unweighted by default. `symmetry` is the global-plane mirror error:",
        "the unbalanced-brick fraction under one whole-model mirror plane.",
        "`speckle` (exposed-surface colour junctions that change colour) and",
        "`profile` (mean Jaccard distance between consecutive layer",
        "footprints) are audition terms, weightless until they pass the gates",
        "below.",
        "",
        "**Read the caveat before drawing conclusions.** No OMR model imports",
        "completely - our catalog covers ~14% of real part occurrences - so the",
        "`human` rows score each set's *basic-brick skeleton*, not the finished",
        "model. That skeleton is the vocabulary our generator works in, so the",
        "comparison is meaningful, but it is not a comparison of finished sets.",
        "",
    ]
    for field in _FIELDS:
        lines += [
            f"## {field}",
            "",
            "| population | n | mean | median | p25 | p75 | min | max |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for population in ("human", "algorithmic", "ours"):
            if not (row := summary.get(field, {}).get(population)):
                continue
            lines.append(
                f"| {population} | {row['n']} | {row['mean']:.4f} | "
                f"{row['median']:.4f} | {row['p25']:.4f} | {row['p75']:.4f} | "
                f"{row['min']:.4f} | {row['max']:.4f} |"
            )
        lines.append("")
    lines += [
        "## Promotion gates",
        "",
        "A term may carry weight only when medians order",
        "`human < ours < algorithmic` strictly AND the one-sided Mann-Whitney",
        f"U (human < ours) clears p < {_GATE_P_THRESHOLD}. Ours candidates are",
        "averaged per source model before this test; the drift harness",
        "is the separate third condition.",
        "",
        "| term | ordering ok | p (human < ours) | gate |",
        "| --- | --- | ---: | --- |",
    ]
    for field in _FIELDS:
        gate = gates.get(field, {})
        if not gate.get("computed"):
            lines.append(f"| {field} | - | - | not computed |")
            continue
        lines.append(
            f"| {field} | {gate['ordering_ok']} | {gate['p_value']:.2e} | "
            f"{'PASS' if gate['passed'] else 'fail'} |"
        )
    lines += [
        "",
        "## How to read a difference",
        "",
        "If our layouts score *lower* (better) than the human population on a",
        "term, that term is not capturing what makes a human build look right -",
        "we are already beating people at it while our output still looks",
        "machine-made. That is a finding about `ObjectiveWeights`, not a win.",
        "",
        "If the human population scores lower, the term is pointing the right",
        "way and the gap is the headroom worth optimizing into.",
        "",
        "Per the self-evaluation playbook's trust order (physics > eyes >",
        "objective), an eyes-vs-objective disagreement here is itself the",
        "result and is worth reporting rather than resolving by fiat.",
        "",
    ]
    return "\n".join(lines)


def _non_negative(text: str) -> int:
    """Parse an argparse value that must be an integer >= 0."""
    value = int(text)
    if value < 0:
        msg = f"{value} is negative; must be >= 0"
        raise argparse.ArgumentTypeError(msg)
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--omr", type=Path, default=_DEFAULT_OMR)
    parser.add_argument("--stabletext2brick", type=Path, default=_DEFAULT_S2B)
    parser.add_argument("--baseline", type=Path, default=_DEFAULT_BASELINE)
    parser.add_argument(
        "--sample",
        type=_non_negative,
        default=200,
        help="cap of eligible layouts per population (0 = uncapped)",
    )
    parser.add_argument(
        "--seed",
        type=_non_negative,
        default=0,
        help="seed for the StableText2Brick cross-shard sample",
    )
    parser.add_argument(
        "--min-bricks",
        type=int,
        default=20,
        help="drop layouts smaller than this; both terms are noise on tiny ones",
    )
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Score all three populations and write the comparison."""
    args = parse_args(argv)
    scores = [
        *human_scores(args.omr, limit=args.sample, min_bricks=args.min_bricks),
        *algorithmic_scores(
            args.stabletext2brick,
            limit=args.sample,
            min_bricks=args.min_bricks,
            seed=args.seed,
        ),
        *our_scores(args.baseline, limit=args.sample, min_bricks=args.min_bricks),
    ]
    if not scores:
        print("no layouts scored; check --omr / --baseline paths", file=sys.stderr)
        return 1

    summary = {
        field: {
            population: distribution(
                [score for score in scores if score.population == population],
                field=field,
            )
            for population in ("human", "algorithmic", "ours")
        }
        for field in _FIELDS
    }
    summary = {
        field: {name: row for name, row in populations.items() if row}
        for field, populations in summary.items()
    }
    gates = promotion_gates(scores)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(
            {
                "generated": stamp,
                "min_bricks": args.min_bricks,
                "sample": args.sample,
                "seed": args.seed,
                "summary": summary,
                "gates": gates,
                "scores": [asdict(score) for score in scores],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown = to_markdown(summary, gates)
    (out / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
