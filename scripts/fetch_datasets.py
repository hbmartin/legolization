"""Fetch the external validation datasets registered in ``datasets.toml``.

The download ladder mirrors ``legolization.corpus.ops._download_one`` — refuse
non-https, stream into a sibling temporary file, verify sha256, then rename
atomically so a partial or corrupt payload never lands at the real path. Two
deliberate differences, both forced by scale: these files run to gigabytes
rather than the corpus meshes' megabytes, so a download resumes from a ``.part``
file via an HTTP Range request, and content hashes live in a generated lock
(``datasets.lock.toml``) rather than inline, so the registry stays hand-editable.

Registry plus lock: ``datasets.toml`` says what we fetch and under what terms,
``datasets.lock.toml`` says what we got. An entry with no lock row is fetched and
then pinned; an entry with one is verified against it.

Usage::

    uv run python scripts/fetch_datasets.py --dry-run
    uv run python scripts/fetch_datasets.py [--group core] [--only NAME ...]
    uv run python scripts/fetch_datasets.py --verify
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import sys
import tarfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from http.client import HTTPResponse

_REPO = Path(__file__).parent.parent
_REGISTRY = Path(__file__).parent / "datasets.toml"
_LOCK = Path(__file__).parent / "datasets.lock.toml"
_DEFAULT_DEST = _REPO / "datasets"

_TIMEOUT_S = 60.0
_CHUNK = 1 << 20
_USER_AGENT = (
    "legolization-dataset-fetcher/1 (+https://github.com/hbmartin/legolization)"
)

type Group = Literal["core", "large", "manual"]
type Status = Literal[
    "already-present",
    "downloaded",
    "pinned",
    "missing",
    "size-mismatch",
    "hash-mismatch",
    "unavailable",
    "skipped",
    "error",
]


@dataclass(frozen=True, slots=True)
class DatasetFile:
    """One fetchable payload inside a registry entry."""

    name: str
    url: str
    expected_bytes: int

    @property
    def relative(self) -> Path:
        """Path of this payload relative to its dataset directory."""
        return Path(self.name)


@dataclass(frozen=True, slots=True, kw_only=True)
class Dataset:
    """One registry entry: a licensed source and the files it provides."""

    name: str
    group: Group
    license: str
    source: str
    notes: str
    citation: str = ""
    available: bool = True
    extract: Literal["tar", "zip", "gz"] | None = None
    files: tuple[DatasetFile, ...] = ()

    @property
    def total_bytes(self) -> int:
        """Sum of every payload's recorded Content-Length."""
        return sum(item.expected_bytes for item in self.files)

    def directory(self, dest: Path) -> Path:
        """Root directory this dataset's payloads live under."""
        return dest / self.name


@dataclass(frozen=True, slots=True, kw_only=True)
class FileReport:
    """Outcome of one operation on one payload."""

    dataset: str
    file: str
    status: Status
    ok: bool
    detail: str = ""
    path: str | None = None
    sha256: str | None = None

    @property
    def line(self) -> str:
        """Human-readable one-line rendering."""
        label = f"{self.dataset}/{self.file}"
        lines = {
            "already-present": f"ok       {label} (already present)",
            "downloaded": f"ok       {label} -> {self.path}",
            "pinned": f"pinned   {label} sha256={self.sha256}",
            "missing": f"MISSING  {label} - run without --verify to fetch",
            "size-mismatch": f"SIZE     {label}: {self.detail}",
            "hash-mismatch": f"HASH     {label}: {self.detail}",
            "unavailable": f"skip     {label}: {self.detail}",
            "skipped": f"skip     {label}: {self.detail}",
            "error": f"ERROR    {label}: {self.detail}",
        }
        return lines.get(self.status, f"{self.status} {label}")


