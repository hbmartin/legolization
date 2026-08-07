"""Manage the self-evaluation corpus described by the packaged manifest.

The corpus has two halves: curated meshes downloaded from pinned URLs
(``download`` fetches and sha256-verifies them into user-data storage)
and synthetic stress-test shapes (``generate`` rebuilds them from the
pure, deterministic generators in :mod:`legolization.corpus.generators`
— the generators are the committed source of truth, never the ``.npy``
files). Each operation returns structured :class:`ModelReport` records;
the ``legolization corpus`` CLI and the module entry point below render
them.

Usage::

    uv run python -m legolization.corpus.ops generate [--only NAME,...]
    uv run python -m legolization.corpus.ops download [--only NAME,...]
    uv run python -m legolization.corpus.ops verify
    uv run python -m legolization.corpus.ops list
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import numpy as np

from legolization.corpus.generators import GENERATORS
from legolization.corpus.manifest import load_manifest, select_models
from legolization.corpus.storage import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from legolization.corpus.manifest import CorpusModel

_DOWNLOAD_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelReport:
    """Outcome of one corpus operation on one manifest model."""

    name: str
    kind: str
    status: str
    ok: bool
    detail: str = ""
    path: str | None = None

    @property
    def line(self) -> str:
        """Human-readable one-line rendering of this report."""
        lines = {
            "generated": f"generated {self.path} {self.detail}",
            "downloaded": f"ok {self.path}",
            "already-present": f"ok {self.path} (already present)",
            "missing": f"MISSING {self.name} ({self.path}) - run generate/download",
            "stale": f"STALE (regenerate) {self.name}",
            "corrupt": f"CORRUPT {self.name}: {self.detail}",
            "hash-mismatch": f"HASH MISMATCH {self.name}",
            "error": f"error: {self.name}: {self.detail}",
        }
        return lines.get(self.status, f"{self.status} {self.name}")

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this report."""
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "ok": self.ok,
            "detail": self.detail,
            "path": self.path,
        }


def exit_code(reports: Iterable[ModelReport]) -> int:
    """Map reports to a process exit code: any failure is 1."""
    return int(any(not report.ok for report in reports))


def generate(
    models: list[CorpusModel],
    only: str | None = None,
) -> list[ModelReport]:
    """Regenerate synthetic models from their registered generators."""
    reports: list[ModelReport] = []
    for model in select_models(models, only):
        if model.kind != "synthetic":
            continue
        if model.generator not in GENERATORS:
            reports.append(
                ModelReport(
                    name=model.name,
                    kind=model.kind,
                    status="error",
                    ok=False,
                    detail=f"unknown generator {model.generator!r}",
                )
            )
            continue
        codes = GENERATORS[model.generator]()
        target = model.abs_path
        target.parent.mkdir(parents=True, exist_ok=True)
        np.save(target, codes)
        reports.append(
            ModelReport(
                name=model.name,
                kind=model.kind,
                status="generated",
                ok=True,
                detail=str(codes.shape),
                path=str(target),
            )
        )
    return reports


