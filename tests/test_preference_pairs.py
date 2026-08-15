"""Conventions of the tracked aesthetic preference log.

The one that matters most is polarity: ``winner`` names the MODEL (a =
``model_a``), never the screen position — ``presentation_order`` exists
precisely so the two can differ. Reading it backwards would flip every
verdict while still producing a plausible log, the same failure mode the
dataset adapters pin against.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).parent.parent


def _load_review_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "review_pairs", _REPO / "scripts" / "review_pairs.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["review_pairs"] = module
    spec.loader.exec_module(module)
    return module


def _load_render_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "render_pairs", _REPO / "scripts" / "render_pairs.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_pairs"] = module
    spec.loader.exec_module(module)
    return module


def _verdict(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "20260809T000000Z-000",
        "model_a": "x/a.mpd",
        "model_b": "x/b.mpd",
        "sha256_a": "aa",
        "sha256_b": "bb",
        "views": ["front", "iso", "top"],
        "winner": "a",
        "judge": "claude",
        "confidence": "high",
        "presentation_order": "ba",
    }
    row.update(overrides)
    return row


def _manifest_pair(**overrides: object) -> dict[str, object]:
    pair: dict[str, object] = {
        "id": "20260809T000000Z-000",
        "model_a": "x/a.mpd",
        "model_b": "x/b.mpd",
        "sha256_a": "aa",
        "sha256_b": "bb",
        "presentation_order": "ba",
        "images": {"a": {"iso": "a.png"}, "b": {"iso": "b.png"}},
        "status": "rendered",
    }
    pair.update(overrides)
    return pair


def test_validation_rejects_out_of_vocabulary_rows():
    review = _load_review_module()
    review.validate_verdict(_verdict())  # the reference row is valid
    for bad in (
        _verdict(winner="left"),
        _verdict(judge="gpt"),
        _verdict(confidence="medium"),
        _verdict(presentation_order="left-right"),
        _verdict(views=[]),
    ):
        with pytest.raises(review.VerdictError):
            review.validate_verdict(bad)
    incomplete = _verdict()
    del incomplete["sha256_a"]
    with pytest.raises(review.VerdictError):
        review.validate_verdict(incomplete)


def test_winner_names_the_model_not_the_screen_side(tmp_path):
    # presentation_order is "ba": model_b was shown first. A human answering
    # "…=a" still means model_a wins; merge must not translate positions.
    review = _load_review_module()
    log = tmp_path / "pairs.jsonl"
    manifest = {"pairs": [_manifest_pair()]}
    review.merge_verdicts("20260809T000000Z-000=a", manifest, log=log)
    (row,) = review.load_log(log)
    assert row["winner"] == "a"
    assert row["sha256_a"] == "aa"  # the identity the letter refers to
    assert row["presentation_order"] == "ba"  # what the judge actually saw
    assert row["judge"] == "human"
    assert "recorded" in row


def test_pending_review_is_no_row_or_low_confidence_claude(tmp_path):
    review = _load_review_module()
    log = tmp_path / "pairs.jsonl"
    pairs = [
        _manifest_pair(id="p-judged-high"),
        _manifest_pair(id="p-escalated"),
        _manifest_pair(id="p-unjudged"),
        _manifest_pair(id="p-broken", status="render-failed"),
    ]
    review.append_verdict(_verdict(id="p-judged-high", confidence="high"), log=log)
    review.append_verdict(_verdict(id="p-escalated", confidence="low"), log=log)
    pending = review.pending_pairs({"pairs": pairs}, review.load_log(log))
    assert [pair["id"] for pair in pending] == ["p-escalated", "p-unjudged"]
    # Human escalation stays blind: Claude's provisional call never reaches
    # the review page, where it could anchor the human's choice.
    assert pending[0] == pairs[1]


def test_human_row_supersedes_the_escalation(tmp_path):
    review = _load_review_module()
    log = tmp_path / "pairs.jsonl"
    review.append_verdict(_verdict(id="p-1", confidence="low"), log=log)
    review.append_verdict(_verdict(id="p-1", judge="human", winner="b"), log=log)
    pending = review.pending_pairs(
        {"pairs": [_manifest_pair(id="p-1")]}, review.load_log(log)
    )
    assert pending == []
    assert len(review.load_log(log)) == 2  # append-only, nothing rewritten


def test_merge_refuses_a_pair_with_failed_renders(tmp_path):
    review = _load_review_module()
    log = tmp_path / "pairs.jsonl"
    with pytest.raises(review.VerdictError, match="cannot be judged"):
        review.merge_verdicts(
            "20260809T000000Z-000=a",
            {"pairs": [_manifest_pair(status="render-failed")]},
            log=log,
        )
    assert not log.exists()


def test_merge_is_atomic_when_a_later_pair_has_failed_renders(
    tmp_path: Path,
) -> None:
    review = _load_review_module()
    log = tmp_path / "pairs.jsonl"
    manifest = {
        "pairs": [
            _manifest_pair(id="p-valid"),
            _manifest_pair(id="p-failed", status="render-failed"),
        ]
    }
    with pytest.raises(review.VerdictError, match="cannot be judged"):
        review.merge_verdicts("p-valid=a p-failed=b", manifest, log=log)
    assert not log.exists()


def test_merge_refuses_duplicate_pair_ids_atomically(tmp_path: Path) -> None:
    review = _load_review_module()
    log = tmp_path / "pairs.jsonl"
    manifest = {"pairs": [_manifest_pair(id="p-duplicate")]}
    with pytest.raises(review.VerdictError, match="appears more than once"):
        review.merge_verdicts(
            "p-duplicate=a p-duplicate=b",
            manifest,
            log=log,
        )
    assert not log.exists()


def test_render_pair_contains_source_failures_and_records_portable_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = _load_render_module()

    def fail_to_resolve(path: Path) -> Path:
        msg = f"unreadable {path.name}"
        raise OSError(msg)

    monkeypatch.setattr(render, "resolve_model", fail_to_resolve)
    pair = render.render_pair(
        pair_id="broken",
        side_a=tmp_path / "a.ldr",
        side_b=tmp_path / "b.ldr",
        out_dir=tmp_path / "renders",
        views=("iso",),
        size=200,
        rng=np.random.default_rng(0),
    )

    assert pair.status == "render-failed"
    assert pair.sha256_a is None
    assert pair.sha256_b is None
    assert set(pair.errors) == {"a", "b"}
    assert Path(pair.model_a).is_absolute()
    assert render._recorded_path(_REPO / "scripts" / "review_pairs.py") == (  # noqa: SLF001
        "scripts/review_pairs.py"
    )


def test_review_main_reports_malformed_record_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _load_review_module()

    assert review.main(["--record", "{", "--log", str(tmp_path / "log.jsonl")]) == 1
    captured = capsys.readouterr().err
    assert "cannot record verdict" in captured
    assert "Traceback" not in captured


def test_review_page_marks_missing_images_unjudgeable_with_useful_alt_text(
    tmp_path: Path,
) -> None:
    review = _load_review_module()
    existing = tmp_path / "b.png"
    existing.write_bytes(b"png bytes")
    pair = _manifest_pair(
        images={
            "a": {"top": str(tmp_path / "missing.png")},
            "b": {"iso": str(existing)},
        }
    )

    page = review.review_page([pair])

    assert 'alt="side 1, iso view"' in page
    assert "This pair is unjudgeable" in page
    assert "Missing image" in page


def test_readme_documents_the_log_beside_it():
    readme = (_REPO / "references" / "aesthetic-preferences" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "winner" in readme
    assert "never the screen position" in readme
    log_dir = _REPO / "references" / "aesthetic-preferences"
    stray = [
        path.name
        for path in log_dir.iterdir()
        if path.name not in {"README.md", "pairs.jsonl", "models"}
    ]
    assert not stray, f"unexpected files in the tracked log dir: {stray}"


def test_log_rows_if_any_all_validate():
    review = _load_review_module()
    for row in review.load_log():
        review.validate_verdict(dict(row))
        json.dumps(row)  # every row stays JSON-serializable as read


def test_committed_rows_reference_portable_hash_pinned_models():
    review = _load_review_module()
    for row in review.load_log():
        for side in ("a", "b"):
            model = Path(str(row[f"model_{side}"]))
            assert not model.is_absolute()
            resolved = _REPO / model
            assert resolved.is_file()
            assert (
                hashlib.sha256(resolved.read_bytes()).hexdigest()
                == row[f"sha256_{side}"]
            )
