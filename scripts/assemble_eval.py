"""Assemble a corpus scorecard from one completed collection manifest.

This command never runs placement. It validates every candidate artifact
against the collection's commit, dirty-source, configuration, and input
identities, then writes a scorecard and optionally the canonical baseline.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from legolization.compare import Candidate, select_best
from legolization.eval_artifacts import atomic_json, candidate_from_payload
from legolization.placement.registry import strategy_names

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
_ASSEMBLED = _REPO / "eval" / "runs" / "assembled"


class _CorpusModel(Protocol):
    """Corpus model fields needed for canonical-scope validation."""

    name: str
    kind: str


class _CorpusModule(Protocol):
    """Dynamically loaded corpus module surface used by the assembler."""

    def load_manifest(self) -> list[_CorpusModel]: ...


@dataclass(frozen=True, slots=True)
class _ModelSummary:
    """Typed model projection consumed by the evaluator's row builder."""

    name: str
    kind: str
    traits: tuple[str, ...]
    expect_min_buildable: int


class _Evaluator(Protocol):
    """Checked surface of the dynamically loaded evaluation module."""

    BASELINE: Path
    MESH_BASELINE: Path

    def build_row(
        self,
        model: _ModelSummary,
        report_dict: dict[str, Any],
        status: str,
    ) -> dict[str, Any]: ...

    def load_corpus_module(self) -> _CorpusModule: ...

    def to_markdown(self, rows: list[dict[str, Any]]) -> str: ...

    def compare_to_baseline(
        self,
        *,
        rows: list[dict[str, Any]],
        baseline_rows: list[dict[str, Any]],
        tolerance: float,
    ) -> tuple[list[str], list[str]]: ...


class ManifestError(ValueError):
    """A collection manifest is structurally invalid or incomplete."""


def _load_evaluator() -> _Evaluator:
    spec = importlib.util.spec_from_file_location(
        "eval_corpus_script_for_assembly",
        _SCRIPTS / "eval_corpus.py",
    )
    if spec is None or spec.loader is None:
        msg = "cannot load scripts/eval_corpus.py"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast("_Evaluator", module)


def _validate_manifest(  # noqa: C901, PLR0912, PLR0915 - schema validation
    payload: object,
) -> dict[str, Any]:
    """Return a complete collection manifest or raise a clean validation error."""
    if not isinstance(payload, dict):
        msg = "manifest root must be an object"
        raise ManifestError(msg)
    manifest = cast("dict[str, Any]", payload)
    if manifest.get("schema") != 1:
        msg = "unsupported collection manifest schema"
        raise ManifestError(msg)
    if manifest.get("status") != "complete":
        msg = "collection status is not complete"
        raise ManifestError(msg)
    if not isinstance(manifest.get("collection_id"), str):
        msg = "collection_id must be a string"
        raise ManifestError(msg)
    if not isinstance(manifest.get("identity"), dict):
        msg = "identity must be an object"
        raise ManifestError(msg)
    scope = manifest.get("scope")
    models = manifest.get("models")
    if not isinstance(scope, dict) or not isinstance(models, list):
        msg = "scope must be an object and models must be a list"
        raise ManifestError(msg)

    scope_models = scope.get("models")
    strategies = scope.get("strategies")
    seeds = scope.get("seeds")
    if (
        not isinstance(scope.get("kind"), str)
        or not isinstance(scope_models, list)
        or not all(isinstance(name, str) for name in scope_models)
        or not isinstance(strategies, list)
        or not all(isinstance(strategy, str) for strategy in strategies)
        or not isinstance(seeds, list)
        or not all(isinstance(seed, int) for seed in seeds)
    ):
        msg = "scope kind/models/strategies/seeds have invalid structure"
        raise ManifestError(msg)

    expected_candidates = {
        (strategy, seed) for strategy in strategies for seed in seeds
    }
    actual_models: list[str] = []
    for index, raw_model in enumerate(models):
        if not isinstance(raw_model, dict):
            msg = f"models[{index}] must be an object"
            raise ManifestError(msg)
        model = cast("dict[str, Any]", raw_model)
        name = model.get("model")
        candidates = model.get("candidates")
        if (
            not isinstance(name, str)
            or not isinstance(model.get("kind"), str)
            or not isinstance(model.get("traits"), list)
            or not all(isinstance(trait, str) for trait in model["traits"])
            or not isinstance(model.get("expect_min_buildable"), int)
            or not isinstance(model.get("input_hash"), str)
            or not isinstance(candidates, list)
        ):
            msg = f"models[{index}] has invalid required fields"
            raise ManifestError(msg)
        actual_models.append(name)
        actual_candidates: list[tuple[str, int]] = []
        for candidate_index, raw_candidate in enumerate(candidates):
            if not isinstance(raw_candidate, dict):
                msg = f"models[{index}].candidates[{candidate_index}] must be an object"
                raise ManifestError(msg)
            strategy = raw_candidate.get("strategy")
            seed = raw_candidate.get("seed")
            if (
                not isinstance(strategy, str)
                or not isinstance(seed, int)
                or not isinstance(raw_candidate.get("config_hash"), str)
                or not isinstance(raw_candidate.get("artifact"), str)
            ):
                msg = (
                    f"models[{index}].candidates[{candidate_index}] has invalid "
                    "required fields"
                )
                raise ManifestError(msg)
            actual_candidates.append((strategy, seed))
        if (
            len(actual_candidates) != len(expected_candidates)
            or set(actual_candidates) != expected_candidates
        ):
            msg = f"model {name!r} does not contain the complete candidate matrix"
            raise ManifestError(msg)

    if len(actual_models) != len(scope_models) or set(actual_models) != set(
        scope_models
    ):
        msg = "models entries do not match the complete scope model set"
        raise ManifestError(msg)
    return manifest


