"""Crawl the LDraw Official Model Repository for official-set MPD files.

The OMR has no bulk archive and no API, and `library.ldraw.org/library/omr/`
itself returns 403, so the only route is the set pages. Measured structure
(2026-08-08):

  - set pages live at ``https://library.ldraw.org/omr/sets/<id>`` for id ~1..4000
    (5000 returns 404);
  - each page links its models directly as
    ``https://library.ldraw.org/library/omr/<name>.mpd``.

This is therefore a **polite crawl**, not a pinned download, and is kept apart
from ``fetch_datasets.py`` for that reason. It rate-limits, resumes from an
on-disk index, and never re-fetches a model it already has.

Every OMR file carries its own licence in a ``0 !LICENSE`` header line (CCAL 2.0
for older files, CC BY 4.0 for newer). The crawler records that line per model in
``index.json`` so attribution is derived from the files rather than assumed.

Usage::

    uv run python scripts/fetch_omr.py --limit 20          # smoke run
    uv run python scripts/fetch_omr.py                     # full crawl
    uv run python scripts/fetch_omr.py --index-only        # discover, do not download
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO = Path(__file__).parent.parent
_DEFAULT_DEST = _REPO / "datasets" / "omr"

_SET_URL = "https://library.ldraw.org/omr/sets/{set_id}"
_MPD_PATTERN = re.compile(
    r'href="(https://library\.ldraw\.org/library/omr/([^"]+\.mpd))"',
    re.IGNORECASE,
)
_LICENSE_PATTERN = re.compile(rb"^0[ \t]+!LICENSE[ \t]+([^\r\n]+)", re.MULTILINE)
# At least one real OMR file (6386-1.mpd) runs its !LICENSE and the following
# meta-command together on one physical line, with no break between
# "CAreadme.txt" and "0 !HELP ...". Capturing to end-of-line would then fold a
# copyright notice into the licence string and split the attribution histogram
# in two. Trim anything from a run-together `0 !META` onwards.
_TRAILING_META = re.compile(r"\s*0\s+![A-Z].*$", re.DOTALL)
_TITLE_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")

_TIMEOUT_S = 30.0
_MAX_SET_ID = 4200
# Consecutive discovery failures that read as a site outage rather than a few
# flaky pages. Aborting early keeps an outage from burning one delay per
# remaining set id; the failed ids stay unvisited, so the next run retries.
_OUTAGE_THRESHOLD = 10
_USER_AGENT = (
    "legolization-omr-crawler/1 (+https://github.com/hbmartin/legolization; "
    "research use, rate-limited)"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class OmrModel:
    """One MPD discovered on a set page."""

    set_id: int
    set_title: str
    name: str
    url: str
    license: str = ""
    bytes: int = 0

    @property
    def filename(self) -> str:
        """Local filename for this model."""
        return self.name


@dataclass(slots=True)
class Index:
    """The resumable crawl index: discovered models plus visited set ids."""

    models: dict[str, OmrModel] = field(default_factory=dict)
    visited: set[int] = field(default_factory=set)
    generated: str = ""

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read an existing index, treating an absent file as an empty one."""
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            models={
                name: OmrModel(**row) for name, row in payload.get("models", {}).items()
            },
            visited=set(payload.get("visited", [])),
            generated=payload.get("generated", ""),
        )

    def write(self, path: Path) -> None:
        """Write the index atomically, sorted for a stable diff."""
        payload = {
            "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "https://library.ldraw.org/omr",
            "note": (
                "Licence strings are the verbatim '0 !LICENSE' header line of "
                "each MPD. Attribution is derived from the files, not assumed."
            ),
            "visited": sorted(self.visited),
            "models": {name: asdict(self.models[name]) for name in sorted(self.models)},
        }
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(path)


class FetchFailure(Enum):
    """Why a fetch produced no body, and whether that answer is final.

    The distinction is load-bearing for the resumable index: a definitively
    absent page may be recorded as visited, but a timeout, DNS failure, or
    rate-limit response must not be - marking it visited would silently drop
    that set from every future resumed crawl.
    """

    ABSENT = "absent"  # HTTP 404/410 - the resource is not there
    TRANSIENT = "transient"  # timeout, DNS, 5xx, 429 - retry on a later crawl


