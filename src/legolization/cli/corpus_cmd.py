"""The ``corpus`` command group: evaluation corpus workflows.

Inputs live in platform user-data storage; collections and scorecards
default to ``./legolization-eval/runs/``. Baselines change only through
``corpus assemble --write-baseline`` — ``corpus evaluate`` cannot write
one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from legolization.cli.common import add_json_option
from legolization.cli.envelope import ArtifactRecord, ErrorRecord, ResultEnvelope
from legolization.cli.exit_codes import (
    COMPLETE,
    OPERATIONAL_ERROR,
    UNBUILDABLE,
    CliUsageError,
)

if TYPE_CHECKING:
    from legolization.corpus.assemble import AssembleOutcome
    from legolization.corpus.evaluate import EvaluateResult
    from legolization.corpus.ops import ModelReport


def _seed_list(value: str) -> tuple[int, ...]:
    """Parse a comma-separated seed list for argparse."""
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        msg = f"{value!r} must be comma-separated integers"
        raise argparse.ArgumentTypeError(msg)
    try:
        return tuple(int(part) for part in parts)
    except ValueError as error:
        msg = f"{value!r} must be comma-separated integers"
        raise argparse.ArgumentTypeError(msg) from error


def _add_models_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--models",
        default=None,
        metavar="NAME,...",
        help="restrict to these manifest model names",
    )


def _add_traits_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--traits",
        default=None,
        metavar="TRAIT,...",
        help="restrict to models carrying any of these traits",
    )


def _add_sweep_options(parser: argparse.ArgumentParser) -> None:
    """Register the shared collection-scope flags."""
    parser.add_argument(
        "--strategies",
        default=None,
        metavar="NAME,...",
        help="placement strategies to run (default: all)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="parallel candidate workers (0 = one per candidate, CPU-capped)",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--seeds",
        type=_seed_list,
        default=None,
        metavar="N,N,...",
        help="run every strategy once per seed",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="run output root (default: ./legolization-eval/runs)",
    )


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``corpus`` operations and handlers."""
    operations = parser.add_subparsers(
        dest="corpus_command",
        metavar="OPERATION",
        required=True,
    )

    listing = operations.add_parser(
        "list",
        help="list corpus models and their availability",
    )
    add_json_option(listing)
    listing.set_defaults(handler=_run_list, command_name="corpus list")

    generate = operations.add_parser(
        "generate",
        help="generate synthetic corpus inputs into user-data storage",
    )
    _add_models_option(generate)
    _add_traits_option(generate)
    add_json_option(generate)
    generate.set_defaults(handler=_run_generate, command_name="corpus generate")

    download = operations.add_parser(
        "download",
        help="download pinned corpus meshes into user-data storage",
    )
    _add_models_option(download)
    add_json_option(download)
    download.set_defaults(handler=_run_download, command_name="corpus download")

    verify = operations.add_parser(
        "verify",
        help="verify corpus inputs against their pinned hashes and generators",
    )
    add_json_option(verify)
    verify.set_defaults(handler=_run_verify, command_name="corpus verify")

    collect = operations.add_parser(
        "collect",
        help="run placement strategies and collect resumable candidate artifacts",
    )
    _add_models_option(collect)
    _add_traits_option(collect)
    collect.add_argument(
        "--kind",
        choices=("mesh", "synthetic"),
        default="synthetic",
        help="corpus kind to sweep (default: synthetic; mesh is opt-in)",
    )
    _add_sweep_options(collect)
    collect.add_argument("--timeout", type=float, default=300.0)
    collect.add_argument(
        "--fresh",
        action="store_true",
        help="ignore exact successful artifacts and rerun selected candidates",
    )
    add_json_option(collect)
    collect.set_defaults(handler=_run_collect, command_name="corpus collect")

    assemble = operations.add_parser(
        "assemble",
        help="assemble a collected run into a scorecard and diff the baseline",
    )
    assemble.add_argument(
        "--runs",
        type=Path,
        default=None,
        help=(
            "runs root to pick the newest collection from, or one "
            "collection.json (default: ./legolization-eval/runs)"
        ),
    )
    assemble.add_argument(
        "--out",
        type=Path,
        default=None,
        help="scorecard output root (default: ./legolization-eval/runs)",
    )
    assemble.add_argument("--baseline", type=Path, default=None)
    assemble.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "replace the canonical baseline; requires the full-kind, "
            "every-strategy, seed-0 scope and a failure-free evaluation"
        ),
    )
    assemble.add_argument("--tolerance", type=float, default=0.05)
    add_json_option(assemble)
    assemble.set_defaults(handler=_run_assemble, command_name="corpus assemble")

    evaluate = operations.add_parser(
        "evaluate",
        help="collect and assemble every currently available corpus input",
    )
    evaluate.add_argument(
        "--kind",
        choices=("mesh", "synthetic"),
        default=None,
        help="restrict to one corpus kind (default: both)",
    )
    _add_sweep_options(evaluate)
    add_json_option(evaluate)
    evaluate.set_defaults(handler=_run_evaluate, command_name="corpus evaluate")


