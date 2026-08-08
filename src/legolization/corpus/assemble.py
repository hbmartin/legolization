"""Assemble a corpus scorecard from one completed collection manifest.

This command never runs placement. It validates every candidate artifact
against the collection's commit, dirty-source, configuration, and input
identities, then writes a scorecard and optionally the canonical baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from legolization.compare import Candidate, select_best
from legolization.corpus.collect import (
    BASELINE,
    MESH_BASELINE,
    RUNS,
    build_row,
    compare_to_baseline,
    to_markdown,
)
from legolization.corpus.manifest import load_manifest
from legolization.eval_artifacts import atomic_json, candidate_from_payload
from legolization.placement.registry import strategy_names

if TYPE_CHECKING:
    from legolization.corpus.collect import CorpusModelLike


@dataclass(frozen=True, slots=True, kw_only=True)
class AssembleOutcome:
    """Everything one assembly pass produced or refused to produce."""

    collection: Path
    rows: list[dict[str, Any]]
    incomplete: tuple[str, ...] = ()
    scope_refusal: str | None = None
    scorecard_path: Path | None = None
    markdown_path: Path | None = None
    failures: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    baseline_path: Path | None = None
    baseline_written: bool = False
    baseline_refusal: str | None = None

    @property
    def operational_error(self) -> str | None:
        """The mechanical failure blocking assembly, if any."""
        if self.incomplete:
            return "collection is incomplete"
        return self.scope_refusal


def _artifact_path(raw: str, base: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else base / path


def _load_candidate(
    candidate_entry: dict[str, Any],
    model_entry: dict[str, Any],
    identity: dict[str, Any],
    base: Path,
) -> tuple[Candidate | None, str | None]:
    artifact_raw = candidate_entry.get("artifact")
    label = (
        f"{model_entry['model']}/{candidate_entry['strategy']}"
        f"/seed-{candidate_entry['seed']}"
    )
    if not artifact_raw:
        return None, f"{label}: missing artifact path"
    path = _artifact_path(str(artifact_raw), base)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return None, f"{label}: unreadable artifact ({error})"
    if not isinstance(payload, dict):
        return None, f"{label}: invalid payload structure"
    expected = {
        "identity": identity,
        "config_hash": candidate_entry.get("config_hash"),
        "input_hash": model_entry.get("input_hash"),
        "model": model_entry["model"],
        "strategy": candidate_entry["strategy"],
        "seed": candidate_entry["seed"],
    }
    mismatched = [key for key, value in expected.items() if payload.get(key) != value]
    if mismatched:
        return None, f"{label}: identity mismatch ({', '.join(mismatched)})"
    try:
        return candidate_from_payload(payload), None
    except (KeyError, TypeError, ValueError) as error:
        return None, f"{label}: invalid payload ({error})"


def _rows(
    manifest: dict[str, Any],
    base: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    identity = manifest["identity"]
    for model_entry in manifest["models"]:
        candidates: list[Candidate] = []
        for candidate_entry in model_entry["candidates"]:
            candidate, error = _load_candidate(
                candidate_entry,
                model_entry,
                identity,
                base,
            )
            if error is not None:
                errors.append(error)
            elif candidate is not None:
                candidates.append(candidate)
        if len(candidates) != len(model_entry["candidates"]):
            continue
        report = select_best(candidates)
        model = cast(
            "CorpusModelLike",
            SimpleNamespace(
                name=model_entry["model"],
                kind=model_entry["kind"],
                traits=tuple(model_entry["traits"]),
                expect_min_buildable=model_entry["expect_min_buildable"],
            ),
        )
        row = build_row(
            model,
            report.to_dict(),
            "ok" if report.winner is not None else "error: all failed",
        )
        row["unsupported_ratio"] = model_entry.get("unsupported_ratio")
        rows.append(row)
    return rows, errors


def _canonical_scope(
    manifest: dict[str, Any],
) -> bool:
    scope = manifest["scope"]
    expected_models = {
        model.name for model in load_manifest() if model.kind == scope["kind"]
    }
    return (
        set(scope["models"]) == expected_models
        and set(scope["strategies"]) == set(strategy_names())
        and scope["seeds"] == [0]
    )


def _baseline_path(
    manifest: dict[str, Any],
    explicit: Path | None,
) -> Path:
    if explicit is not None:
        return explicit
    return BASELINE if manifest["scope"]["kind"] == "synthetic" else MESH_BASELINE


def _failed_models(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    """Return models whose evaluation failed, expectation failures included."""
    successful = [row for row in rows if row["status"] == "ok"]
    failed = [row["model"] for row in rows if row["status"] != "ok"]
    failed.extend(row["model"] for row in successful if not row["expectation_ok"])
    return tuple(failed)


def _payload(
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    stamp: str,
) -> dict[str, object]:
    return {
        "schema": 2,
        "generated": stamp,
        "collection_id": manifest["collection_id"],
        "identity": manifest["identity"],
        "scope": manifest["scope"],
        "input_hashes": {
            model["model"]: model["input_hash"] for model in manifest["models"]
        },
        "configuration_hashes": {
            (
                f"{model['model']}/{candidate['strategy']}/seed-{candidate['seed']}"
            ): candidate["config_hash"]
            for model in manifest["models"]
            for candidate in model["candidates"]
        },
        "models": rows,
    }


def _require(*, condition: bool, message: str) -> None:
    """Reject a structurally invalid manifest with an actionable message."""
    if not condition:
        raise ValueError(message)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_candidate(entry: object, model: str, index: int) -> tuple[str, int]:
    """Check one candidate row and return its ``(strategy, seed)`` position."""
    label = f"model {model!r} candidate[{index}]"
    _require(condition=isinstance(entry, dict), message=f"{label} must be an object")
    candidate = cast("dict[str, Any]", entry)
    strategy = candidate.get("strategy")
    seed = candidate.get("seed")
    _require(
        condition=isinstance(strategy, str),
        message=f"{label} has a non-string strategy",
    )
    _require(
        condition=isinstance(seed, int),
        message=f"{label} has a non-integer seed",
    )
    # None is the well-formed "not collected yet" state; `_rows` turns that
    # into a precise per-candidate error, which beats rejecting the file.
    _require(
        condition=isinstance(candidate.get("config_hash"), str | None),
        message=f"{label} has an invalid config_hash",
    )
    _require(
        condition=isinstance(candidate.get("artifact"), str | None),
        message=f"{label} has an invalid artifact path",
    )
    return cast("str", strategy), cast("int", seed)


def _validate_model(entry: object, index: int, expected: set[tuple[str, int]]) -> str:
    """Check one model row and return its name."""
    _require(
        condition=isinstance(entry, dict),
        message=f"models[{index}] must be an object",
    )
    model = cast("dict[str, Any]", entry)
    name = model.get("model")
    _require(
        condition=isinstance(name, str),
        message=f"models[{index}].model must be a string",
    )
    label = cast("str", name)
    _require(
        condition=isinstance(model.get("kind"), str),
        message=f"model {label!r} has a non-string kind",
    )
    _require(
        condition=_is_str_list(model.get("traits")),
        message=f"model {label!r} has invalid traits",
    )
    _require(
        condition=isinstance(model.get("expect_min_buildable"), int),
        message=f"model {label!r} has a non-integer expect_min_buildable",
    )
    _require(
        condition=isinstance(model.get("input_hash"), str | None),
        message=f"model {label!r} has an invalid input_hash",
    )
    candidates = model.get("candidates")
    _require(
        condition=isinstance(candidates, list),
        message=f"model {label!r} has invalid candidates",
    )
    matrix = [
        _validate_candidate(candidate, label, position)
        for position, candidate in enumerate(cast("list[object]", candidates))
    ]
    _require(
        condition=len(matrix) == len(expected) and set(matrix) == expected,
        message=f"model {label!r} does not carry the complete candidate matrix",
    )
    return label


def _validate_scope(scope: object) -> tuple[list[str], set[tuple[str, int]]]:
    """Check the scope block and return its models and candidate matrix."""
    _require(condition=isinstance(scope, dict), message="scope must be an object")
    fields = cast("dict[str, Any]", scope)
    seeds = fields.get("seeds")
    _require(
        condition=isinstance(fields.get("kind"), str),
        message="scope.kind must be a string",
    )
    _require(
        condition=_is_str_list(fields.get("models")),
        message="scope.models must be a list of strings",
    )
    _require(
        condition=_is_str_list(fields.get("strategies")),
        message="scope.strategies must be a list of strings",
    )
    _require(
        condition=isinstance(seeds, list)
        and all(isinstance(seed, int) for seed in seeds),
        message="scope.seeds must be a list of integers",
    )
    return cast("list[str]", fields["models"]), {
        (strategy, seed)
        for strategy in cast("list[str]", fields["strategies"])
        for seed in cast("list[int]", seeds)
    }


def _validate_manifest(payload: object) -> dict[str, Any]:
    """Return a structurally sound collection manifest, or raise ``ValueError``.

    This checks shape, not progress: a collection still mid-flight is
    well-formed and its ``None`` placeholders survive, because ``_rows``
    reports those as named incomplete candidates. Without the check, a
    hand-edited or truncated manifest surfaced as a ``KeyError`` from deep
    inside row building instead of a message naming the bad field.
    """
    _require(
        condition=isinstance(payload, dict),
        message="manifest root must be an object",
    )
    manifest = cast("dict[str, Any]", payload)
    _require(
        condition=manifest.get("schema") == 1,
        message="unsupported collection manifest schema",
    )
    _require(
        condition=isinstance(manifest.get("collection_id"), str),
        message="collection_id must be a string",
    )
    _require(
        condition=isinstance(manifest.get("identity"), dict),
        message="identity must be an object",
    )
    scope_models, expected = _validate_scope(manifest.get("scope"))
    models = manifest.get("models")
    _require(condition=isinstance(models, list), message="models must be a list")
    names = [
        _validate_model(entry, index, expected)
        for index, entry in enumerate(cast("list[object]", models))
    ]
    _require(
        condition=len(names) == len(scope_models) and set(names) == set(scope_models),
        message="models entries do not match the scope model set",
    )
    return manifest


def _read_manifest(collection: Path) -> dict[str, Any]:
    """Load and validate a collection manifest, raising ``ValueError``."""
    try:
        payload = json.loads(collection.read_text())
    except (OSError, json.JSONDecodeError) as error:
        msg = f"cannot read collection: {error}"
        raise ValueError(msg) from error
    return _validate_manifest(payload)


def _baseline_diff(
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    baseline_path: Path,
    *,
    tolerance: float,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compare successful rows to the baseline when the scope is comparable."""
    seeds = manifest["scope"]["seeds"]
    comparable = len(seeds) == 1 and set(manifest["scope"]["strategies"]) == set(
        strategy_names()
    )
    if not comparable or not baseline_path.exists():
        return (), ()
    known = json.loads(baseline_path.read_text())["models"]
    hard, info = compare_to_baseline(
        rows=[row for row in rows if row["status"] == "ok"],
        baseline_rows=known,
        tolerance=tolerance,
    )
    return tuple(hard), tuple(info)