def _get(url: str) -> bytes | FetchFailure:
    """Fetch a URL, or say whether the failure is definitive or retryable."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310 - https literal
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as error:
        return (
            FetchFailure.ABSENT if error.code in (404, 410) else FetchFailure.TRANSIENT
        )
    except (urllib.error.URLError, OSError):
        return FetchFailure.TRANSIENT


def _strip_tags(html: str) -> str:
    """Collapse an HTML fragment to plain text."""
    return " ".join(_TAG_PATTERN.sub(" ", html).split())


def discover(set_id: int) -> tuple[str, list[OmrModel]] | None:
    """Return a set page's title and models, or classify the failure.

    ``("", [])`` means the set page is definitively absent and may be recorded
    as visited; ``None`` means the fetch failed transiently and the set must be
    retried on a later crawl.
    """
    body = _get(_SET_URL.format(set_id=set_id))
    if isinstance(body, FetchFailure):
        return None if body is FetchFailure.TRANSIENT else ("", [])
    html = body.decode("utf-8", errors="replace")
    title = (
        _strip_tags(match.group(1)) if (match := _TITLE_PATTERN.search(html)) else ""
    )
    seen: dict[str, OmrModel] = {}
    for url, name in _MPD_PATTERN.findall(html):
        # The name becomes a local filename joined onto the crawl directory.
        # A page (or a compromised page) offering a separator or a dotfile
        # could otherwise write outside it.
        if "/" in name or "\\" in name or name.startswith("."):
            continue
        seen.setdefault(
            name, OmrModel(set_id=set_id, set_title=title, name=name, url=url)
        )
    return title, list(seen.values())


def download(model: OmrModel, *, dest: Path) -> OmrModel | str:
    """Fetch one MPD, returning the model enriched with licence and size.

    Returns a failure string rather than raising, so one dead link never ends a
    crawl of thousands.
    """
    # discover() filters names, but this is the last line before a filesystem
    # access - a hand-edited index or a future caller must not bypass it.
    name = model.filename
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return f"{model.name}: download failed (unsafe filename {name!r})"
    target = dest / name
    if target.resolve().parent != dest.resolve():
        return f"{model.name}: download failed (filename escapes {dest})"
    if target.exists():
        body = target.read_bytes()
    elif isinstance(fetched := _get(model.url), FetchFailure):
        return f"{model.name}: download failed ({fetched.value})"
    else:
        body = fetched
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.part")
        temp.write_bytes(body)
        temp.replace(target)
    return OmrModel(
        set_id=model.set_id,
        set_title=model.set_title,
        name=model.name,
        url=model.url,
        license=license_of(body),
        bytes=len(body),
    )


def license_of(body: bytes) -> str:
    """Read the ``0 !LICENSE`` header line, or ``""`` when the file has none.

    Attribution is derived from the files rather than assumed, so this has to
    survive malformed headers - see ``_TRAILING_META``.
    """
    if (match := _LICENSE_PATTERN.search(body)) is None:
        return ""
    raw = match.group(1).decode("utf-8", errors="replace")
    return _TRAILING_META.sub("", raw).strip()


def _pending(index: Index, *, limit: int) -> Iterator[int]:
    """Yield the next set ids to visit, honouring ``--limit``."""
    yielded = 0
    for set_id in range(1, _MAX_SET_ID + 1):
        if set_id in index.visited:
            continue
        if limit and yielded >= limit:
            return
        yielded += 1
        yield set_id


def _download_matches(
    model: OmrModel,
    *,
    expected_bytes: int,
    dest: Path,
) -> bool:
    """Whether the recorded download still exists at its expected size."""
    target = dest / "ldraw" / model.filename
    try:
        return target.is_file() and target.stat().st_size == expected_bytes
    except OSError:
        return False


def _record_models(
    models: list[OmrModel],
    index: Index,
    *,
    index_only: bool,
    dest: Path,
    delay: float,
) -> list[str]:
    """Download or register each discovered model, returning failures."""
    failures: list[str] = []
    for model in models:
        recorded = index.models.get(model.name)
        if (
            recorded is not None
            and recorded.bytes
            and _download_matches(
                model,
                expected_bytes=recorded.bytes,
                dest=dest,
            )
        ):
            continue
        if index_only:
            index.models.setdefault(model.name, model)
            continue
        time.sleep(delay)
        match download(model, dest=dest / "ldraw"):
            case str() as failure:
                failures.append(failure)
            case OmrModel() as fetched:
                index.models[fetched.name] = fetched
    return failures


def crawl(
    *,
    dest: Path,
    delay: float,
    limit: int,
    index_only: bool,
    progress: bool,
) -> tuple[Index, list[str]]:
    """Walk set ids, recording models and optionally downloading them."""
    index_path = dest / "index.json"
    index = Index.load(index_path)
    failures: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)

    consecutive_failures = 0
    for set_id in _pending(index, limit=limit):
        if (discovered := discover(set_id)) is None:
            failures.append(f"set {set_id}: transient fetch failure; will retry")
            consecutive_failures += 1
            if consecutive_failures >= _OUTAGE_THRESHOLD:
                failures.append(
                    f"aborting after {consecutive_failures} consecutive discovery "
                    "failures - the site looks down; unvisited sets retry next run"
                )
                break
            time.sleep(delay)
            continue
        consecutive_failures = 0
        _, models = discovered
        index.visited.add(set_id)
        failures.extend(
            _record_models(models, index, index_only=index_only, dest=dest, delay=delay)
        )
        if progress:
            print(
                f"\rset {set_id}/{_MAX_SET_ID}  models={len(index.models)}  "
                f"failures={len(failures)}",
                end="",
                file=sys.stderr,
            )
        index.write(index_path)
        time.sleep(delay)

    if progress:
        print(file=sys.stderr)
    return index, failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dest", type=Path, default=_DEFAULT_DEST)
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="seconds between requests (default 1.0; do not lower without reason)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="visit at most N new set ids (0 = all); use for a smoke run",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="discover model URLs without downloading them",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Crawl the OMR and report what landed."""
    args = parse_args(argv)
    if args.delay < 0.5:
        print(
            "refusing a delay below 0.5s; this is a shared community host",
            file=sys.stderr,
        )
        return 1
    index, failures = crawl(
        dest=args.dest,
        delay=args.delay,
        limit=args.limit,
        index_only=args.index_only,
        progress=not args.quiet,
    )
    downloaded = [model for model in index.models.values() if model.bytes]
    total = sum(model.bytes for model in downloaded)
    licences = sorted({model.license for model in downloaded if model.license})
    print(
        f"sets visited={len(index.visited)} models known={len(index.models)} "
        f"downloaded={len(downloaded)} bytes={total:,} failures={len(failures)}"
    )
    for licence in licences:
        share = sum(1 for model in downloaded if model.license == licence)
        print(f"  licence  {share:5d}  {licence}")
    if unlicensed := [model.name for model in downloaded if not model.license]:
        print(f"  NO !LICENSE header: {len(unlicensed)} model(s)")
    for failure in failures[:20]:
        print(f"  failed   {failure}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
