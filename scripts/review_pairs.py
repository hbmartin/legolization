"""Record, validate, and batch-review aesthetic pair verdicts.

Three modes over the tracked preference log
(`references/aesthetic-preferences/pairs.jsonl`; conventions in the README
beside it):

record one verdict (the ``judge-aesthetics`` skill's write path)::

    uv run python scripts/review_pairs.py --record '{"id": …, "winner": "a", …}'

build a self-contained review page of every pair still awaiting a human
verdict (no row yet, or only a low-confidence Claude row)::

    uv run python scripts/review_pairs.py --manifest eval/preferences/…/manifest.json

merge a human's batch verdicts back, one ``id=winner`` token per pair::

    uv run python scripts/review_pairs.py --manifest … --merge "0=a 3=b 7=tie"

Every write path funnels through the same validation, so a malformed row can
never enter the log silently; rows are append-only and a human row supersedes
(never rewrites) the Claude row for the same pair.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_REPO = Path(__file__).resolve().parent.parent
_LOG = _REPO / "references" / "aesthetic-preferences" / "pairs.jsonl"
_DEFAULT_OUT = _REPO / "eval" / "preferences"

_WINNERS = frozenset({"a", "b", "tie"})
_JUDGES = frozenset({"claude", "human"})
_CONFIDENCES = frozenset({"high", "low"})
_ORDERS = frozenset({"ab", "ba"})
_REQUIRED = (
    "id",
    "model_a",
    "model_b",
    "sha256_a",
    "sha256_b",
    "views",
    "winner",
    "judge",
    "confidence",
    "presentation_order",
)


class VerdictError(ValueError):
    """A verdict row that must not enter the log."""


def validate_verdict(row: dict[str, object]) -> dict[str, object]:
    """Check one row against the log conventions; return it stamped."""
    if missing := [key for key in _REQUIRED if key not in row]:
        msg = f"verdict is missing {', '.join(missing)}"
        raise VerdictError(msg)
    checks = (
        ("winner", _WINNERS),
        ("judge", _JUDGES),
        ("confidence", _CONFIDENCES),
        ("presentation_order", _ORDERS),
    )
    for key, allowed in checks:
        if row[key] not in allowed:
            msg = f"{key} must be one of {sorted(allowed)}, got {row[key]!r}"
            raise VerdictError(msg)
    if not isinstance(row["views"], list) or not row["views"]:
        msg = "views must be a non-empty list of view names"
        raise VerdictError(msg)
    stamped = dict(row)
    stamped.setdefault("recorded", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return stamped


def append_verdict(row: dict[str, object], *, log: Path = _LOG) -> None:
    """Validate and append one verdict line."""
    stamped = validate_verdict(row)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stamped, sort_keys=True) + "\n")


def load_log(log: Path = _LOG) -> list[dict[str, object]]:
    """Read every recorded verdict."""
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _manifest_pairs(manifest: Mapping[str, object]) -> list[dict[str, object]]:
    """Narrow the parsed manifest's pair list to the shape the writer emits."""
    pairs = manifest.get("pairs")
    if not isinstance(pairs, list):
        msg = "manifest has no pairs list; was it written by render_pairs.py?"
        raise VerdictError(msg)
    return [
        {str(key): value for key, value in pair.items()}
        for pair in pairs
        if isinstance(pair, dict)
    ]


def _side_images(pair: Mapping[str, object], side: str) -> dict[str, str]:
    """Narrow one side's view -> image-path mapping."""
    images = pair.get("images")
    if not isinstance(images, dict):
        return {}
    side_images = images.get(side)
    if not isinstance(side_images, dict):
        return {}
    return {str(view): str(path) for view, path in side_images.items()}


def pending_pairs(
    manifest: Mapping[str, object], rows: Sequence[dict[str, object]]
) -> list[dict[str, object]]:
    """Manifest pairs still awaiting a human verdict.

    A pair is settled by any ``human`` row or a ``high``-confidence ``claude``
    row; everything else (no row, or a low-confidence Claude escalation)
    belongs on the review page. Render failures are never judged.
    """
    settled = {
        str(row["id"])
        for row in rows
        if row["judge"] == "human"
        or (row["judge"] == "claude" and row["confidence"] == "high")
    }
    pending = []
    for pair in _manifest_pairs(manifest):
        pair_id = str(pair.get("id"))
        if pair.get("status") != "rendered" or pair_id in settled:
            continue
        # Do not carry an escalated model verdict into the human page: seeing
        # its winner or rationale would anchor the independent human judgment.
        pending.append(dict(pair))
    return pending


