"""One-shot corpus evaluation: generate, collect, and assemble.

``corpus evaluate`` sweeps every currently available input — synthetic
shapes always (missing ``.npy`` files are regenerated first) and
downloaded meshes only when present and hash-valid. Unavailable meshes
are recorded as skipped, never as failures. The collection reuses the
resumable machinery in :mod:`legolization.corpus.collect` (multi-seed
behavior included) and each kind's scorecard is assembled with
:mod:`legolization.corpus.assemble`. Evaluation never writes a
baseline; that mutation belongs exclusively to
``corpus assemble --write-baseline``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from legolization.corpus import collect, ops
from legolization.corpus.assemble import assemble_collection
from legolization.corpus.manifest import load_manifest
from legolization.corpus.storage import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from legolization.corpus.manifest import CorpusModel

_KINDS = ("synthetic", "mesh")


@dataclass(frozen=True, slots=True, kw_only=True)
class SkippedMesh:
    """A mesh input left out of the sweep, with its availability reason."""

    name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        """Return the JSON payload for this skip record."""
        return {"name": self.name, "reason": self.reason}


@dataclass(frozen=True, slots=True, kw_only=True)
class KindEvaluation:
    """Collection plus assembly results for one corpus kind."""

    kind: str
    models: tuple[str, ...]
    collection_path: Path
    collection_complete: bool
    incomplete: tuple[str, ...] = ()
    scorecard_path: Path | None = None
    markdown_path: Path | None = None
    rows: tuple[dict[str, Any], ...] = ()
    failures: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this kind's evaluation."""
        return {
            "kind": self.kind,
            "models": list(self.models),
            "collection": str(self.collection_path),
            "collection_complete": self.collection_complete,
            "incomplete": list(self.incomplete),
            "scorecard": (
                str(self.scorecard_path) if self.scorecard_path is not None else None
            ),
            "rows": [
                {
                    "model": row["model"],
                    "status": row["status"],
                    "winner": row["winner"],
                    "buildable_count": row["buildable_count"],
                    "expectation_ok": row["expectation_ok"],
                }
                for row in self.rows
            ],
            "failures": list(self.failures),
            "regressions": list(self.regressions),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluateResult:
    """Summary of one full evaluate operation."""

    generated: tuple[str, ...]
    generation_errors: tuple[str, ...]
    skipped: tuple[SkippedMesh, ...]
    runs: tuple[KindEvaluation, ...]

    @property
    def operational_errors(self) -> tuple[str, ...]:
        """Mechanical failures: broken generation or unassemblable runs."""
        return self.generation_errors + tuple(
            f"{run.kind}: collection could not be assembled "
            f"({len(run.incomplete)} candidate problem(s))"
            for run in self.runs
            if run.incomplete
        )

    @property
    def failures(self) -> tuple[str, ...]:
        """Evaluation and expectation failures across every kind."""
        return tuple(
            f"{run.kind}: {model}" for run in self.runs for model in run.failures
        )

    @property
    def regressions(self) -> tuple[str, ...]:
        """Baseline regressions across every kind."""
        return tuple(
            f"{run.kind}: {line}" for run in self.runs for line in run.regressions
        )

    @property
    def exit_code(self) -> int:
        """0 complete; 1 operational; 2 expectation failure or regression."""
        if self.operational_errors:
            return 1
        if self.failures or self.regressions:
            return 2
        return 0

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this result."""
        return {
            "generated": list(self.generated),
            "generation_errors": list(self.generation_errors),
            "skipped": [skip.to_dict() for skip in self.skipped],
            "runs": [run.to_dict() for run in self.runs],
            "operational_errors": list(self.operational_errors),
            "failures": list(self.failures),
            "regressions": list(self.regressions),
            "exit_code": self.exit_code,
        }


def _mesh_unavailable_reason(model: CorpusModel) -> str | None:
    """Why a mesh cannot join the sweep, or None when it is usable."""
    if model.sha256 is None:
        return "manifest is missing sha256"
    if not model.abs_path.exists():
        return "not downloaded"
    if sha256_of(model.abs_path) != model.sha256:
        return "sha256 mismatch (stale or corrupt download)"
    return None


def _generate_missing(
    models: Sequence[CorpusModel],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Regenerate synthetic inputs whose files are missing from storage."""
    missing = [
        model
        for model in models
        if model.kind == "synthetic" and not model.abs_path.exists()
    ]
    if not missing:
        return (), ()
    reports = ops.generate(list(missing))
    generated = tuple(report.name for report in reports if report.ok)
    errors = tuple(
        f"{report.name}: {report.detail}" for report in reports if not report.ok
    )
    return generated, errors


def _collect_argv(  # noqa: PLR0913 - mirrors the collect CLI surface
    *,
    kind: str,
    strategies: str | None,
    jobs: int,
    timeout: float,
    seed: int,
    seeds: tuple[int, ...] | None,
    out: Path | None,
    fresh: bool,
) -> list[str]:
    """Build the collect argv for one kind, so defaults stay in one place."""
    argv = [
        "--kind",
        kind,
        "--jobs",
        str(jobs),
        "--timeout",
        str(timeout),
        "--seed",
        str(seed),
    ]
    if strategies is not None:
        argv += ["--strategies", strategies]
    if seeds is not None:
        argv += ["--seeds", ",".join(str(value) for value in seeds)]
    if out is not None:
        argv += ["--out", str(out)]
    if fresh:
        argv.append("--fresh")
    return argv


def _evaluate_kind(
    models: Sequence[CorpusModel],
    argv: list[str],
    *,
    out: Path,
) -> KindEvaluation:
    """Collect one kind's models and assemble their scorecard."""
    run = collect.run_collection(models, collect.parse_args(argv))
    outcome = assemble_collection(run.manifest_path, out=out)
    return KindEvaluation(
        kind=str(models[0].kind),
        models=tuple(model.name for model in models),
        collection_path=run.manifest_path,
        collection_complete=run.complete,
        incomplete=outcome.incomplete,
        scorecard_path=outcome.scorecard_path,
        markdown_path=outcome.markdown_path,
        rows=tuple(outcome.rows),
        failures=outcome.failures,
        regressions=outcome.regressions,
        notes=outcome.notes,
    )


def evaluate(  # noqa: PLR0913 - the scoped-down evaluate surface
    *,
    kind: str | None = None,
    strategies: str | None = None,
    jobs: int = 0,
    timeout: float = 300.0,
    seed: int = 0,
    seeds: tuple[int, ...] | None = None,
    out: Path | None = None,
    fresh: bool = False,
    progress: Callable[[str], None] | None = None,
) -> EvaluateResult:
    """Evaluate every currently available corpus input in one operation.

    Synthetic inputs are always in scope (files regenerate on demand);
    meshes join only when downloaded and hash-valid, and are otherwise
    recorded as skipped. There is deliberately no way to write a
    baseline from here.
    """
    manifest = load_manifest()
    kinds = _KINDS if kind is None else (kind,)
    selected = [model for model in manifest if model.kind in kinds]
    generated, generation_errors = (
        _generate_missing(selected) if "synthetic" in kinds else ((), ())
    )
    skipped = tuple(
        SkippedMesh(name=model.name, reason=reason)
        for model in selected
        if model.kind == "mesh"
        and (reason := _mesh_unavailable_reason(model)) is not None
    )
    skipped_names = {skip.name for skip in skipped}
    runs: list[KindEvaluation] = []
    for kind_name in kinds:
        scoped = [
            model
            for model in selected
            if model.kind == kind_name and model.name not in skipped_names
        ]
        if not scoped:
            continue
        if progress is not None:
            progress(f"evaluating {len(scoped)} {kind_name} model(s)")
        argv = _collect_argv(
            kind=kind_name,
            strategies=strategies,
            jobs=jobs,
            timeout=timeout,
            seed=seed,
            seeds=seeds,
            out=out,
            fresh=fresh,
        )
        effective_out = out if out is not None else collect.RUNS
        runs.append(_evaluate_kind(scoped, argv, out=effective_out))
    return EvaluateResult(
        generated=generated,
        generation_errors=generation_errors,
        skipped=skipped,
        runs=tuple(runs),
    )