def assemble_collection(
    collection: Path,
    *,
    out: Path,
    baseline: Path | None = None,
    write_baseline: bool = False,
    tolerance: float = 0.05,
) -> AssembleOutcome:
    """Validate one collection and assemble its scorecard artifacts.

    Raises :class:`ValueError` for an unreadable or unsupported
    collection; every other failure is reported on the outcome. A
    baseline write requires the canonical scope and refuses when the
    evaluation had failures, always preserving the existing file.
    """
    manifest = _read_manifest(collection)
    rows, errors = _rows(manifest, collection.parent)
    if errors:
        return AssembleOutcome(
            collection=collection,
            rows=rows,
            incomplete=tuple(errors),
        )
    if write_baseline and not _canonical_scope(manifest):
        return AssembleOutcome(
            collection=collection,
            rows=rows,
            scope_refusal=(
                "baseline assembly requires the full kind, every strategy, and seed 0"
            ),
        )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = _payload(manifest, rows, stamp)
    out_dir = out / str(manifest["collection_id"])
    scorecard_path = out_dir / "scorecard.json"
    atomic_json(scorecard_path, payload)
    markdown_path = out_dir / "scorecard.md"
    markdown_path.write_text(to_markdown(rows) + "\n")

    failures = _failed_models(rows)
    baseline_path = _baseline_path(manifest, baseline)
    regressions: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    if not write_baseline:
        regressions, notes = _baseline_diff(
            manifest,
            rows,
            baseline_path,
            tolerance=tolerance,
        )
    baseline_written = False
    baseline_refusal: str | None = None
    if write_baseline:
        if failures:
            baseline_refusal = "baseline not written because the evaluation failed"
        else:
            atomic_json(baseline_path, payload)
            baseline_written = True
    return AssembleOutcome(
        collection=collection,
        rows=rows,
        scorecard_path=scorecard_path,
        markdown_path=markdown_path,
        failures=failures,
        regressions=regressions,
        notes=notes,
        baseline_path=baseline_path,
        baseline_written=baseline_written,
        baseline_refusal=baseline_refusal,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse assembler command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("--out", type=Path, default=RUNS)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--tolerance", type=float, default=0.05)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Validate one collection and assemble its scorecard."""
    args = parse_args(argv)
    try:
        outcome = assemble_collection(
            args.collection,
            out=args.out,
            baseline=args.baseline,
            write_baseline=args.write_baseline,
            tolerance=args.tolerance,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if outcome.incomplete:
        print("collection is incomplete:", file=sys.stderr)
        for line in outcome.incomplete:
            print(f"  {line}", file=sys.stderr)
        return 1
    if outcome.scope_refusal is not None:
        print(f"error: {outcome.scope_refusal}", file=sys.stderr)
        return 1
    print(f"wrote {outcome.scorecard_path}")
    print(to_markdown(outcome.rows))
    for model in outcome.failures:
        print(f"evaluation failure: {model}")
    for line in outcome.notes:
        print(f"note: {line}")
    for line in outcome.regressions:
        print(f"REGRESSION: {line}")
    if outcome.baseline_refusal is not None:
        print(outcome.baseline_refusal, file=sys.stderr)
    elif outcome.baseline_written:
        print(f"wrote {outcome.baseline_path}")
    return int(bool(outcome.failures or outcome.regressions))


if __name__ == "__main__":
    sys.exit(main())
