"""Audit or update the built-in part masses from BrickLink ``Parts.txt``.

BrickLink's catalog export is tab-separated and identifies mass in the
``Weight (in Grams)`` column.  A dry run is the default: it resolves every
catalog LDraw part, reports changed or unresolved entries, and leaves the
catalog untouched.  Pass ``--write`` to atomically replace resolved
``mass_g`` values in the selected catalog.

Matching is deliberately conservative and ordered:

1. exact BrickLink item number;
2. exact entry in BrickLink's comma-separated alternate-number field;
3. an LDraw trailing revision letter removed (for example ``3040b`` →
   ``3040``), then the first two rules again.

Usage::

    uv run python scripts/ingest_bricklink_masses.py Parts.txt
    uv run python scripts/ingest_bricklink_masses.py Parts.txt --write
    uv run python scripts/ingest_bricklink_masses.py Parts.txt --json report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_PARTS = _REPO / "Parts.txt"
_DEFAULT_CATALOG = _REPO / "src" / "legolization" / "data" / "catalog.json"
_REQUIRED_COLUMNS = frozenset(
    {
        "Category ID",
        "Category Name",
        "Number",
        "Name",
        "Alternate Item Number",
        "Weight (in Grams)",
        "Dimensions",
    }
)
_REVISION_SUFFIX = re.compile(r"(?<=\d)[a-z]$")
_JSON_NUMBER = r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?"
_MISSING_VALUES = frozenset({"", "?"})

MatchKind = Literal[
    "exact",
    "alternate",
    "revision",
    "revision_alternate",
    "not_found",
    "ambiguous",
    "missing_mass",
]


@dataclass(frozen=True, slots=True)
class BrickLinkPart:
    """One row from BrickLink's tab-separated parts export."""

    category_id: int
    category_name: str
    number: str
    name: str
    alternate_numbers: tuple[str, ...]
    mass_g: float | None
    dimensions: str


@dataclass(frozen=True, slots=True)
class PartIndex:
    """BrickLink records indexed by primary and alternate item numbers."""

    primary: dict[str, BrickLinkPart]
    alternate: dict[str, tuple[BrickLinkPart, ...]]


@dataclass(frozen=True, slots=True)
class MassAuditRow:
    """Resolution and mass comparison for one legolization catalog entry."""

    part_key: str
    ldraw_part: str
    catalog_mass_g: float
    match: MatchKind
    bricklink_number: str | None = None
    bricklink_name: str | None = None
    bricklink_mass_g: float | None = None
    delta_g: float | None = None
    delta_percent: float | None = None
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        """Whether this row supplies a usable BrickLink mass."""
        return self.bricklink_mass_g is not None

    @property
    def changed(self) -> bool:
        """Whether importing this row changes the catalog mass."""
        return self.delta_g is not None and not math.isclose(
            self.delta_g,
            0.0,
            abs_tol=1.0e-12,
        )


@dataclass(frozen=True, slots=True)
class MassAudit:
    """Complete catalog audit against one BrickLink export."""

    rows: tuple[MassAuditRow, ...]

    @property
    def resolved_count(self) -> int:
        """Number of catalog entries with a usable imported mass."""
        return sum(row.resolved for row in self.rows)

    @property
    def changed_count(self) -> int:
        """Number of catalog entries whose mass differs from BrickLink."""
        return sum(row.changed for row in self.rows)

    @property
    def unresolved_count(self) -> int:
        """Number of catalog entries without a usable imported mass."""
        return len(self.rows) - self.resolved_count

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe audit representation."""
        return {
            "summary": {
                "catalog_parts": len(self.rows),
                "resolved": self.resolved_count,
                "changed": self.changed_count,
                "unresolved": self.unresolved_count,
            },
            "parts": [asdict(row) for row in self.rows],
        }


def normalize_part_number(value: str) -> str:
    """Normalize a BrickLink or LDraw part identifier for matching."""
    return value.strip().lower().removesuffix(".dat")


def _required(row: dict[str, str | None], field: str, line: int) -> str:
    value = (row.get(field) or "").strip()
    if not value:
        msg = f"Parts.txt line {line}: missing {field!r}"
        raise ValueError(msg)
    return value


def _parse_mass(value: str, line: int) -> float | None:
    if value in _MISSING_VALUES:
        return None
    try:
        mass = float(value)
    except ValueError as error:
        msg = f"Parts.txt line {line}: invalid mass {value!r}"
        raise ValueError(msg) from error
    if not math.isfinite(mass) or mass <= 0:
        msg = f"Parts.txt line {line}: mass must be finite and positive, got {value!r}"
        raise ValueError(msg)
    return mass


def load_parts(path: Path) -> list[BrickLinkPart]:
    """Parse and validate a BrickLink ``Parts.txt`` export."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        columns = frozenset(reader.fieldnames or ())
        if missing := sorted(_REQUIRED_COLUMNS - columns):
            msg = f"{path} is missing columns: {', '.join(missing)}"
            raise ValueError(msg)
        parts: list[BrickLinkPart] = []
        seen: set[str] = set()
        for line, raw in enumerate(reader, start=2):
            if not any(raw.values()):
                continue
            number = _required(raw, "Number", line)
            normalized = normalize_part_number(number)
            if normalized in seen:
                msg = f"Parts.txt line {line}: duplicate item number {number!r}"
                raise ValueError(msg)
            seen.add(normalized)
            alternates = tuple(
                normalize_part_number(item)
                for item in (raw.get("Alternate Item Number") or "").split(",")
                if item.strip()
            )
            category_id_text = _required(raw, "Category ID", line)
            try:
                category_id = int(category_id_text)
            except ValueError as error:
                msg = f"Parts.txt line {line}: invalid category ID {category_id_text!r}"
                raise ValueError(msg) from error
            parts.append(
                BrickLinkPart(
                    category_id=category_id,
                    category_name=_required(raw, "Category Name", line),
                    number=number.strip(),
                    name=_required(raw, "Name", line),
                    alternate_numbers=alternates,
                    mass_g=_parse_mass(
                        (raw.get("Weight (in Grams)") or "").strip(),
                        line,
                    ),
                    dimensions=(raw.get("Dimensions") or "").strip(),
                )
            )
    return parts


