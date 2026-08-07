"""The managed official LDraw parts library behind ``parts sync``.

The library lives in platform user-data storage (no admin rights),
is validated after download, records source/version/hash metadata,
and checks for updates weekly. A failed check with a valid existing
library continues silently — rendering never requires the network.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from legolization.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

LIBRARY_URL = "https://library.ldraw.org/library/updates/complete.zip"
METADATA_FILENAME = "library.json"
METADATA_SCHEMA = "legolization.parts-library/v1"
CHECK_INTERVAL = timedelta(days=7)

_REQUIRED_PREFIXES = ("ldraw/parts/", "ldraw/p/")
_REQUIRED_MEMBERS = ("ldraw/LDConfig.ldr",)
_HTTP_NOT_MODIFIED = 304
_FETCH_TIMEOUT_S = 30.0

type SyncStatus = Literal["fresh", "downloaded", "updated", "offline-kept"]
type Fetcher = Callable[[str, dict[str, str]], "FetchResponse"]


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchResponse:
    """One HTTP response for the library download seam."""

    status: int
    headers: dict[str, str]
    payload: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class LibraryMetadata:
    """Source, hash, and freshness record of the managed library."""

    source_url: str
    sha256: str
    size_bytes: int
    downloaded_at: str
    last_check_at: str
    etag: str | None = None
    last_modified: str | None = None
    file_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this metadata."""
        return {
            "schema": METADATA_SCHEMA,
            "source_url": self.source_url,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "downloaded_at": self.downloaded_at,
            "last_check_at": self.last_check_at,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "file_count": self.file_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Rehydrate metadata from its JSON payload."""
        return cls(
            source_url=str(payload["source_url"]),
            sha256=str(payload["sha256"]),
            size_bytes=int(payload["size_bytes"]),
            downloaded_at=str(payload["downloaded_at"]),
            last_check_at=str(payload["last_check_at"]),
            etag=(str(payload["etag"]) if payload.get("etag") else None),
            last_modified=(
                str(payload["last_modified"]) if payload.get("last_modified") else None
            ),
            file_count=int(payload.get("file_count", 0)),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncResult:
    """Outcome of one ``parts sync`` invocation."""

    status: SyncStatus
    root: Path
    ldraw_dir: Path | None
    metadata: LibraryMetadata | None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this result."""
        return {
            "status": self.status,
            "root": str(self.root),
            "ldraw_dir": str(self.ldraw_dir) if self.ldraw_dir else None,
            "metadata": self.metadata.to_dict() if self.metadata else None,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True, kw_only=True)
class _SyncState:
    """Working state threaded through the sync steps."""

    root: Path
    now: datetime
    metadata: LibraryMetadata | None
    warnings: list[str] = field(default_factory=list)


def managed_root(env: Mapping[str, str] | None = None) -> Path:
    """Return the platform user-data root for the managed library."""
    import os  # noqa: PLC0415

    import platformdirs  # noqa: PLC0415

    environment = os.environ if env is None else env
    if override := environment.get("LEGOLIZATION_DATA_HOME"):
        return Path(override)
    return Path(platformdirs.user_data_dir("legolization", appauthor=False))


def library_dir(root: Path) -> Path | None:
    """Return the valid managed LDraw directory under ``root``, if any."""
    candidate = root / "ldraw"
    if (
        (candidate / "parts").is_dir()
        and (candidate / "p").is_dir()
        and (candidate / "LDConfig.ldr").is_file()
    ):
        return candidate
    return None


def load_metadata(root: Path) -> LibraryMetadata | None:
    """Load the stored library metadata; corrupt records read as ``None``."""
    import json  # noqa: PLC0415

    path = root / METADATA_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != METADATA_SCHEMA:
            return None
        return LibraryMetadata.from_dict(payload)
    except (OSError, KeyError, TypeError, ValueError):
        return None


def sync(
    root: Path | None = None,
    *,
    fetch: Fetcher | None = None,
    force: bool = False,
    now: Callable[[], datetime] | None = None,
) -> SyncResult:
    """Install or update the managed library with the weekly policy."""
    state = _SyncState(
        root=root if root is not None else managed_root(),
        now=(now or _utc_now)(),
        metadata=load_metadata(root if root is not None else managed_root()),
    )
    existing = library_dir(state.root)
    if existing is not None and not force and _is_fresh(state):
        return _result("fresh", state, existing)
    try:
        response = (fetch or _urllib_fetch)(LIBRARY_URL, _conditional_headers(state))
    except OSError as error:
        if existing is not None:
            state.warnings.append(
                f"update check failed ({error}); keeping the existing library"
            )
            return _result("offline-kept", state, existing)
        msg = f"no usable parts library and the download failed: {error}"
        raise ConfigurationError(msg) from error
    if response.status == _HTTP_NOT_MODIFIED and existing is not None:
        state.metadata = _touched(state)
        _write_metadata(state)
        return _result("fresh", state, existing)
    try:
        file_count = _validate_archive(response.payload)
    except ConfigurationError as error:
        if existing is not None:
            state.warnings.append(
                f"downloaded archive failed validation ({error}); "
                "keeping the existing library"
            )
            return _result("offline-kept", state, existing)
        raise
    _extract_archive(state.root, response.payload)
    state.metadata = _downloaded_metadata(state, response, file_count)
    _write_metadata(state)
    status: SyncStatus = "updated" if existing is not None else "downloaded"
    return _result(status, state, library_dir(state.root))


def _is_fresh(state: _SyncState) -> bool:
    if state.metadata is None:
        return False
    try:
        checked = datetime.fromisoformat(state.metadata.last_check_at)
    except ValueError:
        return False
    return state.now - checked < CHECK_INTERVAL


def _conditional_headers(state: _SyncState) -> dict[str, str]:
    headers: dict[str, str] = {}
    if state.metadata is not None:
        if state.metadata.etag:
            headers["If-None-Match"] = state.metadata.etag
        if state.metadata.last_modified:
            headers["If-Modified-Since"] = state.metadata.last_modified
    return headers


def _validate_archive(payload: bytes) -> int:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.testzip() is not None:
                msg = "corrupt member in the downloaded archive"
                raise ConfigurationError(msg)
            names = archive.namelist()
    except zipfile.BadZipFile as error:
        msg = f"downloaded payload is not a zip archive: {error}"
        raise ConfigurationError(msg) from error
    for prefix in _REQUIRED_PREFIXES:
        if not any(name.startswith(prefix) for name in names):
            msg = f"archive is missing the required {prefix} tree"
            raise ConfigurationError(msg)
    for member in _REQUIRED_MEMBERS:
        if member not in names:
            msg = f"archive is missing the required {member} member"
            raise ConfigurationError(msg)
    return len(names)


def _extract_archive(root: Path, payload: bytes) -> None:
    root.mkdir(parents=True, exist_ok=True)
    staging = root / ".ldraw.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(staging)
    target = root / "ldraw"
    replaced = root / ".ldraw.old"
    if replaced.exists():
        shutil.rmtree(replaced)
    if target.exists():
        target.rename(replaced)
    (staging / "ldraw").rename(target)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(replaced, ignore_errors=True)


def _downloaded_metadata(
    state: _SyncState,
    response: FetchResponse,
    file_count: int,
) -> LibraryMetadata:
    import hashlib  # noqa: PLC0415

    stamp = state.now.isoformat()
    return LibraryMetadata(
        source_url=LIBRARY_URL,
        sha256=hashlib.sha256(response.payload).hexdigest(),
        size_bytes=len(response.payload),
        downloaded_at=stamp,
        last_check_at=stamp,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
        file_count=file_count,
    )


def _touched(state: _SyncState) -> LibraryMetadata:
    assert state.metadata is not None  # noqa: S101 - guarded by caller
    from dataclasses import replace  # noqa: PLC0415

    return replace(state.metadata, last_check_at=state.now.isoformat())


def _write_metadata(state: _SyncState) -> None:
    from legolization.eval_artifacts import atomic_json  # noqa: PLC0415

    assert state.metadata is not None  # noqa: S101 - guarded by caller
    atomic_json(state.root / METADATA_FILENAME, state.metadata.to_dict())


def _result(
    status: SyncStatus,
    state: _SyncState,
    ldraw: Path | None,
) -> SyncResult:
    return SyncResult(
        status=status,
        root=state.root,
        ldraw_dir=ldraw,
        metadata=state.metadata,
        warnings=tuple(state.warnings),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _urllib_fetch(url: str, headers: dict[str, str]) -> FetchResponse:
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https constant
    try:
        with urllib.request.urlopen(  # noqa: S310 - https constant
            request,
            timeout=_FETCH_TIMEOUT_S,
        ) as response:
            return FetchResponse(
                status=int(response.status),
                headers=dict(response.headers),
                payload=response.read(),
            )
    except urllib.error.HTTPError as error:
        if error.code == _HTTP_NOT_MODIFIED:
            return FetchResponse(
                status=error.code, headers=dict(error.headers), payload=b""
            )
        msg = f"library download failed: HTTP {error.code}"
        raise OSError(msg) from error
    except urllib.error.URLError as error:
        msg = f"library download failed: {error.reason}"
        raise OSError(msg) from error
