"""Authoritative part-data sources for catalog inference, fully cited.

The confirmed lookup ladder, in order:

1. a local BrickLink ``Parts.txt`` catalog dump (``./Parts.txt`` or
   ``$LEGOLIZATION_BRICKLINK_DUMP``) for a measured mass, cited as
   ``bricklink-catalog-dump``;
2. the Rebrickable API v3 part endpoint, only when
   ``$REBRICKABLE_API_KEY`` is set, for identity/name confirmation;
3. the BrickOwl API, only when ``$BRICKOWL_API_KEY`` is set, for an
   independent weight.

No HTML scraping. Network access uses stdlib ``urllib`` with 5-second
per-request timeouts under one bounded total budget; the fetch callable
is injectable for tests. Offline or keyless rungs are recorded as
``skipped`` rows with a reason — every rung always appears in
``sources.json``.
"""

from __future__ import annotations

import csv
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

SOURCES_SCHEMA = "legolization.catalog-sources/v1"

ENV_BRICKLINK_DUMP = "LEGOLIZATION_BRICKLINK_DUMP"
ENV_REBRICKABLE_KEY = "REBRICKABLE_API_KEY"
ENV_BRICKOWL_KEY = "BRICKOWL_API_KEY"

_REQUEST_TIMEOUT_S = 5.0
_TOTAL_BUDGET_S = 15.0
_REVISION_SUFFIX = re.compile(r"(?<=\d)[a-z]$")

_BRICKLINK_LICENSE = "BrickLink catalog data (local export); https://www.bricklink.com"
_REBRICKABLE_LICENSE = "Data provided by Rebrickable; https://rebrickable.com"
_BRICKOWL_LICENSE = "Data provided by BrickOwl; https://www.brickowl.com"