def build_index(parts: list[BrickLinkPart]) -> PartIndex:
    """Index parsed parts without hiding ambiguous alternate mappings."""
    primary = {normalize_part_number(part.number): part for part in parts}
    alternate_lists: dict[str, list[BrickLinkPart]] = {}
    for part in parts:
        for number in part.alternate_numbers:
            alternate_lists.setdefault(number, []).append(part)
    alternate = {
        number: tuple(sorted(matches, key=lambda part: part.number))
        for number, matches in alternate_lists.items()
    }
    return PartIndex(primary=primary, alternate=alternate)


def _lookup(
    number: str,
    index: PartIndex,
    *,
    exact_kind: MatchKind,
    alternate_kind: MatchKind,
) -> tuple[MatchKind, tuple[BrickLinkPart, ...]] | None:
    if part := index.primary.get(number):
        return exact_kind, (part,)
    if matches := index.alternate.get(number):
        return alternate_kind, matches
    return None


def resolve_part(
    ldraw_part: str,
    index: PartIndex,
) -> tuple[MatchKind, tuple[BrickLinkPart, ...]]:
    """Resolve one LDraw identifier using the documented match order."""
    number = normalize_part_number(ldraw_part)
    if (
        found := _lookup(
            number,
            index,
            exact_kind="exact",
            alternate_kind="alternate",
        )
    ) or (
        (base := _REVISION_SUFFIX.sub("", number)) != number
        and (
            found := _lookup(
                base,
                index,
                exact_kind="revision",
                alternate_kind="revision_alternate",
            )
        )
    ):
        kind, matches = found
    else:
        return "not_found", ()
    if len(matches) > 1:
        return "ambiguous", matches
    if matches[0].mass_g is None:
        return "missing_mass", matches
    return kind, matches


def load_catalog_document(path: Path) -> dict[str, Any]:
    """Load the versioned catalog JSON used by legolization."""
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("schema") != 1:
        msg = f"{path} must be a schema-1 catalog object"
        raise ValueError(msg)
    if not isinstance(document.get("parts"), list):
        msg = f"{path} must contain a parts list"
        raise TypeError(msg)
    return cast("dict[str, Any]", document)


def audit_catalog(document: dict[str, Any], index: PartIndex) -> MassAudit:
    """Compare every catalog mass with its resolved BrickLink record."""
    rows: list[MassAuditRow] = []
    for position, raw in enumerate(document["parts"], start=1):
        if not isinstance(raw, dict):
            msg = f"catalog part {position} must be an object"
            raise TypeError(msg)
        spec = cast("dict[str, Any]", raw)
        try:
            part_key = str(spec["key"])
            ldraw_part = str(spec["ldraw_part"])
            catalog_mass = float(spec["mass_g"])
        except (KeyError, TypeError, ValueError) as error:
            msg = f"catalog part {position} has invalid key, ldraw_part, or mass_g"
            raise ValueError(msg) from error
        kind, matches = resolve_part(ldraw_part, index)
        if len(matches) != 1:
            rows.append(
                MassAuditRow(
                    part_key=part_key,
                    ldraw_part=ldraw_part,
                    catalog_mass_g=catalog_mass,
                    match=kind,
                    candidates=tuple(part.number for part in matches),
                )
            )
            continue
        match = matches[0]
        if match.mass_g is None:
            rows.append(
                MassAuditRow(
                    part_key=part_key,
                    ldraw_part=ldraw_part,
                    catalog_mass_g=catalog_mass,
                    match=kind,
                    bricklink_number=match.number,
                    bricklink_name=match.name,
                )
            )
            continue
        delta = match.mass_g - catalog_mass
        rows.append(
            MassAuditRow(
                part_key=part_key,
                ldraw_part=ldraw_part,
                catalog_mass_g=catalog_mass,
                match=kind,
                bricklink_number=match.number,
                bricklink_name=match.name,
                bricklink_mass_g=match.mass_g,
                delta_g=round(delta, 6),
                delta_percent=round(100.0 * delta / catalog_mass, 3),
            )
        )
    return MassAudit(rows=tuple(rows))


