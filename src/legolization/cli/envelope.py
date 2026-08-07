"""The one-envelope JSON result contract for ``--json`` output.

Under ``--json``, stdout contains exactly one envelope; progress and
warnings go to stderr. Errors still emit the same envelope on stdout.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from legolization.cli.exit_codes import (
    exit_code_for_exception,
    status_for_exit_code,
)
from legolization.version import package_version

if TYPE_CHECKING:
    from typing import TextIO

RESULT_SCHEMA = "legolization.result/v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactRecord:
    """One artifact produced by a command; ``path`` is never absolute."""

    path: str
    kind: str
    sha256: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Return the JSON payload for this artifact."""
        payload = {"path": self.path, "kind": self.kind}
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorRecord:
    """Machine-readable description of a command failure."""

    type: str
    message: str
    detail: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this error."""
        payload: dict[str, object] = {"type": self.type, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultEnvelope:
    """The single result object every command reports."""

    command: str
    exit_code: int
    artifacts: tuple[ArtifactRecord, ...] = ()
    warnings: tuple[str, ...] = ()
    error: ErrorRecord | None = None
    data: dict[str, object] | None = None
    version: str = field(default_factory=package_version)

    @property
    def status(self) -> str:
        """Machine-readable status derived from the exit code."""
        return status_for_exit_code(self.exit_code)

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload; null ``error``/``data`` are omitted."""
        payload: dict[str, object] = {
            "schema": RESULT_SCHEMA,
            "version": self.version,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "warnings": list(self.warnings),
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        if self.data is not None:
            payload["data"] = self.data
        return payload


def envelope_for_error(
    command: str,
    error: BaseException,
    *,
    warnings: tuple[str, ...] = (),
) -> ResultEnvelope:
    """Build the failure envelope for an exception."""
    return ResultEnvelope(
        command=command,
        exit_code=exit_code_for_exception(error),
        warnings=warnings,
        error=ErrorRecord(
            type=type(error).__name__,
            message=str(error) or type(error).__name__,
        ),
    )


def emit(envelope: ResultEnvelope, *, stream: TextIO | None = None) -> None:
    """Write the envelope as the sole JSON object on the stream."""
    target = sys.stdout if stream is None else stream
    json.dump(envelope.to_dict(), target, indent=2, sort_keys=True)
    target.write("\n")
