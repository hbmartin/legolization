"""Content-addressed persistent cache for reusable placement templates."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Self, cast

from filelock import FileLock
from platformdirs import user_cache_path

_CACHE_SCHEMA = "legolization.template-cache"
_CACHE_VERSION = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateCacheKey:
    """Every input capable of changing a reusable solved template."""

    component_signature: str
    catalog_hash: str
    configuration_hash: str
    physics_profile: str
    algorithm_version: str

    @property
    def digest(self) -> str:
        """Return the canonical content address."""
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheEntry:
    """Deterministic cache-inspection row."""

    key: str
    size_bytes: int
    payload_sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateCache:
    """Atomic, locked, corruption-aware template cache."""

    root: Path

    @classmethod
    def default(cls) -> Self:
        """Use the platform-specific per-user cache directory."""
        return cls(root=user_cache_path("legolization") / "templates-v1")

    def read(self, key: TemplateCacheKey) -> dict[str, Any] | None:
        """Read and authenticate an entry, quarantining corrupt files."""
        path = self._entry_path(key.digest)
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes()
            payload = _decode_entry(raw, expected_key=key.digest)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._quarantine(path)
            return None
        return payload

    def write(self, key: TemplateCacheKey, payload: dict[str, Any]) -> Path:
        """Atomically write a canonical JSON entry under a per-key lock."""
        self._ensure_directories()
        path = self._entry_path(key.digest)
        lock = FileLock(self._lock_path(key.digest))
        envelope = {
            "key": key.digest,
            "payload": payload,
            "schema": _CACHE_SCHEMA,
            "version": _CACHE_VERSION,
        }
        serialized = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with lock:
            if path.is_file():
                return path
            with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(serialized)
                handle.flush()
            temporary.replace(path)
        return path

    def inspect(self) -> tuple[CacheEntry, ...]:
        """Return sorted authenticated metadata without reading timestamps."""
        entries: list[CacheEntry] = []
        if not self.root.is_dir():
            return ()
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.name):
            try:
                raw = path.read_bytes()
                _decode_entry(raw, expected_key=path.stem)
            except (OSError, ValueError, json.JSONDecodeError):
                self._quarantine(path)
                continue
            entries.append(
                CacheEntry(
                    key=path.stem,
                    size_bytes=len(raw),
                    payload_sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
        return tuple(entries)

    def clear(self, *, key: str | None = None, all_entries: bool = False) -> int:
        """Remove one exact content address or every entry when explicit."""
        if (key is None) == (not all_entries):
            msg = "specify exactly one cache key or all_entries=True"
            raise ValueError(msg)
        paths = (
            tuple(self.root.glob("*.json"))
            if all_entries and self.root.is_dir()
            else (self._entry_path(_validated_digest(cast("str", key))),)
        )
        removed = 0
        for path in paths:
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    def quarantine(self, key: TemplateCacheKey) -> None:
        """Quarantine one semantically invalid entry by its exact key."""
        self._quarantine(self._entry_path(key.digest))

    def _entry_path(self, digest: str) -> Path:
        return self.root / f"{digest}.json"

    def _lock_path(self, digest: str) -> Path:
        return self.root / ".locks" / f"{digest}.lock"

    def _ensure_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".locks").mkdir(exist_ok=True)
        (self.root / ".corrupt").mkdir(exist_ok=True)

    def _quarantine(self, path: Path) -> None:
        if not path.is_file():
            return
        self._ensure_directories()
        suffix = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        destination = self.root / ".corrupt" / f"{path.name}.{suffix}"
        if destination.exists():
            destination = destination.with_name(
                f"{destination.name}.{time.monotonic_ns()}"
            )
        path.replace(destination)


def _validated_digest(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        msg = "cache key must be a lowercase SHA-256 digest"
        raise ValueError(msg)
    return value


def _decode_entry(raw: bytes, *, expected_key: str) -> dict[str, Any]:
    """Decode and authenticate one cache envelope."""
    envelope = json.loads(raw)
    if not isinstance(envelope, dict):
        msg = "cache envelope must be an object"
        raise TypeError(msg)
    if envelope.get("schema") != _CACHE_SCHEMA:
        msg = "cache schema mismatch"
        raise ValueError(msg)
    if envelope.get("version") != _CACHE_VERSION:
        msg = "cache version mismatch"
        raise ValueError(msg)
    if envelope.get("key") != expected_key:
        msg = "cache content address mismatch"
        raise ValueError(msg)
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        msg = "cache payload must be an object"
        raise TypeError(msg)
    return cast("dict[str, Any]", payload)