def download(
    models: list[CorpusModel],
    only: str | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> list[ModelReport]:
    """Fetch mesh models from their pinned URLs, verifying sha256."""
    reports: list[ModelReport] = []
    for model in select_models(models, only):
        if model.kind != "mesh":
            continue
        reports.append(_download_one(model, progress=progress))
    return reports


def _download_one(
    model: CorpusModel,
    *,
    progress: Callable[[str], None] | None,
) -> ModelReport:
    """Fetch one mesh model, reporting the outcome."""

    def failure(detail: str) -> ModelReport:
        return ModelReport(
            name=model.name,
            kind=model.kind,
            status="error",
            ok=False,
            detail=detail,
            path=str(model.abs_path),
        )

    if model.source_url is None or model.sha256 is None:
        return failure("manifest is missing source_url/sha256")
    target = model.abs_path
    if target.exists() and sha256_of(target) == model.sha256:
        return ModelReport(
            name=model.name,
            kind=model.kind,
            status="already-present",
            ok=True,
            path=str(target),
        )
    if not model.source_url.startswith("https://"):
        return failure("refusing non-https URL")
    target.parent.mkdir(parents=True, exist_ok=True)
    if progress is not None:
        progress(f"fetching {model.source_url}")
    try:
        with TemporaryDirectory(
            dir=target.parent,
            prefix=f".{target.name}.",
        ) as temp_dir:
            temp_path = Path(temp_dir) / target.name
            with (
                urllib.request.urlopen(  # noqa: S310 - https enforced above
                    model.source_url,
                    timeout=_DOWNLOAD_TIMEOUT_S,
                ) as response,
                temp_path.open("wb") as handle,
            ):
                shutil.copyfileobj(response, handle)
            if (actual := sha256_of(temp_path)) != model.sha256:
                return failure(
                    f"sha256 mismatch (expected {model.sha256}, got {actual}); "
                    "file discarded"
                )
            temp_path.replace(target)
    except (OSError, urllib.error.URLError) as error:
        return failure(f"download failed: {error}")
    return ModelReport(
        name=model.name,
        kind=model.kind,
        status="downloaded",
        ok=True,
        path=str(target),
    )


def verify(models: list[CorpusModel]) -> list[ModelReport]:
    """Check every model is present and matches its source of truth."""
    return [_verify_one(model) for model in models]


def _verify_one(model: CorpusModel) -> ModelReport:  # noqa: PLR0911 - one status per outcome
    """Verify one model against its pinned hash or generator."""

    def report(status: str, *, ok: bool, detail: str = "") -> ModelReport:
        return ModelReport(
            name=model.name,
            kind=model.kind,
            status=status,
            ok=ok,
            detail=detail,
            path=str(model.abs_path),
        )

    if not model.abs_path.exists():
        return report("missing", ok=False)
    match model.kind:
        case "mesh":
            if model.sha256 is not None and sha256_of(model.abs_path) == model.sha256:
                return report("ok", ok=True)
            return report("hash-mismatch", ok=False)
        case "synthetic":
            if model.generator not in GENERATORS:
                return report(
                    "error",
                    ok=False,
                    detail=f"unknown generator {model.generator!r}",
                )
            expected = GENERATORS[model.generator]()
            try:
                actual = np.load(model.abs_path)
            except (EOFError, OSError, ValueError) as error:
                return report("corrupt", ok=False, detail=str(error))
            if np.array_equal(actual, expected):
                return report("ok", ok=True)
            return report("stale", ok=False)
        case _:
            return report("error", ok=False, detail=f"unknown kind {model.kind!r}")


def list_models(models: list[CorpusModel]) -> list[ModelReport]:
    """Report each manifest model's kind, availability, and traits."""
    return [
        ModelReport(
            name=model.name,
            kind=model.kind,
            status="present" if model.abs_path.exists() else "absent",
            ok=True,
            detail=", ".join(model.traits),
            path=str(model.abs_path),
        )
        for model in models
    ]


def list_table(reports: Iterable[ModelReport]) -> str:
    """Render availability reports as the classic status table."""
    lines = [f"{'name':<20} {'kind':<10} {'present':<8} traits"]
    lines.extend(
        f"{report.name:<20} {report.kind:<10} "
        f"{'yes' if report.status == 'present' else 'no':<8} {report.detail}"
        for report in reports
    )
    return "\n".join(lines)


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
    try:
        models = load_manifest()
        match args.command:
            case "generate":
                reports = generate(models, only=args.only)
            case "download":
                reports = download(models, only=args.only, progress=print)
            case "verify":
                reports = verify(models)
            case _:
                print(list_table(reports := list_models(models)))
                return 0
    except ValueError as error:
        print(f"error: {error}")
        return 1
    for report in reports:
        print(report.line)
    return exit_code(reports)


if __name__ == "__main__":
    sys.exit(main())