def _reports_envelope(
    command: str,
    reports: list[ModelReport],
    *,
    json_mode: bool,
    artifacts: tuple[ArtifactRecord, ...] = (),
) -> ResultEnvelope:
    """Fold per-model operation reports into one envelope."""
    if not json_mode:
        for report in reports:
            print(report.line)
    failed = [report for report in reports if not report.ok]
    error = None
    if failed:
        error = ErrorRecord(
            type="CorpusOperationError",
            message=(
                f"{len(failed)} corpus model(s) failed: "
                f"{', '.join(report.name for report in failed)}"
            ),
        )
    return ResultEnvelope(
        command=command,
        exit_code=OPERATIONAL_ERROR if failed else COMPLETE,
        artifacts=artifacts,
        error=error,
        data={"models": [report.to_dict() for report in reports]},
    )


def _run_list(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.corpus import ops  # noqa: PLC0415 - heavy import stays lazy
    from legolization.corpus.manifest import load_manifest  # noqa: PLC0415

    reports = ops.list_models(load_manifest())
    if not args.json:
        print(ops.list_table(reports))
    return ResultEnvelope(
        command="corpus list",
        exit_code=COMPLETE,
        data={"models": [report.to_dict() for report in reports]},
    )


def _run_generate(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.corpus import ops  # noqa: PLC0415 - heavy import stays lazy
    from legolization.corpus.manifest import (  # noqa: PLC0415
        load_manifest,
        select_scope,
    )

    models = select_scope(load_manifest(), names=args.models, traits=args.traits)
    reports = ops.generate(models)
    artifacts = tuple(
        ArtifactRecord(path=report.path, kind="corpus-input")
        for report in reports
        if report.status == "generated" and report.path is not None
    )
    return _reports_envelope(
        "corpus generate",
        reports,
        json_mode=args.json,
        artifacts=artifacts,
    )


def _run_download(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.corpus import ops  # noqa: PLC0415 - heavy import stays lazy
    from legolization.corpus.manifest import load_manifest  # noqa: PLC0415

    reports = ops.download(
        load_manifest(),
        only=args.models,
        progress=lambda message: print(message, file=sys.stderr),
    )
    artifacts = tuple(
        ArtifactRecord(path=report.path, kind="corpus-input")
        for report in reports
        if report.ok and report.path is not None
    )
    return _reports_envelope(
        "corpus download",
        reports,
        json_mode=args.json,
        artifacts=artifacts,
    )


def _run_verify(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.corpus import ops  # noqa: PLC0415 - heavy import stays lazy
    from legolization.corpus.manifest import load_manifest  # noqa: PLC0415

    return _reports_envelope(
        "corpus verify",
        ops.verify(load_manifest()),
        json_mode=args.json,
    )


def _run_collect(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.corpus import collect  # noqa: PLC0415 - heavy import stays lazy
    from legolization.corpus.manifest import (  # noqa: PLC0415
        load_manifest,
        select_scope,
    )

    if args.out is None:
        args.out = collect.RUNS
    models = select_scope(
        load_manifest(),
        names=args.models,
        traits=args.traits,
        kind=args.kind,
    )
    run = collect.run_collection(models, args)
    statuses = {
        str(entry["model"]): str(entry["status"])
        for entry in cast("list[dict[str, object]]", run.manifest["models"])
    }
    if not args.json:
        for name, status in statuses.items():
            print(f"{name}: {status}")
        print(f"collection manifest: {run.manifest_path}")
    error = None
    if not run.complete:
        unfinished = [name for name, status in statuses.items() if status != "ok"]
        error = ErrorRecord(
            type="CorpusCollectionError",
            message=f"collection incomplete: {', '.join(unfinished)}",
        )
    return ResultEnvelope(
        command="corpus collect",
        exit_code=COMPLETE if run.complete else OPERATIONAL_ERROR,
        artifacts=(
            ArtifactRecord(path=str(run.manifest_path), kind="collection-manifest"),
        ),
        error=error,
        data={
            "collection": str(run.manifest_path),
            "collection_id": str(run.manifest["collection_id"]),
            "complete": run.complete,
            "models": cast("dict[str, object]", statuses),
        },
    )


def _resolve_collection(runs: Path) -> Path:
    """Resolve ``--runs`` to one collection manifest path."""
    if runs.is_file():
        return runs
    candidates = sorted(runs.glob("*/collection.json"))
    if not candidates:
        msg = f"no collection manifests under {runs}"
        raise CliUsageError(msg)
    return candidates[-1]


def _print_assemble(outcome: AssembleOutcome) -> None:
    """Print the human-readable assembly summary."""
    from legolization.corpus.collect import to_markdown  # noqa: PLC0415

    if outcome.incomplete:
        print("collection is incomplete:", file=sys.stderr)
        for line in outcome.incomplete:
            print(f"  {line}", file=sys.stderr)
        return
    if outcome.scope_refusal is not None:
        return
    print(f"wrote {outcome.scorecard_path}")
    print(to_markdown(outcome.rows))
    for model in outcome.failures:
        print(f"evaluation failure: {model}")
    for line in outcome.notes:
        print(f"note: {line}")
    for line in outcome.regressions:
        print(f"REGRESSION: {line}")
    if outcome.baseline_written:
        print(f"wrote {outcome.baseline_path}")


def _assemble_envelope(outcome: AssembleOutcome) -> ResultEnvelope:
    """Fold one assembly outcome into the ``corpus assemble`` envelope."""
    artifacts = tuple(
        ArtifactRecord(path=str(path), kind=kind)
        for path, kind in (
            (outcome.scorecard_path, "scorecard"),
            (outcome.markdown_path, "scorecard-markdown"),
            (outcome.baseline_path if outcome.baseline_written else None, "baseline"),
        )
        if path is not None
    )
    warnings = tuple(f"note: {line}" for line in outcome.notes)
    if outcome.baseline_refusal is not None:
        warnings = (*warnings, outcome.baseline_refusal)
    error = None
    exit_code = COMPLETE
    if (operational := outcome.operational_error) is not None:
        exit_code = OPERATIONAL_ERROR
        error = ErrorRecord(
            type="CorpusAssemblyError",
            message=operational,
            detail=(
                {"incomplete": list(outcome.incomplete)} if outcome.incomplete else None
            ),
        )
    elif outcome.failures or outcome.regressions:
        exit_code = UNBUILDABLE
        problems = [f"evaluation failure: {model}" for model in outcome.failures]
        problems.extend(f"regression: {line}" for line in outcome.regressions)
        error = ErrorRecord(
            type="CorpusEvaluationFailure",
            message="; ".join(problems),
        )
    return ResultEnvelope(
        command="corpus assemble",
        exit_code=exit_code,
        artifacts=artifacts,
        warnings=warnings,
        error=error,
        data={
            "collection": str(outcome.collection),
            "scorecard": (
                str(outcome.scorecard_path)
                if outcome.scorecard_path is not None
                else None
            ),
            "models": [
                {
                    "model": row["model"],
                    "status": row["status"],
                    "winner": row["winner"],
                    "buildable_count": row["buildable_count"],
                    "expectation_ok": row["expectation_ok"],
                }
                for row in outcome.rows
            ],
            "failures": list(outcome.failures),
            "regressions": list(outcome.regressions),
            "notes": list(outcome.notes),
            "baseline": (
                str(outcome.baseline_path)
                if outcome.baseline_path is not None
                else None
            ),
            "baseline_written": outcome.baseline_written,
        },
    )


def _run_assemble(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.corpus import assemble, collect  # noqa: PLC0415 - lazy

    runs_root = args.runs if args.runs is not None else collect.RUNS
    collection = _resolve_collection(runs_root)
    outcome = assemble.assemble_collection(
        collection,
        out=args.out if args.out is not None else collect.RUNS,
        baseline=args.baseline,
        write_baseline=args.write_baseline,
        tolerance=args.tolerance,
    )
    if not args.json:
        _print_assemble(outcome)
    return _assemble_envelope(outcome)


def _print_evaluate(result: EvaluateResult) -> None:
    """Print the human-readable evaluation summary."""
    from legolization.corpus.collect import to_markdown  # noqa: PLC0415

    for name in result.generated:
        print(f"generated {name}")
    for skip in result.skipped:
        print(f"skipped {skip.name}: {skip.reason}")
    for run in result.runs:
        print(f"=== {run.kind}")
        print(f"collection: {run.collection_path}")
        if run.incomplete:
            print("collection could not be assembled:", file=sys.stderr)
            for line in run.incomplete:
                print(f"  {line}", file=sys.stderr)
            continue
        print(f"scorecard: {run.scorecard_path}")
        print(to_markdown(list(run.rows)))
        for model in run.failures:
            print(f"evaluation failure: {model}")
        for line in run.notes:
            print(f"note: {line}")
        for line in run.regressions:
            print(f"REGRESSION: {line}")


def _run_evaluate(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.corpus import evaluate  # noqa: PLC0415 - heavy import stays lazy

    result = evaluate.evaluate(
        kind=args.kind,
        strategies=args.strategies,
        jobs=args.jobs,
        seed=args.seed,
        seeds=args.seeds,
        out=args.out,
        progress=lambda message: print(message, file=sys.stderr),
    )
    if not args.json:
        _print_evaluate(result)
    artifacts = tuple(
        ArtifactRecord(path=str(run.scorecard_path), kind="scorecard")
        for run in result.runs
        if run.scorecard_path is not None
    ) + tuple(
        ArtifactRecord(path=str(run.collection_path), kind="collection-manifest")
        for run in result.runs
    )
    error = None
    if result.exit_code == OPERATIONAL_ERROR:
        error = ErrorRecord(
            type="CorpusEvaluationError",
            message="; ".join(result.operational_errors),
        )
    elif result.exit_code == UNBUILDABLE:
        problems = [f"evaluation failure: {name}" for name in result.failures]
        problems.extend(f"regression: {line}" for line in result.regressions)
        error = ErrorRecord(
            type="CorpusEvaluationFailure",
            message="; ".join(problems),
        )
    return ResultEnvelope(
        command="corpus evaluate",
        exit_code=result.exit_code,
        artifacts=artifacts,
        warnings=tuple(
            f"skipped {skip.name}: {skip.reason}" for skip in result.skipped
        ),
        error=error,
        data=result.to_dict(),
    )


__all__ = ["configure"]
