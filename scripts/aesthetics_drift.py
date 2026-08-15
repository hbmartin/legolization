"""Permutation-drift validation of the beauty terms.

The methodology (adapted from the graph-generative permutation analysis in
``references/building-lego-using-deep-generative-models-of-graphs/paper.md``):
take human-authored reference layouts, apply accumulating random perturbations,
and ask whether each metric detects the monotonic drift away from the human
design. A term that cannot tell a progressively vandalized official set from
the original is not measuring what makes a build look right, whatever its
population medians say.

Four operators, one applied per step, each preserving layout validity: delete
a brick (rejected if it would disconnect the structure or empty the layout),
move a brick one cell along one axis (rejected on collision, below-ground, or
disconnection), recolour a brick from the layout's own palette, and swap two
bricks' colours. Rejected draws are retried up to ``--max-tries`` before the
step becomes a no-op.

Each model drifts for ``min(--steps, 2 x brick count)`` operations: beyond a
couple of operations per brick the layout is fully scrambled and every metric
just samples noise around its scrambled equilibrium, which measures nothing
about detection (verified on the first run of this harness: a 48-brick model
saturates by ~90 operations and the remaining 200 checkpoints erased the
correlation).

Per (model, seed, term) the statistic is Spearman's rho between checkpoint
index and term value; a constant series contributes rho = 0 (no detection).
A series whose starting value already exceeds 0.9 is excluded from that
term's aggregate and counted as saturated - a reference with no headroom
cannot exhibit detection by definition (the OMR locomotives start at 0.99
symmetry error and simply stay there). A term PASSES when
``mean(rho) >= 0.6`` and ``fraction(rho > 0) >= 0.8`` over the informative
series. The expected picture that closes the roadmap item: global-plane
``symmetry`` passes and beats the superseded per-layer ``layer_symmetry``;
``perpendicularity`` stays flat or drifts the wrong way, independently
confirming the population baseline's inversion finding
(``docs/reports/aesthetics-validation.md``).

Usage::

    uv run python scripts/aesthetics_drift.py [--omr DIR] [--sample N]
        [--steps N] [--every N] [--seeds N,N,...] [--out DIR]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from scipy.stats import spearmanr

from legolization.graph import ConnectionGraph
from legolization.placement.aesthetics import (
    colour_speckle_error,
    layer_symmetry_error,
    perpendicularity_error,
    profile_roughness,
    symmetry_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

    from legolization.layout import Layout, PlacedBrick

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_OMR = _REPO / "datasets" / "omr" / "ldraw"
_DEFAULT_OUT = _REPO / "eval" / "datasets" / "aesthetics-drift"

# Every term under validation. ``layer_symmetry`` is the superseded v1,
# kept for the side-by-side with its global-plane replacement.
TERMS: dict[str, Callable[[Layout], float]] = {
    "symmetry": symmetry_error,
    "layer_symmetry": layer_symmetry_error,
    "perpendicularity": perpendicularity_error,
    "speckle": colour_speckle_error,
    "profile": profile_roughness,
}

_PASS_MEAN_RHO = 0.6
_PASS_POSITIVE_FRACTION = 0.8
# A series starting above this has no headroom left to detect drift with.
_SATURATED_START = 0.9
# Drift budget per brick; past this the layout is scrambled, not drifting.
_STEPS_PER_BRICK = 2
_MOVES: tuple[tuple[int, int, int], ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DriftConfig:
    """Trajectory shape: how far to drift and how often to measure.

    ``steps`` is a cap; each model actually drifts
    ``min(steps, 2 x brick count)`` operations (see the module docstring).
    """

    steps: int = 300
    every: int = 10
    seeds: tuple[int, ...] = (0, 1, 2)
    max_tries: int = 20
    models: int = 25
    min_bricks: int = 20


def _components(layout: Layout) -> int:
    return ConnectionGraph.from_layout(layout).component_count()


def _choose_brick(layout: Layout, rng: np.random.Generator) -> int:
    return int(rng.choice(sorted(layout.bricks)))


def _reinsert(
    layout: Layout,
    brick: PlacedBrick,
    *,
    offset: tuple[int, int, int] = (0, 0, 0),
    colour: int | None = None,
) -> PlacedBrick:
    """Re-add a removed brick, optionally displaced or recoloured.

    The brick's LDU offset is preserved: OMR imports carry non-zero offsets,
    and dropping one can physically collide even at the original position.
    """
    dx, dy, dz = offset
    return layout.add(
        brick.part_key,
        brick.x + dx,
        brick.y + dy,
        brick.layer + dz,
        brick.yaw,
        brick.colour_code if colour is None else colour,
        offset_ldu=brick.offset_ldu,
    )


def _op_delete(layout: Layout, rng: np.random.Generator, _palette: list[int]) -> bool:
    if len(layout) <= 1:
        return False
    before = _components(layout)
    removed = layout.remove(_choose_brick(layout, rng))
    if _components(layout) > before:
        _reinsert(layout, removed)
        return False
    return True


def _op_move(layout: Layout, rng: np.random.Generator, _palette: list[int]) -> bool:
    before = _components(layout)
    brick = layout.bricks[_choose_brick(layout, rng)]
    dx, dy, dz = _MOVES[int(rng.integers(len(_MOVES)))]
    removed = layout.remove(brick.brick_id)
    try:
        moved = _reinsert(layout, removed, offset=(dx, dy, dz))
    except ValueError:  # collision or below ground
        moved = None
    if moved is not None and _components(layout) <= before:
        return True
    if moved is not None:
        layout.remove(moved.brick_id)
    _reinsert(layout, removed)
    return False


def _op_recolour(layout: Layout, rng: np.random.Generator, palette: list[int]) -> bool:
    brick = layout.bricks[_choose_brick(layout, rng)]
    alternatives = [colour for colour in palette if colour != brick.colour_code]
    if not alternatives:
        return False
    colour = alternatives[int(rng.integers(len(alternatives)))]
    removed = layout.remove(brick.brick_id)
    _reinsert(layout, removed, colour=colour)
    return True


def _swappable_pairs(layout: Layout) -> list[tuple[int, int]]:
    """Return every differently coloured unordered brick pair exactly once."""
    ids = sorted(layout.bricks)
    return [
        (first, second)
        for position, first in enumerate(ids)
        for second in ids[position + 1 :]
        if layout.bricks[first].colour_code != layout.bricks[second].colour_code
    ]


def _op_swap(layout: Layout, rng: np.random.Generator, _palette: list[int]) -> bool:
    if len(layout) < 2:
        return False
    pairs = _swappable_pairs(layout)
    if not pairs:
        return False
    first_id, second_id = pairs[int(rng.integers(len(pairs)))]
    first = layout.bricks[first_id]
    second = layout.bricks[second_id]
    removed_first = layout.remove(first.brick_id)
    removed_second = layout.remove(second.brick_id)
    _reinsert(layout, removed_first, colour=removed_second.colour_code)
    _reinsert(layout, removed_second, colour=removed_first.colour_code)
    return True


_OPS = (_op_delete, _op_move, _op_recolour, _op_swap)


def perturb(
    layout: Layout,
    rng: np.random.Generator,
    *,
    palette: list[int],
    max_tries: int,
) -> None:
    """Apply one random valid perturbation in place, or give up after retries."""
    for _ in range(max_tries):
        operator = _OPS[int(rng.integers(len(_OPS)))]
        if operator(layout, rng, palette):
            return


def trajectory(
    layout: Layout, config: DriftConfig, seed: int
) -> dict[str, list[float]]:
    """Drift one layout and record every term at each checkpoint."""
    work = layout.copy()
    rng = np.random.default_rng(seed)
    palette = sorted({brick.colour_code for brick in layout})
    steps = min(config.steps, _STEPS_PER_BRICK * len(layout))
    series: dict[str, list[float]] = {
        name: [term(work)] for name, term in TERMS.items()
    }
    for step in range(1, steps + 1):
        perturb(work, rng, palette=palette, max_tries=config.max_tries)
        if step % config.every == 0:
            for name, term in TERMS.items():
                series[name].append(term(work))
    return series


def _rho(values: Sequence[float]) -> float:
    """Spearman's rho against checkpoint order; constant series score 0."""
    array = np.asarray(values)
    if np.allclose(array, array[0]):
        return 0.0
    rho = spearmanr(np.arange(array.size), array).statistic
    return 0.0 if np.isnan(rho) else float(rho)