type LookupStatus = Literal["ok", "skipped", "error"]
type Fetch = Callable[[str, dict[str, str], float], bytes]
"""Fetch ``(url, headers, timeout_s)`` -> body bytes; raises ``OSError``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceLookup:
    """One fully cited row of the source-lookup ladder."""

    source: str
    url: str | None
    status: LookupStatus
    reason: str | None
    retrieved_at: str | None
    fields: dict[str, object] = field(default_factory=dict)
    license: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this lookup row."""
        return {
            "source": self.source,
            "url": self.url,
            "status": self.status,
            "reason": self.reason,
            "retrieved_at": self.retrieved_at,
            "fields": dict(self.fields),
            "license": self.license,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceReport:
    """Every ladder rung's outcome for one part lookup."""

    part_id: str
    lookups: tuple[SourceLookup, ...]

    @property
    def measured(self) -> SourceLookup | None:
        """The first successful lookup carrying a measured mass."""
        return next(
            (
                lookup
                for lookup in self.lookups
                if lookup.status == "ok" and "mass_g" in lookup.fields
            ),
            None,
        )

    @property
    def confirmed_name(self) -> str | None:
        """The first source-confirmed part name, if any."""
        return next(
            (
                str(lookup.fields["name"])
                for lookup in self.lookups
                if lookup.status == "ok" and lookup.fields.get("name")
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the ``sources.json`` payload."""
        return {
            "schema": SOURCES_SCHEMA,
            "part_id": self.part_id,
            "lookups": [lookup.to_dict() for lookup in self.lookups],
        }


@dataclass(slots=True, kw_only=True)
class _LadderState:
    """Deadline, environment, and seams threaded through the rungs."""

    env: Mapping[str, str]
    fetch: Fetch
    now: Callable[[], str]
    offline: bool
    deadline: float

    def remaining_s(self) -> float:
        return self.deadline - time.monotonic()


def lookup_sources(  # noqa: PLR0913 - every seam is injectable for tests
    part_id: str,
    *,
    offline: bool = False,
    dump_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    fetch: Fetch | None = None,
    now: Callable[[], str] | None = None,
    budget_s: float = _TOTAL_BUDGET_S,
) -> SourceReport:
    """Run the confirmed source ladder for one LDraw part id."""
    # lizard forgives(parameter_count)
    import os  # noqa: PLC0415

    state = _LadderState(
        env=os.environ if env is None else env,
        fetch=fetch if fetch is not None else _urllib_fetch,
        now=now if now is not None else _utc_now,
        offline=offline,
        deadline=time.monotonic() + budget_s,
    )
    lookups = (
        _bricklink_dump_lookup(part_id, state, dump_path),
        _rebrickable_lookup(part_id, state),
        _brickowl_lookup(part_id, state),
    )
    return SourceReport(part_id=part_id, lookups=lookups)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _urllib_fetch(url: str, headers: dict[str, str], timeout_s: float) -> bytes:
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https constants
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return cast("bytes", response.read())
    except urllib.error.HTTPError as error:
        msg = f"HTTP {error.code}"
        raise OSError(msg) from error
    except urllib.error.URLError as error:
        msg = str(error.reason)
        raise OSError(msg) from error


def _skipped(source: str, reason: str, *, url: str | None = None) -> SourceLookup:
    return SourceLookup(
        source=source,
        url=url,
        status="skipped",
        reason=reason,
        retrieved_at=None,
    )


# --- rung 1: local BrickLink catalog dump ---------------------------------


def _dump_location(state: _LadderState, dump_path: Path | None) -> Path:
    if dump_path is not None:
        return dump_path
    if override := state.env.get(ENV_BRICKLINK_DUMP):
        return Path(override)
    return Path.cwd() / "Parts.txt"


def _bricklink_dump_lookup(
    part_id: str,
    state: _LadderState,
    dump_path: Path | None,
) -> SourceLookup:
    source = "bricklink-catalog-dump"
    location = _dump_location(state, dump_path)
    if not location.is_file():
        return _skipped(
            source,
            f"no local BrickLink dump at {location} "
            f"(./Parts.txt or ${ENV_BRICKLINK_DUMP})",
        )
    url = location.as_uri()
    try:
        row = _resolve_in_dump(part_id, _load_dump_rows(location))
    except (OSError, ValueError) as error:
        return SourceLookup(
            source=source,
            url=url,
            status="error",
            reason=f"unusable BrickLink dump: {error}",
            retrieved_at=None,
        )
    if row is None:
        return _skipped(source, f"no dump entry matches part {part_id!r}", url=url)
    fields: dict[str, object] = {"number": row.number, "name": row.name}
    reason = None
    if row.mass_g is not None:
        fields["mass_g"] = row.mass_g
    else:
        reason = f"dump entry {row.number} carries no measured weight"
    return SourceLookup(
        source=source,
        url=url,
        status="ok",
        reason=reason,
        retrieved_at=state.now(),
        fields=fields,
        license=_BRICKLINK_LICENSE,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _DumpRow:
    """One usable row of a BrickLink tab-separated parts export."""

    number: str
    name: str
    alternates: tuple[str, ...]
    mass_g: float | None


def _normalize_number(value: str) -> str:
    return value.strip().lower().removesuffix(".dat")


def _load_dump_rows(path: Path) -> tuple[_DumpRow, ...]:
    """Parse the columns the ladder needs from a BrickLink ``Parts.txt``.

    Mirrors ``scripts/ingest_bricklink_masses.py``: tab-separated with
    ``Number``, ``Name``, ``Alternate Item Number``, and ``Weight (in
    Grams)`` columns; ``?``/empty weights read as missing.
    """
    rows: list[_DumpRow] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        columns = frozenset(reader.fieldnames or ())
        required = {"Number", "Name", "Weight (in Grams)"}
        if missing := sorted(required - columns):
            msg = f"{path} is missing columns: {', '.join(missing)}"
            raise ValueError(msg)
        for raw in reader:
            number = (raw.get("Number") or "").strip()
            if not number:
                continue
            weight = (raw.get("Weight (in Grams)") or "").strip()
            mass_g = None
            if weight not in {"", "?"}:
                try:
                    mass_g = float(weight)
                except ValueError:
                    mass_g = None
                else:
                    if not math.isfinite(mass_g) or mass_g <= 0:
                        mass_g = None
            rows.append(
                _DumpRow(
                    number=number,
                    name=(raw.get("Name") or "").strip(),
                    alternates=tuple(
                        _normalize_number(item)
                        for item in (raw.get("Alternate Item Number") or "").split(",")
                        if item.strip()
                    ),
                    mass_g=mass_g,
                )
            )
    return tuple(rows)


def _resolve_in_dump(
    part_id: str,
    rows: tuple[_DumpRow, ...],
) -> _DumpRow | None:
    """Resolve one part id with the ingest script's conservative ladder.

    Exact number, then a unique alternate number, then both again with
    a trailing revision letter removed.
    """
    primary = {_normalize_number(row.number): row for row in rows}
    alternates: dict[str, list[_DumpRow]] = {}
    for row in rows:
        for number in row.alternates:
            alternates.setdefault(number, []).append(row)
    for candidate in _candidate_numbers(part_id):
        if (row := primary.get(candidate)) is not None:
            return row
        if len(matched := alternates.get(candidate, [])) == 1:
            return matched[0]
    return None


def _candidate_numbers(part_id: str) -> tuple[str, ...]:
    number = _normalize_number(part_id)
    base = _REVISION_SUFFIX.sub("", number)
    return (number,) if base == number else (number, base)


# --- rung 2: Rebrickable API (identity confirmation) ----------------------


def _rebrickable_lookup(part_id: str, state: _LadderState) -> SourceLookup:
    source = "rebrickable-api"
    url = f"https://rebrickable.com/api/v3/lego/parts/{part_id}/"
    if state.offline:
        return _skipped(source, "offline mode", url=url)
    key = state.env.get(ENV_REBRICKABLE_KEY)
    if not key:
        return _skipped(source, f"${ENV_REBRICKABLE_KEY} is not set", url=url)
    payload = _fetch_json(
        state,
        source=source,
        url=url,
        headers={"Authorization": f"key {key}"},
    )
    if isinstance(payload, SourceLookup):
        return payload
    return SourceLookup(
        source=source,
        url=url,
        status="ok",
        reason=None,
        retrieved_at=state.now(),
        fields={
            "part_num": str(payload.get("part_num", part_id)),
            "name": str(payload.get("name", "")),
        },
        license=_REBRICKABLE_LICENSE,
    )


# --- rung 3: BrickOwl API (independent weight) ----------------------------


def _brickowl_lookup(part_id: str, state: _LadderState) -> SourceLookup:
    source = "brickowl-api"
    cited_url = (
        "https://api.brickowl.com/v1/catalog/lookup"
        f"?key=REDACTED&type=Part&id_type=design_id&id={part_id}"
    )
    if state.offline:
        return _skipped(source, "offline mode", url=cited_url)
    key = state.env.get(ENV_BRICKOWL_KEY)
    if not key:
        return _skipped(source, f"${ENV_BRICKOWL_KEY} is not set", url=cited_url)
    request_url = cited_url.replace("key=REDACTED", f"key={key}")
    payload = _fetch_json(
        state,
        source=source,
        url=request_url,
        headers={},
        cited_url=cited_url,
    )
    if isinstance(payload, SourceLookup):
        return payload
    mass_g = _first_weight_g(payload)
    fields: dict[str, object] = {}
    if (name := _first_string(payload, "name")) is not None:
        fields["name"] = name
    if mass_g is not None:
        fields["mass_g"] = mass_g
    return SourceLookup(
        source=source,
        url=cited_url,
        status="ok",
        reason=None if mass_g is not None else "response carries no weight",
        retrieved_at=state.now(),
        fields=fields,
        license=_BRICKOWL_LICENSE,
    )


def _fetch_json(
    state: _LadderState,
    *,
    source: str,
    url: str,
    headers: dict[str, str],
    cited_url: str | None = None,
) -> dict[str, Any] | SourceLookup:
    """Fetch one JSON document, or the error/skip row that replaces it."""
    recorded = cited_url if cited_url is not None else url
    remaining = state.remaining_s()
    if remaining <= 0:
        return _skipped(source, "source-lookup time budget exhausted", url=recorded)
    try:
        body = state.fetch(url, headers, min(_REQUEST_TIMEOUT_S, remaining))
        payload = json.loads(body)
    except (OSError, ValueError) as error:
        return SourceLookup(
            source=source,
            url=recorded,
            status="error",
            reason=str(error),
            retrieved_at=None,
        )
    if not isinstance(payload, dict):
        return SourceLookup(
            source=source,
            url=recorded,
            status="error",
            reason="response is not a JSON object",
            retrieved_at=None,
        )
    return cast("dict[str, Any]", payload)


def _first_weight_g(payload: object) -> float | None:
    """Find the first positive ``weight`` value (grams) in a response."""
    match payload:
        case dict() as raw:
            mapping = cast("dict[str, object]", raw)
            if (weight := _positive_float(mapping.get("weight"))) is not None:
                return weight
            for value in mapping.values():
                if (found := _first_weight_g(value)) is not None:
                    return found
        case list() as items:
            for value in cast("list[object]", items):
                if (found := _first_weight_g(value)) is not None:
                    return found
        case _:
            return None
    return None


def _first_string(payload: object, key: str) -> str | None:
    """Find the first non-empty string under ``key`` in a response."""
    match payload:
        case dict() as mapping:
            typed = cast("dict[str, object]", mapping)
            if isinstance(value := typed.get(key), str) and value:
                return value
            for child in typed.values():
                if (found := _first_string(child, key)) is not None:
                    return found
        case list() as items:
            for child in cast("list[object]", items):
                if (found := _first_string(child, key)) is not None:
                    return found
        case _:
            return None
    return None


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(cast("str | float", value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None
