"""Render model pairs at fixed views for forced-choice aesthetic judging.

Comparability is the whole point: both sides of a pair render with the same
views, the same size, and the same renderer, so a judged difference is a
difference between models, not between camera set-ups. Presentation order is
randomized per pair with a seeded RNG and *recorded* — the verdict convention
(`references/aesthetic-preferences/README.md`) names the model, never the
screen position, so position bias stays measurable instead of silent.

The output is a manifest the `judge-aesthetics` skill and
`scripts/review_pairs.py` both read::

    uv run python scripts/render_pairs.py --pair A.ldr B.ldr \
        [--pair C D ...] [--views front,iso,top] [--size 800] [--seed 0]
        [--out DIR]

Each side may be a model file or a bundle directory (resolved through
``bundle.json`` like the render CLI). A pair with any failed view is recorded
with ``status: "render-failed"`` and must not be judged — a missing angle is
exactly the kind of asymmetry the fixed-view rule exists to prevent.
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

from legolization.model_render import DEFAULT_VIEWS, render_model_views, resolve_model

if TYPE_CHECKING:
    from collections.abc import Sequence

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _REPO / "eval" / "preferences"


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderedPair:
    """One rendered comparison, ready for a judge."""

    pair_id: str
    model_a: str
    model_b: str
    sha256_a: str | None
    sha256_b: str | None
    presentation_order: str
    images: dict[str, dict[str, str]]
    status: str
    errors: dict[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class _RenderedSide:
    """One source resolved, hashed, and rendered without escaping failures."""

    recorded_path: str
    sha256: str | None
    images: dict[str, str]
    complete: bool
    error: str | None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recorded_path(path: Path) -> str:
    """Use a repository-relative path when portable, otherwise an absolute one."""
    resolved = path.resolve()
    return (
        str(resolved.relative_to(_REPO))
        if resolved.is_relative_to(_REPO)
        else str(resolved)
    )


def _render_side(
    source: Path, out_dir: Path, *, views: Sequence[str], size: int
) -> _RenderedSide:
    """Resolve and render one side, returning failures as manifest data."""
    recorded_path = _recorded_path(source)
    try:
        model = resolve_model(source)
        digest = _sha256(model)
        report = render_model_views(
            model,
            views=views,
            out_dir=out_dir,
            width=size,
            height=size * 3 // 4,
        )
    except Exception as error:  # noqa: BLE001 - pair-local external tool boundary
        detail = str(error)
        print(f"render failed for {source}: {detail}", file=sys.stderr)
        return _RenderedSide(
            recorded_path=recorded_path,
            sha256=None,
            images={},
            complete=False,
            error=detail,
        )
    images: dict[str, str] = {}
    failures: list[str] = []
    for outcome in report.outcomes:
        if outcome.status == "ok" and outcome.path is not None:
            images[outcome.view] = _recorded_path(outcome.path)
        else:
            failures.append(
                f"{outcome.view}: {outcome.detail or 'renderer produced no image'}"
            )
    error = "; ".join(failures) if failures else None
    if error is not None:
        print(f"render failed for {source}: {error}", file=sys.stderr)
    return _RenderedSide(
        recorded_path=recorded_path,
        sha256=digest,
        images=images,
        complete=not failures,
        error=error,
    )


def render_pair(  # noqa: PLR0913 - one bag of render state
    pair_id: str,
    side_a: Path,
    side_b: Path,
    out_dir: Path,
    *,
    views: Sequence[str],
    size: int,
    rng: np.random.Generator,
) -> RenderedPair:
    """Render both sides of one comparison under identical camera set-ups."""
    rendered_a = _render_side(
        source=side_a,
        out_dir=out_dir / "a",
        views=views,
        size=size,
    )
    rendered_b = _render_side(
        source=side_b,
        out_dir=out_dir / "b",
        views=views,
        size=size,
    )
    return RenderedPair(
        pair_id=pair_id,
        model_a=rendered_a.recorded_path,
        model_b=rendered_b.recorded_path,
        sha256_a=rendered_a.sha256,
        sha256_b=rendered_b.sha256,
        presentation_order="ab" if rng.integers(2) == 0 else "ba",
        images={"a": rendered_a.images, "b": rendered_b.images},
        status=(
            "rendered"
            if rendered_a.complete and rendered_b.complete
            else "render-failed"
        ),
        errors={
            side: error
            for side, error in (("a", rendered_a.error), ("b", rendered_b.error))
            if error is not None
        },
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--pair",
        nargs=2,
        action="append",
        type=Path,
        required=True,
        metavar=("A", "B"),
        help="two models (files or bundle directories) to compare; repeatable",
    )
    parser.add_argument(
        "--views",
        type=lambda text: tuple(text.split(",")),
        default=DEFAULT_VIEWS,
    )
    parser.add_argument("--size", type=int, default=800, help="image width")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Render every pair and write the judging manifest."""
    args = parse_args(argv)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out if args.out is not None else _DEFAULT_OUT / f"pairs-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    pairs: list[RenderedPair] = []
    for index, (side_a, side_b) in enumerate(args.pair):
        pair_id = f"{stamp}-{index:03d}"
        pair = render_pair(
            pair_id=pair_id,
            side_a=side_a,
            side_b=side_b,
            out_dir=out / pair_id,
            views=args.views,
            size=args.size,
            rng=rng,
        )
        pairs.append(pair)
        print(f"{pair_id}: {pair.status}", file=sys.stderr)

    manifest = {
        "generated": stamp,
        "seed": args.seed,
        "views": list(args.views),
        "size": args.size,
        "pairs": [
            {
                "id": pair.pair_id,
                "model_a": pair.model_a,
                "model_b": pair.model_b,
                "sha256_a": pair.sha256_a,
                "sha256_b": pair.sha256_b,
                "presentation_order": pair.presentation_order,
                "images": pair.images,
                "status": pair.status,
                "errors": pair.errors,
            }
            for pair in pairs
        ],
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(manifest_path)
    failed = sum(pair.status != "rendered" for pair in pairs)
    return 1 if failed == len(pairs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