def summarize(
    trajectories: dict[str, dict[int, dict[str, list[float]]]],
) -> dict[str, dict[str, object]]:
    """Aggregate the per-(model, seed) rho values into per-term verdicts.

    Series that start above ``_SATURATED_START`` are excluded from that
    term's aggregate (and counted): a reference with no headroom cannot show
    detection, whatever the term is worth.
    """
    verdicts: dict[str, dict[str, object]] = {}
    for term in TERMS:
        rhos: list[float] = []
        deltas: list[float] = []
        saturated = 0
        for seeds in trajectories.values():
            for series in seeds.values():
                values = series[term]
                if values[0] > _SATURATED_START:
                    saturated += 1
                    continue
                rhos.append(_rho(values))
                deltas.append(values[-1] - values[0])
        if not rhos:
            verdicts[term] = {
                "mean_rho": 0.0,
                "positive_fraction": 0.0,
                "mean_delta": 0.0,
                "series": 0,
                "saturated": saturated,
                "passed": False,
            }
            continue
        rho_array = np.asarray(rhos)
        mean_rho = float(rho_array.mean())
        positive = float((rho_array > 0).mean())
        verdicts[term] = {
            "mean_rho": mean_rho,
            "positive_fraction": positive,
            "mean_delta": float(np.mean(deltas)),
            "series": len(rhos),
            "saturated": saturated,
            "passed": mean_rho >= _PASS_MEAN_RHO
            and positive >= _PASS_POSITIVE_FRACTION,
        }
    return verdicts


