"""Compare our beauty scalar across human-authored, algorithmic, and our layouts.

ROADMAP, "Placement quality", carries an open item: *validate the beauty scalar
against human judgement*. Min-style symmetry/balance and SM-GA/Bao
perpendicularity are live objective terms in
:mod:`legolization.placement.aesthetics` and the ``beauty`` strategy optimizes
them directly, but neither has ever been checked against a build a person
actually designed. The missing ingredient was a corpus of human-authored
assemblies; the OMR crawl supplies one.

Three populations, scored by the **same two existing pure functions**
(``perpendicularity_error``, ``symmetry_error``) so nothing new is being
measured - only the same metric on different authors:

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

from legolization.catalog import default_catalog
from legolization.datasets import stabletext2brick as s2b
from legolization.ldraw_in import import_occurrences
from legolization.placement.aesthetics import perpendicularity_error, symmetry_error

if TYPE_CHECKING:
    from collections.abc import Sequence

    from legolization.layout import Layout

_REPO = Path(__file__).parent.parent
_DEFAULT_OMR = _REPO / "datasets" / "omr" / "ldraw"
_DEFAULT_S2B = _REPO / "datasets" / "stabletext2brick"
_DEFAULT_BASELINE = _REPO / "eval" / "baselines" / "scorecard.json"
_DEFAULT_OUT = _REPO / "eval" / "datasets" / "aesthetics"


@dataclass(frozen=True, slots=True, kw_only=True)
class Score:
    """One layout's beauty terms. Both are errors: lower is 'prettier'."""

    population: str
    name: str
    bricks: int
    perpendicularity: float
    symmetry: float


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
    )


def human_scores(models: Path, *, limit: int, min_bricks: int) -> list[Score]:
    """Score the importable skeleton of each OMR model.

    ``limit`` caps *eligible* scores, not attempted files: unreadable models
    and skeletons below ``min_bricks`` do not consume the cap, so ``--sample``
    means the same thing for every population.
    """
    catalog = default_catalog()
    paths = sorted(models.glob("*.mpd")) + sorted(models.glob("*.ldr"))
    scores: list[Score] = []
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
        if (
            score := score_layout(imported.layout, population="human", name=path.name)
        ) and (score.bricks >= min_bricks):
            scores.append(score)
            if limit and len(scores) >= limit:
                break
    return scores


def algorithmic_scores(
    root: Path, *, limit: int, min_bricks: int, seed: int
) -> list[Score]:
    """Score a seeded sample of StableText2Brick structures across all shards.

    Reading only the first shard would make the reported distribution a fact
    about parquet shard order rather than about the release, so rows are drawn
    deterministically over every shard with the sweep's sampling convention.
    The draw is oversampled because parse failures and structures below
    ``min_bricks`` are dropped after the fact; ``limit`` caps eligible scores.
    """
    if not (paths := s2b.shard_paths(root)):
        return []
    counts = s2b.shard_row_counts(paths)
    indices = s2b.sample_indices(counts, sample=2 * limit if limit else 0, seed=seed)
    scores: list[Score] = []
    for item in s2b.load_selected(paths, counts, indices):
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
            if (perp := metrics.get("perpendicularity")) is None:
                continue
            if (symmetry := metrics.get("symmetry")) is None:
                continue
            scores.append(
                Score(
                    population="ours",
                    name=f"{model['model']}/{candidate['strategy']}",
                    bricks=int(bricks),
                    perpendicularity=float(perp),
                    symmetry=float(symmetry),
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


def to_markdown(summary: dict[str, dict[str, dict[str, float]]]) -> str:
    """Render the comparison."""
    lines = [
        "# Beauty scalar: human vs algorithmic vs ours",
        "",
        "Both terms are **errors**, normalized to [0, 1], lower is better",
        "(`placement/aesthetics.py`). `perpendicularity` is the fraction of",
        "rectangular support pairs whose long axes are parallel - SM-GA's n_p,",
        "where crossing bricks bond layers like plywood. `symmetry` is Min's",
        "balance term g_a, the mean unbalanced-brick fraction per layer.",
        "",
        "**Read the caveat before drawing conclusions.** No OMR model imports",
        "completely - our catalog covers ~14% of real part occurrences - so the",
        "`human` rows score each set's *basic-brick skeleton*, not the finished",
        "model. That skeleton is the vocabulary our generator works in, so the",
        "comparison is meaningful, but it is not a comparison of finished sets.",
        "",
    ]
    for field in ("perpendicularity", "symmetry"):
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--omr", type=Path, default=_DEFAULT_OMR)
    parser.add_argument("--stabletext2brick", type=Path, default=_DEFAULT_S2B)
    parser.add_argument("--baseline", type=Path, default=_DEFAULT_BASELINE)
    parser.add_argument(
        "--sample",
        type=int,
        default=200,
        help="cap of eligible layouts per population (0 = uncapped)",
    )
    parser.add_argument(
        "--seed",
        type=int,
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
        for field in ("perpendicularity", "symmetry")
    }
    summary = {
        field: {name: row for name, row in populations.items() if row}
        for field, populations in summary.items()
    }

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
                "scores": [asdict(score) for score in scores],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown = to_markdown(summary)
    (out / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
