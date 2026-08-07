"""The approved material-ladder retry for initially unbuildable results.

The ladder is fixed: a four-plate shell, a six-plate shell, then solid.
It never silently rescales or drops components — rung configurations
are produced only through the strict override parser, so nothing else
can change. The total retry budget is shared fairly between rungs via
cumulative soft deadlines, so a rung finishing early hands its unused
time to the next; the run stops at the first buildable rung.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from legolization.bundle.identity import bundle_identity
from legolization.bundle.paths import resolve_output_dir
from legolization.bundle.record import (
    BundleRecord,
    read_identity_key,
    source_payload,
    versions_payload,
    write_record,
)
from legolization.cli.envelope import ArtifactRecord, ResultEnvelope
from legolization.cli.exit_codes import COMPLETE, UNBUILDABLE

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from legolization.bundle.identity import BundleIdentity
    from legolization.bundle.orchestrator import BundleRequest


@dataclass(frozen=True, slots=True, kw_only=True)
class MaterialRung:
    """One rung of the material ladder as strict config overrides."""

    name: str
    overrides: dict[str, object]


LADDER: tuple[MaterialRung, ...] = (
    MaterialRung(
        name="four-plate",
        overrides={"geometry.shell_plates": 4, "geometry.hollow": True},
    ),
    MaterialRung(
        name="six-plate",
        overrides={"geometry.shell_plates": 6, "geometry.hollow": True},
    ),
    MaterialRung(name="solid", overrides={"geometry.hollow": False}),
)

_PROMOTED_DIRS = ("model", "bom")

type BundleRunner = Callable[["BundleRequest"], ResultEnvelope]


def run_retry(
    request: BundleRequest,
    *,
    total_budget_s: float,
    runner: BundleRunner | None = None,
) -> ResultEnvelope:
    """Run the material ladder inside one identity-stamped retry bundle."""
    from legolization.bundle.orchestrator import run_bundle  # noqa: PLC0415
    from legolization.configuration import (  # noqa: PLC0415
        mapping_hash,
        merge_overrides,
    )

    effective_runner = run_bundle if runner is None else runner
    invocation = {**request.invocation_payload(), "retry": "material-ladder"}
    identity = bundle_identity(
        request.input_path,
        request.config,
        invocation=invocation,
    )
    decision = resolve_output_dir(
        request.input_path,
        request.output_dir,
        flavor="legolization",
        identity_key=identity.key(),
        fresh=request.fresh,
        read_key=read_identity_key,
    )
    directory = decision.directory
    directory.mkdir(parents=True, exist_ok=True)
    record = _retry_record(request, identity, directory)
    started = request.clock()
    rung_reports: list[dict[str, Any]] = []
    winner: tuple[MaterialRung, ResultEnvelope] | None = None
    share = total_budget_s / len(LADDER)
    for index, rung in enumerate(LADDER):
        deadline = started + (index + 1) * share
        remaining = deadline - request.clock()
        if remaining <= 0:
            rung_reports.append(
                {"name": rung.name, "status": "skipped", "reason": "budget exhausted"}
            )
            continue
        rung_config = merge_overrides(request.config, dict(rung.overrides))
        envelope = effective_runner(
            replace(
                request,
                config=rung_config,
                output_dir=directory / "rungs" / rung.name,
                duration_s=remaining,
                cancel_pending=False,
            )
        )
        rung_reports.append(
            {
                "name": rung.name,
                "status": "buildable" if envelope.exit_code == COMPLETE else "failed",
                "exit_code": envelope.exit_code,
                "config_sha256": mapping_hash(rung_config.to_dict()),
            }
        )
        if envelope.exit_code == COMPLETE:
            winner = (rung, envelope)
            break
    stage = record.stage("retry")
    stage.detail["rungs"] = rung_reports
    stage.detail["total_budget_s"] = total_budget_s
    stage.status = "complete"
    if winner is not None:
        rung, _ = winner
        _promote_rung(record, directory, rung)
        record.verdicts.update(
            {"buildable": True, "winner": {"strategy": "retry", "rung": rung.name}}
        )
        record.status = "complete"
        record.exit_code = COMPLETE
    else:
        record.verdicts.update({"buildable": False, "winner": None})
        record.status = "unbuildable"
        record.exit_code = UNBUILDABLE
    write_record(record, directory)
    return _envelope(record, directory, rung_reports)


def _retry_record(
    request: BundleRequest,
    identity: BundleIdentity,
    directory: Path,
) -> BundleRecord:
    from legolization.bundle.identity import (  # noqa: PLC0415
        result_affecting_config,
    )
    from legolization.bundle.record import read_record  # noqa: PLC0415
    from legolization.configuration import mapping_hash  # noqa: PLC0415

    if (existing := read_record(directory)) is not None:
        existing.status = "in-progress"
        return existing
    values = result_affecting_config(
        request.config,
        invocation={**request.invocation_payload(), "retry": "material-ladder"},
    )
    return BundleRecord(
        identity=identity,
        source=source_payload(
            request.input_path,
            bundle_dir=directory,
            sha256=identity.input_sha256,
        ),
        configuration={"sha256": mapping_hash(values), "values": values},
        versions=versions_payload(),
        quality=request.quality,
    )


def _promote_rung(record: BundleRecord, directory: Path, rung: MaterialRung) -> None:
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415

    rung_dir = directory / "rungs" / rung.name
    for child in _PROMOTED_DIRS:
        source = rung_dir / child
        if not source.is_dir():
            continue
        target = directory / child
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        for path in sorted(target.rglob("*")):
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                record.record_artifact(
                    path=relative,
                    stage="retry",
                    kind=child,
                    sha256=input_sha256(path),
                )


def _envelope(
    record: BundleRecord,
    directory: Path,
    rung_reports: list[dict[str, Any]],
) -> ResultEnvelope:
    return ResultEnvelope(
        command="bundle",
        exit_code=record.exit_code or 0,
        artifacts=tuple(
            ArtifactRecord(
                path=str(directory / entry.path),
                kind=entry.kind,
                sha256=entry.sha256,
            )
            for entry in record.artifacts
        ),
        warnings=tuple(record.warnings),
        data={
            "bundle_dir": str(directory),
            "status": record.status,
            "retry": "material-ladder",
            "rungs": rung_reports,
            "verdicts": dict(record.verdicts),
        },
    )