def _baseline_module() -> ModuleType:
    """Load aesthetics_baseline.py for its shared OMR skeleton importer."""
    spec = importlib.util.spec_from_file_location(
        "aesthetics_baseline_script", _REPO / "scripts" / "aesthetics_baseline.py"
    )
    if spec is None or spec.loader is None:
        msg = "aesthetics_baseline.py is not importable"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def to_markdown(
    verdicts: dict[str, dict[str, object]], *, models: int, config: DriftConfig
) -> str:
    """Render the verdict table."""
    lines = [
        "# Permutation drift: do the beauty terms detect vandalism?",
        "",
        f"{models} OMR skeletons x seeds {list(config.seeds)},",
        f"min({config.steps}, {_STEPS_PER_BRICK} x bricks) accumulating",
        f"perturbations per model, measured every {config.every}. rho is",
        "Spearman's correlation between checkpoint index and term value;",
        f"PASS needs mean(rho) >= {_PASS_MEAN_RHO} and",
        f"fraction(rho > 0) >= {_PASS_POSITIVE_FRACTION} over the",
        f"informative series (start <= {_SATURATED_START}; saturated starts",
        "are counted, not scored - no headroom, no detection).",
        "",
        "| term | mean rho | rho > 0 | mean drift | series | saturated | verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    lines.extend(
        f"| {term} | {row['mean_rho']:.3f} | {row['positive_fraction']:.0%} | "
        f"{row['mean_delta']:+.4f} | {row['series']} | {row['saturated']} | "
        f"{'PASS' if row['passed'] else 'fail'} |"
        for term, row in verdicts.items()
    )
    lines += [
        "",
        "A term that fails here cannot tell a progressively vandalized",
        "official set from the original, so it must not carry objective",
        "weight regardless of how its population medians look",
        "(`scripts/aesthetics_baseline.py`).",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    defaults = DriftConfig()
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--omr", type=Path, default=_DEFAULT_OMR)
    parser.add_argument("--sample", type=int, default=defaults.models)
    parser.add_argument("--steps", type=int, default=defaults.steps)
    parser.add_argument("--every", type=int, default=defaults.every)
    parser.add_argument(
        "--seeds",
        type=lambda text: tuple(int(part) for part in text.split(",")),
        default=defaults.seeds,
    )
    parser.add_argument("--min-bricks", type=int, default=defaults.min_bricks)
    parser.add_argument("--max-tries", type=int, default=defaults.max_tries)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Drift the reference corpus and write the per-term verdicts."""
    args = parse_args(argv)
    config = DriftConfig(
        steps=args.steps,
        every=args.every,
        seeds=args.seeds,
        max_tries=args.max_tries,
        models=args.sample,
        min_bricks=args.min_bricks,
    )
    baseline = _baseline_module()
    trajectories: dict[str, dict[int, dict[str, list[float]]]] = {}
    for name, layout in baseline.human_layouts(
        args.omr, limit=config.models, min_bricks=config.min_bricks
    ):
        trajectories[name] = {
            seed: trajectory(layout, config, seed) for seed in config.seeds
        }
        print(f"drifted {name}", file=sys.stderr)
    if not trajectories:
        print("no reference layouts; check --omr", file=sys.stderr)
        return 1

    verdicts = summarize(trajectories)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(
            {
                "generated": stamp,
                "config": asdict(config),
                "models": sorted(trajectories),
                "terms": verdicts,
                "trajectories": trajectories,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown = to_markdown(verdicts, models=len(trajectories), config=config)
    (out / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