def _image_tag(path: str) -> str:
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f'<img src="data:image/png;base64,{data}" alt="">'


def review_page(pairs: Sequence[dict[str, object]]) -> str:
    """Render the pending pairs as one self-contained HTML page."""
    blocks = []
    for pair in pairs:
        order = pair.get("presentation_order")
        sides = ("a", "b") if order == "ab" else ("b", "a")
        columns = "".join(
            '<div class="side"><h3>{label}</h3>{imgs}</div>'.format(
                label=f"side {position + 1}",
                imgs="".join(
                    _image_tag(path)
                    for _, path in sorted(_side_images(pair, side).items())
                ),
            )
            for position, side in enumerate(sides)
        )
        blocks.append(
            f"<section><h2>{html.escape(str(pair['id']))}</h2>"
            f"<p>side 1 = model_{sides[0]}, side 2 = model_{sides[1]}; answer "
            f"with the MODEL letter, e.g. <code>{html.escape(str(pair['id']))}"
            f"=a</code></p>"
            f'<div class="pair">{columns}</div></section>'
        )
    body = "\n".join(blocks) if blocks else "<p>Nothing awaiting review.</p>"
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Aesthetic pair review</title><style>"
        "body{font-family:system-ui;margin:2rem;background:#fafafa;color:#222}"
        ".pair{display:flex;gap:1rem}.side{flex:1}"
        ".side img{max-width:100%;display:block;margin-bottom:.5rem;"
        "border:1px solid #ddd}"
        "section{margin-bottom:3rem;border-bottom:1px solid #ccc}"
        "</style>"
        "<h1>Aesthetic pair review</h1>"
        "<p>Pick the model that looks better built, per pair: reply with "
        "<code>id=a</code>, <code>id=b</code>, or <code>id=tie</code> tokens "
        "for scripts/review_pairs.py --merge. The letter names the model, "
        "not the screen side.</p>"
        f"{body}"
    )


def merge_verdicts(
    tokens: str, manifest: Mapping[str, object], *, log: Path = _LOG
) -> int:
    """Append one human verdict per ``id=winner`` token; return the count."""
    by_id = {str(pair.get("id")): pair for pair in _manifest_pairs(manifest)}
    pending_rows: list[dict[str, object]] = []
    for token in tokens.split():
        pair_id, _, winner = token.partition("=")
        if (pair := by_id.get(pair_id)) is None:
            msg = f"{pair_id} is not in this manifest"
            raise VerdictError(msg)
        if pair.get("status") != "rendered":
            msg = f"{pair_id} was not rendered completely and cannot be judged"
            raise VerdictError(msg)
        pending_rows.append(
            validate_verdict(
                {
                    "id": pair.get("id"),
                    "model_a": pair.get("model_a"),
                    "model_b": pair.get("model_b"),
                    "sha256_a": pair.get("sha256_a"),
                    "sha256_b": pair.get("sha256_b"),
                    "views": sorted(_side_images(pair, "a")),
                    "winner": winner,
                    "judge": "human",
                    "confidence": "high",
                    "presentation_order": pair.get("presentation_order"),
                    "notes": "batch review",
                }
            )
        )
    for row in pending_rows:
        append_verdict(row, log=log)
    return len(pending_rows)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--record", type=str, default=None, help="one verdict JSON")
    parser.add_argument("--merge", type=str, default=None, help="'id=a id=b' tokens")
    parser.add_argument("--log", type=Path, default=_LOG)
    parser.add_argument("--out", type=Path, default=None, help="review page path")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch between record, merge, and page building."""
    args = parse_args(argv)
    if args.record is not None:
        append_verdict(json.loads(args.record), log=args.log)
        print("recorded", file=sys.stderr)
        return 0
    if args.manifest is None:
        print("--manifest is required unless --record is used", file=sys.stderr)
        return 1
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.merge is not None:
        merged = merge_verdicts(args.merge, manifest, log=args.log)
        print(f"merged {merged} human verdicts", file=sys.stderr)
        return 0
    pending = pending_pairs(manifest, load_log(args.log))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out if args.out is not None else _DEFAULT_OUT / f"review-{stamp}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(review_page(pending), encoding="utf-8")
    print(f"{len(pending)} pairs awaiting review", file=sys.stderr)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