def _artifact_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else _REPO / path


def _load_candidate(
    candidate_entry: dict[str, Any],
    model_entry: dict[str, Any],
    identity: dict[str, Any],
) -> tuple[Candidate | None, str | None]:
    artifact_raw = candidate_entry.get("artifact")
    label = (
        f"{model_entry['model']}/{candidate_entry['strategy']}"
        f"/seed-{candidate_entry['seed']}"
    )
    if not artifact_raw:
        return None, f"{label}: missing artifact path"
    path = _artifact_path(str(artifact_raw))
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
    evaluator: _Evaluator,
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    identity = manifest["identity"]
    for model_entry in manifest["models"]:
        candidates: list[Candidate] = []
        for candidate_entry in model_entry["candidates"]:
            candidate, error = _load_candidate(
                candidate_entry=candidate_entry,
                model_entry=model_entry,
                identity=identity,
            )
            if error is not None:
                errors.append(error)
            elif candidate is not None:
                candidates.append(candidate)
        if len(candidates) != len(model_entry["candidates"]):
            continue
        report = select_best(candidates)
        model = _ModelSummary(
            name=model_entry["model"],
            kind=model_entry["kind"],
            traits=tuple(model_entry["traits"]),
            expect_min_buildable=model_entry["expect_min_buildable"],
        )
        row = evaluator.build_row(
            model=model,
            report_dict=report.to_dict(),
            status="ok" if report.winner is not None else "error: all failed",
        )
        row["unsupported_ratio"] = model_entry.get("unsupported_ratio")
        rows.append(row)
    return rows, errors


def _canonical_scope(
    evaluator: _Evaluator,
    manifest: dict[str, Any],
) -> bool:
    scope = manifest["scope"]
    corpus = evaluator.load_corpus_module()
    expected_models = {
        model.name for model in corpus.load_manifest() if model.kind == scope["kind"]
    }
    return (
        set(scope["models"]) == expected_models
        and set(scope["strategies"]) == set(strategy_names())
        and scope["seeds"] == [0]
    )


def _baseline_path(
    evaluator: _Evaluator,
    manifest: dict[str, Any],
    explicit: Path | None,
) -> Path:
    if explicit is not None:
        return explicit
    return (
        evaluator.BASELINE
        if manifest["scope"]["kind"] == "synthetic"
        else evaluator.MESH_BASELINE
    )


def _assess(rows: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """Return failure status and rows eligible for baseline comparison."""
    successful = [row for row in rows if row["status"] == "ok"]
    failed = [row["model"] for row in rows if row["status"] != "ok"]
    failed.extend(row["model"] for row in successful if not row["expectation_ok"])
    for model in failed:
        print(f"evaluation failure: {model}")
    return int(bool(failed)), successful


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse assembler command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("--out", type=Path, default=_ASSEMBLED)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--tolerance", type=float, default=0.05)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    """Validate one collection and assemble its scorecard."""
    args = parse_args(argv)
    evaluator = _load_evaluator()
    try:
        payload = json.loads(args.collection.read_text())
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read collection: {error}", file=sys.stderr)
        return 1
    try:
        manifest = _validate_manifest(payload)
    except ManifestError as error:
        print(f"error: malformed collection manifest: {error}", file=sys.stderr)
        return 1
    rows, errors = _rows(evaluator=evaluator, manifest=manifest)
    if errors:
        print("collection is incomplete:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    if args.write_baseline and not _canonical_scope(
        evaluator=evaluator,
        manifest=manifest,
    ):
        print(
            "error: baseline assembly requires the full kind, every strategy, "
            "and seed 0",
            file=sys.stderr,
        )
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = _payload(manifest, rows, stamp)
    out_dir = args.out / str(manifest["collection_id"])
    atomic_json(out_dir / "scorecard.json", payload)
    (out_dir / "scorecard.md").write_text(evaluator.to_markdown(rows) + "\n")
    print(f"wrote {out_dir / 'scorecard.json'}")
    print(evaluator.to_markdown(rows))

    exit_code, successful_rows = _assess(rows)
    seeds = manifest["scope"]["seeds"]
    baseline = _baseline_path(
        evaluator=evaluator,
        manifest=manifest,
        explicit=args.baseline,
    )
    comparable_scope = len(seeds) == 1 and set(manifest["scope"]["strategies"]) == set(
        strategy_names()
    )
    if not args.write_baseline and comparable_scope and baseline.exists():
        known = json.loads(baseline.read_text())["models"]
        hard, info = evaluator.compare_to_baseline(
            rows=successful_rows,
            baseline_rows=known,
            tolerance=args.tolerance,
        )
        for line in info:
            print(f"note: {line}")
        for line in hard:
            print(f"REGRESSION: {line}")
        exit_code = max(exit_code, int(bool(hard)))
    if args.write_baseline:
        if exit_code:
            print("baseline not written because assembly failed", file=sys.stderr)
        else:
            atomic_json(baseline, payload)
            print(f"wrote {baseline}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