@dataclass(slots=True)
class Lock:
    """The generated hash lock: dataset/file -> sha256 and observed size."""

    rows: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = _LOCK) -> Self:
        """Read the lock, treating an absent file as an empty lock."""
        if not path.exists():
            return cls()
        return cls(rows=dict(tomllib.loads(path.read_text(encoding="utf-8"))))

    def get(self, key: str) -> dict[str, object] | None:
        """Return the locked row for ``dataset/file``, if any."""
        return self.rows.get(key)

    def put(self, key: str, *, sha256: str, size: int) -> None:
        """Record a freshly measured payload."""
        self.rows[key] = {
            "sha256": sha256,
            "bytes": size,
            "fetched": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def write(self, path: Path = _LOCK) -> None:
        """Write the lock deterministically (sorted keys, atomic replace)."""
        lines = [
            "# Generated by scripts/fetch_datasets.py - do not edit by hand.",
            "# Content hashes for the payloads registered in datasets.toml.",
            "# Commit this alongside the registry: it turns a re-fetch into a",
            "# verification instead of an act of trust.",
            "",
        ]
        for key in sorted(self.rows):
            row = self.rows[key]
            lines.append(f'["{key}"]')
            lines.append(f'sha256 = "{row["sha256"]}"')
            lines.append(f"bytes = {row['bytes']}")
            lines.append(f'fetched = "{row["fetched"]}"')
            lines.append("")
        temp = path.with_suffix(f"{path.suffix}.tmp")
        temp.write_text("\n".join(lines), encoding="utf-8")
        temp.replace(path)


def load_registry(path: Path = _REGISTRY) -> list[Dataset]:
    """Parse ``datasets.toml`` into typed registry entries."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    datasets: list[Dataset] = []
    for entry in raw.get("dataset", []):
        files = tuple(
            DatasetFile(
                name=str(item["name"]),
                url=str(item["url"]),
                expected_bytes=int(item["bytes"]),
            )
            for item in entry.get("file", [])
        )
        datasets.append(
            Dataset(
                name=str(entry["name"]),
                group=entry.get("group", "core"),
                license=str(entry["license"]),
                source=str(entry["source"]),
                notes=str(entry.get("notes", "")).strip(),
                citation=str(entry.get("citation", "")),
                available=bool(entry.get("available", True)),
                extract=entry.get("extract"),
                files=files,
            )
        )
    if len(names := {item.name for item in datasets}) != len(datasets):
        msg = f"duplicate dataset names in {path}"
        raise ValueError(msg)
    if not names:
        msg = f"{path} declares no datasets"
        raise ValueError(msg)
    return datasets


def select(
    datasets: Iterable[Dataset],
    *,
    only: tuple[str, ...],
    groups: tuple[str, ...],
) -> list[Dataset]:
    """Filter the registry by explicit name, else by group."""
    if only:
        by_name = {item.name: item for item in datasets}
        if unknown := sorted(set(only) - set(by_name)):
            msg = f"unknown dataset(s): {', '.join(unknown)}"
            raise ValueError(msg)
        return [by_name[name] for name in only]
    return [item for item in datasets if item.group in groups]


def sha256_of(path: Path) -> str:
    """Stream a file through sha256 without holding it in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _open(url: str, *, offset: int = 0) -> HTTPResponse:
    """Open ``url``, optionally resuming from ``offset`` bytes."""
    headers = {"User-Agent": _USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https enforced by the caller
    return urllib.request.urlopen(request, timeout=_TIMEOUT_S)  # noqa: S310


def _stream(url: str, target: Path, *, progress: bool) -> None:
    """Download ``url`` to ``target`` via a resumable ``.part`` sidecar.

    A server that ignores the Range header answers 200 with the whole body; in
    that case the partial file is overwritten rather than appended to, so a
    resume against a non-Range host degrades to a restart instead of silently
    concatenating two copies.
    """
    part = target.with_name(f"{target.name}.part")
    offset = part.stat().st_size if part.exists() else 0
    with _open(url, offset=offset) as response:
        resumed = bool(offset) and response.status == 206
        if not resumed:
            offset = 0
        total = int(response.headers.get("Content-Length") or 0) + offset
        done = offset
        with part.open("ab" if resumed else "wb") as handle:
            while chunk := response.read(_CHUNK):
                handle.write(chunk)
                done += len(chunk)
                if progress and total:
                    print(
                        f"\r  {done / 1e6:,.0f} / {total / 1e6:,.0f} MB",
                        end="",
                        file=sys.stderr,
                    )
    if progress and total:
        print(file=sys.stderr)
    part.replace(target)


def _reconcile_existing(
    dataset: Dataset,
    item: DatasetFile,
    *,
    target: Path,
    lock: Lock,
) -> FileReport:
    """Report on a payload already on disk, pinning it if it is not yet locked."""
    key = f"{dataset.name}/{item.name}"
    actual = sha256_of(target)
    common = {"dataset": dataset.name, "file": item.name, "path": str(target)}
    if (locked := lock.get(key)) is None:
        lock.put(key, sha256=actual, size=target.stat().st_size)
        return FileReport(**common, status="pinned", ok=True, sha256=actual)
    if actual == locked["sha256"]:
        return FileReport(**common, status="already-present", ok=True)
    return FileReport(
        **common,
        status="hash-mismatch",
        ok=False,
        detail=f"expected {locked['sha256']}, on disk {actual}",
    )


def fetch_one(
    dataset: Dataset,
    item: DatasetFile,
    *,
    dest: Path,
    lock: Lock,
    progress: bool,
) -> FileReport:
    """Fetch one payload, verifying it against the lock when pinned."""
    key = f"{dataset.name}/{item.name}"
    target = dataset.directory(dest) / item.relative
    locked = lock.get(key)

    def report(status: Status, *, ok: bool, detail: str = "") -> FileReport:
        return FileReport(
            dataset=dataset.name,
            file=item.name,
            status=status,
            ok=ok,
            detail=detail,
            path=str(target),
        )

    if target.exists():
        return _reconcile_existing(dataset, item, target=target, lock=lock)

    if not item.url.startswith("https://"):
        return report("error", ok=False, detail="refusing non-https URL")
    target.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        print(f"fetching {key}", file=sys.stderr)
    try:
        _stream(item.url, target, progress=progress)
    except (OSError, urllib.error.URLError, ValueError) as error:
        return report("error", ok=False, detail=f"download failed: {error}")

    actual = sha256_of(target)
    size = target.stat().st_size
    if locked is not None and actual != locked["sha256"]:
        target.unlink()
        return report(
            "hash-mismatch",
            ok=False,
            detail=f"expected {locked['sha256']}, got {actual}; file discarded",
        )
    if locked is None:
        lock.put(key, sha256=actual, size=size)
        if size != item.expected_bytes:
            return FileReport(
                dataset=dataset.name,
                file=item.name,
                status="size-mismatch",
                ok=True,
                detail=(
                    f"registry records {item.expected_bytes:,} bytes, "
                    f"got {size:,}; upstream changed - review before committing "
                    f"the lock"
                ),
                path=str(target),
                sha256=actual,
            )
    return report("downloaded", ok=True)


def verify_one(
    dataset: Dataset, item: DatasetFile, *, dest: Path, lock: Lock
) -> FileReport:
    """Check one payload against the lock without touching the network."""
    key = f"{dataset.name}/{item.name}"
    target = dataset.directory(dest) / item.relative
    common = {"dataset": dataset.name, "file": item.name, "path": str(target)}
    if not target.exists():
        return FileReport(**common, status="missing", ok=False)
    if (locked := lock.get(key)) is None:
        return FileReport(
            **common,
            status="skipped",
            ok=True,
            detail="not pinned yet; fetch to record a hash",
        )
    if (actual := sha256_of(target)) != locked["sha256"]:
        return FileReport(
            **common,
            status="hash-mismatch",
            ok=False,
            detail=f"expected {locked['sha256']}, on disk {actual}",
        )
    return FileReport(**common, status="already-present", ok=True)


def _extract_zip(archive: Path, root: Path) -> None:
    """Unpack a zip, refusing any member that would escape ``root``.

    ``ZipFile.extractall`` does not validate member paths, so an archive with an
    absolute name or a ``..`` component can write outside the destination. These
    sources are trusted, but a fetcher that writes wherever a remote file says to
    is the wrong shape regardless.
    """
    resolved_root = root.resolve()
    with zipfile.ZipFile(archive) as handle:
        for info in handle.infolist():
            destination = (root / info.filename).resolve()
            if not destination.is_relative_to(resolved_root):
                msg = f"refusing zip member escaping the destination: {info.filename!r}"
                raise ValueError(msg)
        handle.extractall(root)  # noqa: S202 - every member validated above


def extract(dataset: Dataset, *, dest: Path, progress: bool) -> list[FileReport]:
    """Unpack a dataset's archives in place, skipping already-unpacked ones."""
    reports: list[FileReport] = []
    root = dataset.directory(dest)
    for item in dataset.files:
        archive = root / item.relative
        if not archive.exists():
            continue
        marker = archive.with_name(f"{archive.name}.extracted")
        if marker.exists():
            continue
        try:
            match dataset.extract:
                case "tar":
                    with tarfile.open(archive) as handle:
                        handle.extractall(root, filter="data")
                case "zip":
                    _extract_zip(archive, root)
                case "gz":
                    plain = archive.with_suffix("")
                    with gzip.open(archive) as source, plain.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
                case _:
                    continue
        except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as error:
            reports.append(
                FileReport(
                    dataset=dataset.name,
                    file=item.name,
                    status="error",
                    ok=False,
                    detail=f"extract failed: {error}",
                    path=str(archive),
                )
            )
            continue
        marker.touch()
        if progress:
            print(f"extracted {dataset.name}/{item.name}", file=sys.stderr)
    return reports


def extract_mobilebrick(*, dest: Path, progress: bool) -> list[FileReport]:
    """Pull only ``*/mesh/*.ply`` out of the 13 GB MobileBrick archive.

    The rest of the archive is RGBD imagery. Unpacking it whole would cost
    ~13 GB for ~300 MB of ground-truth geometry, so this is a member filter
    rather than a plain extract.
    """
    root = dest / "mobilebrick"
    archive = next(
        (
            path
            for path in (root / "MobileBrick_Mar23.zip", dest / "MobileBrick_Mar23.zip")
            if path.exists()
        ),
        None,
    )
    if archive is None:
        return [
            FileReport(
                dataset="mobilebrick",
                file="MobileBrick_Mar23.zip",
                status="missing",
                ok=False,
                detail=f"expected the zip at {root} or {dest}",
            )
        ]
    meshes = root / "meshes"
    meshes.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with zipfile.ZipFile(archive) as handle:
            wanted = [
                info
                for info in handle.infolist()
                if info.filename.endswith(".ply") and "/mesh/" in info.filename
            ]
            for info in wanted:
                sequence = Path(info.filename).parent.parent.name
                out = meshes / f"{sequence}.ply"
                if out.exists():
                    continue
                with handle.open(info) as source, out.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                written += 1
                if progress:
                    print(
                        f"\r  {written} / {len(wanted)} meshes", end="", file=sys.stderr
                    )
    except (OSError, zipfile.BadZipFile) as error:
        return [
            FileReport(
                dataset="mobilebrick",
                file="MobileBrick_Mar23.zip",
                status="error",
                ok=False,
                detail=f"selective extract failed: {error}",
                path=str(archive),
            )
        ]
    if progress:
        print(file=sys.stderr)
    return [
        FileReport(
            dataset="mobilebrick",
            file="mesh/*.ply",
            status="downloaded",
            ok=True,
            detail=f"{written} ground-truth meshes extracted",
            path=str(meshes),
        )
    ]


def size_table(datasets: Iterable[Dataset]) -> str:
    """Render the dry-run size table."""
    rows = [("dataset", "group", "files", "download", "licence")]
    total = 0
    for item in datasets:
        if not item.available:
            rows.append((item.name, item.group, "-", "UNAVAILABLE", item.license))
            continue
        total += item.total_bytes
        rows.append(
            (
                item.name,
                item.group,
                str(len(item.files)),
                _human(item.total_bytes),
                item.license,
            )
        )
    rows.append(("TOTAL", "", "", _human(total), ""))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    )


def _human(size: int) -> str:
    """Format a byte count in the largest unit that keeps it readable."""
    if size >= 1_000_000_000:
        return f"{size / 1e9:,.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1e6:,.1f} MB"
    return f"{size:,} B"


def _payloads(datasets: Iterable[Dataset]) -> Iterator[tuple[Dataset, DatasetFile]]:
    """Yield every (dataset, file) pair that is actually fetchable."""
    for dataset in datasets:
        if not dataset.available or dataset.group == "manual":
            continue
        for item in dataset.files:
            yield dataset, item


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--only", action="append", default=[], metavar="NAME")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        choices=["core", "large", "manual"],
        help="registry groups to fetch (default: core)",
    )
    parser.add_argument("--dest", type=Path, default=_DEFAULT_DEST)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the size table only"
    )
    parser.add_argument(
        "--verify", action="store_true", help="hash-check on disk, no network"
    )
    parser.add_argument(
        "--no-extract", action="store_true", help="download without unpacking"
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def _run_special(
    datasets: Iterable[Dataset],
    *,
    dest: Path,
    verify: bool,
    progress: bool,
) -> list[FileReport]:
    """Handle the entries that are not plain https payload downloads.

    Two shapes: sources the registry marks unavailable, and the ``manual`` group,
    whose payloads are acquired out of band. MobileBrick is the one manual entry
    with work to do here — a selective member extract against a zip that is
    already on disk.
    """
    reports: list[FileReport] = []
    for dataset in datasets:
        if not dataset.available:
            reports.append(
                FileReport(
                    dataset=dataset.name,
                    file="-",
                    status="unavailable",
                    ok=True,
                    detail="registry marks this source unavailable; see its notes",
                )
            )
        elif dataset.group != "manual":
            continue
        elif dataset.name == "mobilebrick" and not verify:
            reports.extend(extract_mobilebrick(dest=dest, progress=progress))
        else:
            reports.append(
                FileReport(
                    dataset=dataset.name,
                    file="-",
                    status="skipped",
                    ok=True,
                    detail="manual group; acquired out of band",
                )
            )
    return reports


def main(argv: list[str] | None = None) -> int:
    """Fetch, verify, or price the registered datasets."""
    args = parse_args(argv)
    progress = not args.quiet
    try:
        datasets = select(
            load_registry(),
            only=tuple(args.only),
            groups=tuple(args.group) or ("core",),
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(size_table(datasets))
        return 0

    lock = Lock.load()
    reports = _run_special(
        datasets, dest=args.dest, verify=args.verify, progress=progress
    )
    for dataset, item in _payloads(datasets):
        reports.append(
            verify_one(dataset, item, dest=args.dest, lock=lock)
            if args.verify
            else fetch_one(dataset, item, dest=args.dest, lock=lock, progress=progress)
        )

    if not args.verify:
        lock.write()
        if not args.no_extract:
            for dataset in datasets:
                if dataset.extract and dataset.available:
                    reports.extend(extract(dataset, dest=args.dest, progress=progress))

    for report in reports:
        print(report.line)
    failed = [report for report in reports if not report.ok]
    if failed:
        print(f"\n{len(failed)} of {len(reports)} failed", file=sys.stderr)
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