def apply_masses(document: dict[str, Any], audit: MassAudit) -> int:
    """Apply every resolved mass to the in-memory catalog document."""
    parts = cast("list[dict[str, Any]]", document["parts"])
    if len(parts) != len(audit.rows):
        msg = "catalog changed after audit"
        raise ValueError(msg)
    updated = 0
    for spec, row in zip(parts, audit.rows, strict=True):
        if str(spec.get("key")) != row.part_key:
            msg = "catalog part order changed after audit"
            raise ValueError(msg)
        if row.bricklink_mass_g is None:
            continue
        if row.changed:
            updated += 1
        spec["mass_g"] = row.bricklink_mass_g
    return updated


def write_catalog(path: Path, audit: MassAudit) -> None:
    """Atomically replace only resolved ``mass_g`` tokens in a catalog."""
    serialized = path.read_text()
    for row in audit.rows:
        if row.bricklink_mass_g is None:
            continue
        key = re.escape(json.dumps(row.part_key))
        pattern = re.compile(
            rf'("key"\s*:\s*{key}.*?"mass_g"\s*:\s*){_JSON_NUMBER}',
            flags=re.DOTALL,
        )
        serialized, replacements = pattern.subn(
            rf"\g<1>{json.dumps(row.bricklink_mass_g)}",
            serialized,
            count=1,
        )
        if replacements != 1:
            msg = f"could not uniquely update catalog part {row.part_key!r}"
            raise ValueError(msg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent, prefix=f".{path.name}.") as temp_dir:
        temporary = Path(temp_dir) / path.name
        temporary.write_text(serialized)
        temporary.replace(path)


def _print_report(audit: MassAudit, *, show_all: bool) -> None:
    for row in audit.rows:
        if row.resolved and not row.changed and not show_all:
            continue
        if row.resolved:
            print(
                f"{row.part_key:<28} {row.catalog_mass_g:>6.2f} g -> "
                f"{row.bricklink_mass_g:>6.2f} g "
                f"({row.delta_percent:+7.2f}%, {row.match})"
            )
        else:
            candidates = (
                f" candidates={','.join(row.candidates)}" if row.candidates else ""
            )
            print(f"{row.part_key:<28} unresolved ({row.match}){candidates}")
    print(
        f"catalog={len(audit.rows)} resolved={audit.resolved_count} "
        f"changed={audit.changed_count} unresolved={audit.unresolved_count}"
    )


def _write_json(path: Path | None, audit: MassAudit) -> None:
    if path is None:
        return
    serialized = json.dumps(audit.to_dict(), indent=2) + "\n"
    if str(path) == "-":
        print(serialized, end="")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parts", type=Path, nargs="?", default=_DEFAULT_PARTS)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument(
        "--write",
        action="store_true",
        help="atomically replace resolved mass_g values in --catalog",
    )
    parser.add_argument("--all", action="store_true", help="show unchanged rows")
    parser.add_argument(
        "--json",
        type=Path,
        metavar="PATH|-",
        help="also write the full audit as JSON; '-' writes it to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the mass ingestion audit and optional catalog update."""
    args = parse_args(argv)
    try:
        index = build_index(load_parts(args.parts))
        document = load_catalog_document(args.catalog)
        audit = audit_catalog(document, index)
        if str(args.json) != "-":
            _print_report(audit, show_all=args.all)
        _write_json(args.json, audit)
        if args.write:
            if audit.unresolved_count:
                print(
                    "error: refusing a partial catalog update while entries are "
                    "unresolved",
                    file=sys.stderr,
                )
                return 2
            updated = apply_masses(document, audit)
            write_catalog(args.catalog, audit)
            print(f"updated {updated} mass values in {args.catalog}")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 2 if audit.unresolved_count else 0


if __name__ == "__main__":
    sys.exit(main())
