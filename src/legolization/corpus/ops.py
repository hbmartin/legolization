"""Manage the self-evaluation corpus described by ``data/corpus/manifest.toml``.

The corpus has two halves: curated meshes downloaded from pinned URLs
(gitignored; ``download`` fetches and sha256-verifies them) and synthetic
stress-test shapes (gitignored; ``generate`` rebuilds them from the pure,
deterministic generators in :mod:`legolization.corpus.generators` — the
generators are the committed source of truth, never the ``.npy`` files).

Usage::

    uv run python -m legolization.corpus.ops generate [--only NAME,...]
    uv run python -m legolization.corpus.ops download [--only NAME,...]
    uv run python -m legolization.corpus.ops verify
    uv run python -m legolization.corpus.ops list
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import numpy as np

from legolization.corpus.generators import GENERATORS
from legolization.corpus.manifest import load_manifest, select_models

if TYPE_CHECKING:
    from legolization.corpus.manifest import CorpusModel

_DOWNLOAD_TIMEOUT_S = 30.0


def generate(models: list[CorpusModel], only: str | None = None) -> int:
    """Regenerate synthetic models from their registered generators."""
    for model in select_models(models, only):
        if model.kind != "synthetic":
            continue
        if model.generator not in GENERATORS:
            print(f"error: {model.name}: unknown generator {model.generator!r}")
            return 1
        codes = GENERATORS[model.generator]()
        model.abs_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(model.abs_path, codes)
        print(f"generated {model.path} {codes.shape}")
    return 0


def _sha256_of(path: Path) -> str:
    """Hex sha256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(models: list[CorpusModel], only: str | None = None) -> int:
    """Fetch mesh models from their pinned URLs, verifying sha256."""
    status = 0
    for model in select_models(models, only):
        if model.kind != "mesh":
            continue
        if model.source_url is None or model.sha256 is None:
            print(f"error: {model.name}: manifest is missing source_url/sha256")
            status = 1
            continue
        if model.abs_path.exists() and _sha256_of(model.abs_path) == model.sha256:
            print(f"ok {model.path} (already present)")
            continue
        if not model.source_url.startswith("https://"):
            print(f"error: {model.name}: refusing non-https URL")
            status = 1
            continue
        model.abs_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"fetching {model.source_url}")
        try:
            with TemporaryDirectory(
                dir=model.abs_path.parent,
                prefix=f".{model.abs_path.name}.",
            ) as temp_dir:
                temp_path = Path(temp_dir) / model.abs_path.name
                with (
                    urllib.request.urlopen(  # noqa: S310 - https enforced above
                        model.source_url,
                        timeout=_DOWNLOAD_TIMEOUT_S,
                    ) as response,
                    temp_path.open("wb") as target,
                ):
                    shutil.copyfileobj(response, target)
                if (actual := _sha256_of(temp_path)) != model.sha256:
                    print(
                        f"error: {model.name}: sha256 mismatch "
                        f"(expected {model.sha256}, got {actual}); file discarded"
                    )
                    status = 1
                    continue
                temp_path.replace(model.abs_path)
        except (OSError, urllib.error.URLError) as error:
            print(f"error: {model.name}: download failed: {error}")
            status = 1
            continue
        print(f"ok {model.path}")
    return status


def verify(models: list[CorpusModel]) -> int:
    """Check every model is present and matches its source of truth."""
    status = 0
    for model in models:
        if not model.abs_path.exists():
            print(f"MISSING {model.name} ({model.path}) - run generate/download")
            status = 1
            continue
        match model.kind:
            case "mesh":
                ok = model.sha256 is not None and (
                    _sha256_of(model.abs_path) == model.sha256
                )
                print(f"{'ok' if ok else 'HASH MISMATCH'} {model.name}")
            case "synthetic":
                expected = GENERATORS[model.generator or ""]()
                try:
                    actual = np.load(model.abs_path)
                except (EOFError, OSError, ValueError) as error:
                    print(f"CORRUPT {model.name}: {error}")
                    status = 1
                    continue
                ok = bool(np.array_equal(actual, expected))
                print(f"{'ok' if ok else 'STALE (regenerate)'} {model.name}")
            case _:
                ok = False
                print(f"UNKNOWN KIND {model.name}: {model.kind}")
        if not ok:
            status = 1
    return status


def list_models(models: list[CorpusModel]) -> int:
    """Print a status table of the manifest."""
    print(f"{'name':<20} {'kind':<10} {'present':<8} traits")
    for model in models:
        present = "yes" if model.abs_path.exists() else "no"
        print(
            f"{model.name:<20} {model.kind:<10} {present:<8} {', '.join(model.traits)}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "download"):
        command = sub.add_parser(name)
        command.add_argument("--only", default=None, metavar="NAME,...")
    sub.add_parser("verify")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    models = load_manifest()
    match args.command:
        case "generate":
            return generate(models, only=args.only)
        case "download":
            return download(models, only=args.only)
        case "verify":
            return verify(models)
        case _:
            return list_models(models)


if __name__ == "__main__":
    sys.exit(main())
