"""The authoritative ``bundle.json`` record with atomic, validated IO."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, cast

from jsonschema import Draft202012Validator

from legolization.bundle.identity import BundleIdentity
from legolization.errors import ManifestError
from legolization.version import package_version

if TYPE_CHECKING:
    from collections.abc import Mapping

BUNDLE_SCHEMA = "legolization.bundle/v1"
BUNDLE_SCHEMA_PATH = Path(__file__).parent.parent / "data" / "bundle-v1.schema.json"
BUNDLE_FILENAME = "bundle.json"

type BundleStatus = Literal[
    "in-progress",
    "complete",
    "partial",
    "unbuildable",
    "error",
    "cancelled",
    "interrupted",
]
type StageStatus = Literal[
    "pending",
    "running",
    "complete",
    "partial",
    "failed",
    "skipped",
    "cancelled",
    "interrupted",
]

_MAX_RELATIVE_ASCENTS = 2
_LIBRARY_DISTRIBUTIONS = ("numpy", "scipy", "trimesh", "pyldraw3")


@dataclass(slots=True, kw_only=True)
class StageRecord:
    """Status, warnings, and artifacts of one bundle stage."""

    status: StageStatus = "pending"
    warnings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this stage."""
        payload: dict[str, Any] = {
            "status": self.status,
            "warnings": list(self.warnings),
            "artifacts": list(self.artifacts),
            "detail": dict(self.detail),
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rehydrate a stage record from its JSON payload."""
        return cls(
            status=cast("StageStatus", payload["status"]),
            warnings=[str(item) for item in payload.get("warnings", [])],
            artifacts=[str(item) for item in payload.get("artifacts", [])],
            error=(str(payload["error"]) if payload.get("error") is not None else None),
            detail=dict(payload.get("detail", {})),
        )


@dataclass(slots=True, kw_only=True)
class ArtifactEntry:
    """One artifact path (relative to the bundle root) with provenance."""

    path: str
    stage: str
    kind: str
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this artifact."""
        return {
            "path": self.path,
            "stage": self.stage,
            "kind": self.kind,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rehydrate an artifact entry from its JSON payload."""
        return cls(
            path=str(payload["path"]),
            stage=str(payload["stage"]),
            kind=str(payload["kind"]),
            sha256=(
                str(payload["sha256"]) if payload.get("sha256") is not None else None
            ),
        )


@dataclass(slots=True, kw_only=True)
class BundleRecord:
    """The complete authoritative record stored in ``bundle.json``."""

    identity: BundleIdentity
    source: dict[str, Any]
    configuration: dict[str, Any]
    versions: dict[str, Any]
    quality: str
    status: BundleStatus = "in-progress"
    exit_code: int | None = None
    stages: dict[str, StageRecord] = field(default_factory=dict)
    artifacts: list[ArtifactEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verdicts: dict[str, Any] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    version: str = field(default_factory=package_version)

    def stage(self, name: str) -> StageRecord:
        """Return the named stage record, creating it when absent."""
        return self.stages.setdefault(name, StageRecord())

    def record_artifact(
        self,
        *,
        path: str,
        stage: str,
        kind: str,
        sha256: str | None = None,
    ) -> None:
        """Register one artifact, replacing a previous entry for its path."""
        self.artifacts = [entry for entry in self.artifacts if entry.path != path]
        self.artifacts.append(
            ArtifactEntry(path=path, stage=stage, kind=kind, sha256=sha256)
        )
        stage_record = self.stage(stage)
        if path not in stage_record.artifacts:
            stage_record.artifacts.append(path)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this record."""
        return {
            "schema": BUNDLE_SCHEMA,
            "version": self.version,
            "identity": self.identity.to_dict(),
            "source": dict(self.source),
            "configuration": dict(self.configuration),
            "versions": dict(self.versions),
            "quality": self.quality,
            "status": self.status,
            "exit_code": self.exit_code,
            "stages": {name: stage.to_dict() for name, stage in self.stages.items()},
            "artifacts": [entry.to_dict() for entry in self.artifacts],
            "warnings": list(self.warnings),
            "verdicts": dict(self.verdicts),
            "pending": [dict(item) for item in self.pending],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rehydrate a record from its validated JSON payload."""
        return cls(
            identity=BundleIdentity.from_dict(payload["identity"]),
            source=dict(payload["source"]),
            configuration=dict(payload["configuration"]),
            versions=dict(payload.get("versions", {})),
            quality=str(payload["quality"]),
            status=cast("BundleStatus", payload["status"]),
            exit_code=(
                int(payload["exit_code"])
                if payload.get("exit_code") is not None
                else None
            ),
            stages={
                str(name): StageRecord.from_dict(stage)
                for name, stage in payload.get("stages", {}).items()
            },
            artifacts=[
                ArtifactEntry.from_dict(entry) for entry in payload.get("artifacts", [])
            ],
            warnings=[str(item) for item in payload.get("warnings", [])],
            verdicts=dict(payload.get("verdicts", {})),
            pending=[dict(item) for item in payload.get("pending", [])],
            version=str(payload.get("version", package_version())),
        )


def bundle_json_path(directory: Path) -> Path:
    """Return the record path inside a bundle directory."""
    return directory / BUNDLE_FILENAME


def write_record(record: BundleRecord, directory: Path) -> None:
    """Validate and atomically replace the bundle record."""
    from legolization.eval_artifacts import atomic_json  # noqa: PLC0415

    payload = record.to_dict()
    _validate_json_schema(payload)
    atomic_json(bundle_json_path(directory), payload)


def read_record(directory: Path) -> BundleRecord | None:
    """Load a bundle record; corrupt or missing records read as ``None``."""
    path = bundle_json_path(directory)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != BUNDLE_SCHEMA:
            return None
        _validate_json_schema(payload)
        return BundleRecord.from_dict(payload)
    except (OSError, KeyError, TypeError, ValueError, ManifestError):
        return None


def read_identity_key(directory: Path) -> str | None:
    """Return the identity key stored in a bundle directory, if readable."""
    record = read_record(directory)
    return record.identity.key() if record is not None else None


def source_payload(
    input_path: Path, *, bundle_dir: Path, sha256: str
) -> dict[str, Any]:
    """Describe the source input without ever storing an absolute path.

    A relative path is recorded only when it stays near the bundle
    (at most two leading ascents); otherwise only filename and hash.
    """
    payload: dict[str, Any] = {"filename": input_path.name, "sha256": sha256}
    try:
        relative = Path(
            os.path.relpath(input_path.resolve(), start=bundle_dir.resolve())
        )
    except ValueError:
        return payload
    ascents = 0
    for part in relative.parts:
        if part != "..":
            break
        ascents += 1
    if ascents <= _MAX_RELATIVE_ASCENTS:
        payload["relative_path"] = relative.as_posix()
    return payload


def versions_payload() -> dict[str, Any]:
    """Record the software and library versions behind a bundle."""
    import importlib.metadata  # noqa: PLC0415
    import platform  # noqa: PLC0415

    from legolization.catalog import catalog_hash  # noqa: PLC0415

    libraries: dict[str, str] = {}
    for distribution in _LIBRARY_DISTRIBUTIONS:
        try:
            libraries[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    return {
        "legolization": package_version(),
        "python": platform.python_version(),
        "libraries": libraries,
        "catalog_sha256": catalog_hash(),
    }


@lru_cache(maxsize=1)
def _bundle_json_schema() -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        json.loads(BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8")),
    )


def _validate_json_schema(payload: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(_bundle_json_schema()).iter_errors(payload),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(item) for item in first.absolute_path) or "bundle"
    msg = f"invalid bundle record at {location}: {first.message}"
    raise ManifestError(msg)
