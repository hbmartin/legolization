"""Platform user-data storage for corpus inputs.

Corpus meshes and synthetic grids live outside the repository —
``LEGOLIZATION_DATA_HOME`` when set, else the platformdirs user-data
directory — so installed wheels can generate, download, and evaluate
without a checkout. A legacy ``data/corpus/`` tree from an existing
checkout is migrated (copied, never deleted) into user-data storage
the first time an input path is resolved.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_LEGACY_SUBDIRS = ("meshes", "synthetic")
_migrated_roots: set[Path] = set()


def corpus_root(env: Mapping[str, str] | None = None) -> Path:
    """Return the user-data root that holds corpus inputs."""
    import platformdirs  # noqa: PLC0415 - keep parser-time imports light

    environment = os.environ if env is None else env
    if override := environment.get("LEGOLIZATION_DATA_HOME"):
        return Path(override)
    return Path(platformdirs.user_data_dir("legolization", appauthor=False))


def inputs_dir(root: Path) -> Path:
    """Return the corpus input tree under a storage root."""
    return root / "corpus"


def legacy_data_dir() -> Path | None:
    """Return the checkout's legacy ``data/corpus`` tree, when run from one.

    Installed wheels resolve the candidate inside site-packages where the
    directory never exists, so they never migrate anything.
    """
    candidate = Path(__file__).resolve().parents[3] / "data" / "corpus"
    return candidate if candidate.is_dir() else None


def sha256_of(path: Path) -> str:
    """Hex sha256 of a file, streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrate_legacy(repo_data_dir: Path | None, root: Path) -> tuple[str, ...]:
    """Copy legacy checkout corpus files into ``root``, verified, one-way.

    Existing files under ``root`` are never overwritten and the legacy
    tree is never deleted. Every copy is sha256-verified against its
    source before it is atomically moved into place; a torn copy is
    discarded. Returns the storage-relative names that were migrated.
    """
    if repo_data_dir is None:
        return ()
    migrated: list[str] = []
    for subdir in _LEGACY_SUBDIRS:
        source_dir = repo_data_dir / subdir
        if not source_dir.is_dir():
            continue
        for source in sorted(source_dir.iterdir()):
            if not source.is_file() or source.name.startswith("."):
                continue
            target = inputs_dir(root) / subdir / source.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_name(f".{target.name}.migrating")
            shutil.copyfile(source, staging)
            if sha256_of(staging) != sha256_of(source):
                staging.unlink(missing_ok=True)
                continue
            staging.replace(target)
            migrated.append(f"{subdir}/{source.name}")
    return tuple(migrated)


def ensure_migrated(root: Path) -> tuple[str, ...]:
    """Run the legacy migration at most once per storage root per process."""
    if root in _migrated_roots:
        return ()
    _migrated_roots.add(root)
    return migrate_legacy(legacy_data_dir(), root)


def resolve_input(relative: Path, env: Mapping[str, str] | None = None) -> Path:
    """Return the storage location of a manifest-relative corpus input."""
    root = corpus_root(env)
    ensure_migrated(root)
    return inputs_dir(root) / relative
