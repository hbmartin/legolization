"""Durable identities and artifacts for resumable corpus evaluation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from legolization import telemetry
from legolization.compare import Candidate, CandidateMetrics

if TYPE_CHECKING:
    from collections.abc import Iterable

_RUNTIME_PREFIXES = ("src/", "scripts/")
_RUNTIME_FILES = frozenset(
    {
        ".python-version",
        "data/corpus/manifest.toml",
        "pyproject.toml",
        "uv.lock",
    }
)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        msg = "git executable is required for evaluation identity"
        raise RuntimeError(msg)
    return executable


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Commit plus the exact runtime-source bytes in the working tree."""

    git_sha: str
    source_hash: str
    dirty: bool

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe identity."""
        return asdict(self)


def _git_paths(repo: Path) -> list[Path]:
    """Return tracked and non-ignored untracked runtime input paths."""
    result = subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [
            _git_executable(),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    relative = [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]
    return sorted(
        path
        for path in relative
        if path.as_posix() in _RUNTIME_FILES
        or path.as_posix().startswith(_RUNTIME_PREFIXES)
    )


def source_identity(repo: Path) -> SourceIdentity:
    """Hash runtime source/config, including non-ignored untracked files."""
    git_sha = telemetry.git_sha(repo)
    if git_sha is None:
        msg = f"{repo} is not a readable git checkout"
        raise RuntimeError(msg)
    digest = hashlib.sha256()
    dirty = False
    for relative in _git_paths(repo):
        path = repo / relative
        if not path.is_file():
            continue
        payload = path.read_bytes()
        encoded = relative.as_posix().encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    status = subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [
            _git_executable(),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *_RUNTIME_PREFIXES,
            *_RUNTIME_FILES,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    dirty = bool(status.stdout.strip())
    return SourceIdentity(
        git_sha=git_sha,
        source_hash=digest.hexdigest(),
        dirty=dirty,
    )


def canonical_value(value: object) -> object:  # noqa: PLR0911
    """Convert nested configuration values to stable JSON primitives."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical_value(getattr(value, field.name))
            for field in fields(value)
            if field.name != "progress"
        }
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        canonical_items = [canonical_value(item) for item in value]
        return sorted(
            canonical_items,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    msg = f"cannot canonically serialize {type(value).__name__}"
    raise TypeError(msg)


def configuration_hash(*values: object) -> str:
    """Hash result-affecting configuration values as canonical JSON."""
    payload = [canonical_value(value) for value in values]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def input_sha256(path: Path) -> str:
    """Hash an input file without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: object) -> None:
    """Atomically replace ``path`` with an indented JSON payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def candidate_payload(
    candidate: Candidate,
    *,
    identity: SourceIdentity,
    config_hash: str,
    input_hash: str,
    model: str,
) -> dict[str, object]:
    """Serialize one completed candidate without its heavyweight layout."""
    return {
        "schema": 1,
        "identity": identity.to_dict(),
        "config_hash": config_hash,
        "input_hash": input_hash,
        "model": model,
        "strategy": candidate.strategy,
        "seed": candidate.seed,
        "status": "ok" if candidate.ok else "error",
        "seconds": candidate.seconds,
        "error": candidate.error,
        "metrics": asdict(candidate.metrics) if candidate.metrics is not None else None,
    }


def candidate_from_payload(payload: Mapping[str, Any]) -> Candidate:
    """Rehydrate the score-bearing portion of a candidate artifact."""
    metrics_payload = payload.get("metrics")
    metrics = (
        CandidateMetrics(**metrics_payload)
        if isinstance(metrics_payload, dict)
        else None
    )
    return Candidate(
        strategy=str(payload["strategy"]),
        seconds=float(payload["seconds"]),
        metrics=metrics,
        error=str(payload["error"]) if payload.get("error") is not None else None,
        seed=int(payload["seed"]),
    )


def safe_name(value: str) -> str:
    """Make a manifest identifier safe as one path component."""
    return _SAFE_NAME.sub("-", value).strip("-") or "candidate"


def candidate_path(  # noqa: PLR0913
    root: Path,
    *,
    model: str,
    strategy: str,
    seed: int,
    identity: SourceIdentity,
    config_hash: str,
    input_hash: str,
) -> Path:
    """Return the stable artifact path for one exact candidate identity."""
    identity_key = (
        f"{identity.git_sha[:12]}-{identity.source_hash[:12]}-"
        f"{config_hash[:12]}-{input_hash[:12]}"
    )
    return (
        root
        / "candidates"
        / safe_name(model)
        / safe_name(strategy)
        / f"seed-{seed}"
        / f"{identity_key}.json"
    )


def matching_candidate(  # noqa: PLR0913
    path: Path,
    *,
    identity: SourceIdentity,
    config_hash: str,
    input_hash: str,
    model: str,
    strategy: str,
    seed: int,
) -> Candidate | None:
    """Load an exact successful artifact, rejecting corruption or drift."""
    try:
        payload = json.loads(path.read_text())
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != 1
            or payload.get("identity") != identity.to_dict()
            or payload.get("config_hash") != config_hash
            or payload.get("input_hash") != input_hash
            or payload.get("model") != model
            or payload.get("strategy") != strategy
            or payload.get("seed") != seed
            or payload.get("status") != "ok"
        ):
            return None
        candidate = candidate_from_payload(payload)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return candidate if candidate.ok else None


def all_successful(paths: Iterable[Path]) -> bool:
    """Return whether every artifact path contains a successful result."""
    for path in paths:
        try:
            payload = json.loads(path.read_text())
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                return False
        except (OSError, json.JSONDecodeError):
            return False
    return True
