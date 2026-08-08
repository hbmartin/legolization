"""Measure catalog and parser coverage over real, human-authored LDraw models.

Every ``.ldr``/``.mpd`` this project has parsed, it also wrote. That is a blind
spot in :mod:`legolization.ldraw_in`, in the catalog, and in the SNOT and
submodel paths. This script points the importer at the LDraw Official Model
Repository - official LEGO sets, rebuilt by people, fetched by
``scripts/fetch_omr.py`` - and counts what happens.

It uses ``ldraw_in.import_occurrences`` rather than ``import_ldraw`` because that
seam **returns** ``OccurrenceImport.problems`` instead of raising, so every
problem in every model is counted rather than the first one aborting the model.

Two deliverables:

1. **A catalog-coverage number** - the share of real part occurrences our catalog
   supports, plus the unsupported part codes ranked by how often they actually
   appear. That replaces guesswork about which part to add next in the
   `extend-lego-part-support` skill.
2. **An independent parser cross-check.** ``bricknet.parse_ldr`` (MIT, CVPR 2026)
   is a second, unrelated LDraw parser. Where the two disagree on part count, one
   of them is wrong. Enable with ``--cross-check``.

Usage::

    uv run python scripts/ldraw_coverage.py [--models DIR] [--limit N]
        [--cross-check] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self

from ldraw import Severity, iter_ldr_issues, load_model

from legolization.catalog import default_catalog
from legolization.ldraw_in import import_occurrences

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from legolization.ldraw_in import LdrawImportProblem

_REPO = Path(__file__).parent.parent
_DEFAULT_MODELS = _REPO / "datasets" / "omr" / "ldraw"
_DEFAULT_LIBRARY = _REPO / "datasets" / "ldraw-library" / "ldraw"
_DEFAULT_OUT = _REPO / "eval" / "datasets" / "ldraw-coverage"

# The measured failure taxonomy of ldraw_in. Order matters: the first pattern
# that matches a problem message classifies it, so keep the specific ones first.
_TAXONOMY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("part-not-in-catalog", re.compile(r"part not in the catalog")),
    ("colour-not-solid", re.compile(r"colour is not in the solid palette")),
    ("rotation-not-yaw", re.compile(r"rotation is not a yaw multiple")),
    ("off-grid", re.compile(r"position is off the stud/plate grid")),
    ("sideways-unsupported", re.compile(r"sideways part in an unsupported")),
    ("ambiguous-piece", re.compile(r"ambiguous piece")),
    ("no-candidate-fits", re.compile(r"no candidate part fits")),
    ("collision", re.compile(r"collide|overlap", re.IGNORECASE)),
)
_PART_CODE = re.compile(r"piece \d+ \(([^)]+)\)")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelReport:
    """Coverage outcome for one real model."""

    name: str
    status: str
    occurrences: int = 0
    placed: int = 0
    problems: int = 0
    taxonomy: dict[str, int] = field(default_factory=dict)
    unsupported_parts: dict[str, int] = field(default_factory=dict)
    bricknet_parts: int | None = None
    detail: str = ""

    @property
    def coverage(self) -> float:
        """Share of this model's occurrences the importer placed."""
        return self.placed / self.occurrences if self.occurrences else 0.0


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    """Which LDraw stems are real parts and which are geometric primitives.

    The distinction matters because a raw occurrence count is not a part count.
    Some OMR MPDs inline unofficial part *definitions* as submodels, and the
    analysis descends into them, so primitive references (``stud``, ``4-4ring3``,
    ``box4o8a``) enter the occurrence stream alongside real parts. Counting those
    as "unsupported parts" would badly understate coverage and would put
    primitives at the top of the what-to-support-next ranking, where they do not
    belong - we will never place a ``2-4edge``.

    Built from the official library (``datasets/ldraw-library/ldraw``): ``parts/``
    is authoritative for parts, ``p/`` for primitives.
    """

    parts: frozenset[str]
    primitives: frozenset[str]

    @classmethod
    def load(cls, root: Path) -> Self | None:
        """Index a library tree, or return ``None`` if it is not there."""
        parts_dir, prims_dir = root / "parts", root / "p"
        if not parts_dir.is_dir() or not prims_dir.is_dir():
            return None
        return cls(
            parts=frozenset(path.stem.casefold() for path in parts_dir.glob("*.dat")),
            primitives=frozenset(
                path.stem.casefold() for path in prims_dir.rglob("*.dat")
            ),
        )

    def kind(self, stem: str) -> str:
        """Classify one stem as ``part``, ``primitive``, or ``unknown``."""
        folded = stem.casefold().removesuffix(".dat")
        if folded in self.parts:
            return "part"
        if folded in self.primitives:
            return "primitive"
        return "unknown"


def classify(problem: LdrawImportProblem) -> tuple[str, str | None]:
    """Return ``(taxonomy key, part code)`` for one importer problem."""
    message = problem.summary
    key = next(
        (name for name, pattern in _TAXONOMY if pattern.search(message)),
        "other",
    )
    code = match.group(1) if (match := _PART_CODE.search(message)) else None
    return key, code


def _bricknet_part_count(path: Path) -> int | None:
    """Part count from BrickNet's independent parser, or ``None`` if it cannot."""
    try:
        import bricknet  # noqa: PLC0415 - dev-only dependency, optional at runtime

        graph = bricknet.parse_ldr(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - a third-party parser failing is data, not a crash
        return None
    for attribute in ("part_ids", "parts"):
        if (value := getattr(graph, attribute, None)) is not None:
            return len(value)
    return None


def inspect_model(path: Path, *, cross_check: bool) -> ModelReport:
    """Import one model, counting occurrences, placements, and problems."""
    catalog = default_catalog()
    try:
        loaded = load_model(path)
        analysis = loaded.analyze(None)
    except Exception as error:  # noqa: BLE001 - upstream parser raises broadly
        return ModelReport(
            name=path.name,
            status="load-failed",
            detail=f"{type(error).__name__}: {error}",
        )
    if loaded.model is None or analysis is None:
        errors = sum(
            1 for issue in iter_ldr_issues(loaded) if issue.severity is Severity.ERROR
        )
        return ModelReport(
            name=path.name,
            status="load-failed",
            detail=f"no analysable model ({errors} error diagnostic(s))",
        )

    occurrences = tuple(analysis.occurrences)
    try:
        imported = import_occurrences(
            occurrences,
            catalog=catalog,
            ground=False,
            default_model=path.name,
        )
    except Exception as error:  # noqa: BLE001 - decode is what we are measuring
        return ModelReport(
            name=path.name,
            status="import-raised",
            occurrences=len(occurrences),
            detail=f"{type(error).__name__}: {error}",
        )

    taxonomy: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    for problem in imported.problems:
        key, code = classify(problem)
        taxonomy[key] += 1
        if key == "part-not-in-catalog" and code:
            unsupported[code.casefold()] += 1
    placed = sum(1 for _ in imported.layout)
    return ModelReport(
        name=path.name,
        status="ok" if not imported.problems else "partial",
        occurrences=len(occurrences),
        placed=placed,
        problems=len(imported.problems),
        taxonomy=dict(taxonomy),
        unsupported_parts=dict(unsupported),
        bricknet_parts=_bricknet_part_count(path) if cross_check else None,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Summary:
    """The coverage headline rolled up over every inspected model."""

    models: int
    statuses: dict[str, int]
    occurrences: int
    placed: int
    fully_importable: int
    taxonomy: dict[str, int]
    unsupported_parts: dict[str, int]
    unsupported_part_kinds: int
    bricknet_disagreements: list[tuple[str, int, int]]
    unsupported_by_kind: dict[str, int] = field(default_factory=dict)
    unsupported_real_parts: dict[str, int] = field(default_factory=dict)

    @property
    def occurrence_coverage(self) -> float:
        """Share of real part occurrences the importer placed."""
        return self.placed / self.occurrences if self.occurrences else 0.0

    def to_dict(self) -> Mapping[str, object]:
        """JSON payload, with the derived coverage folded in."""
        return asdict(self) | {"occurrence_coverage": self.occurrence_coverage}


def aggregate(
    reports: Sequence[ModelReport],
    *,
    library: LibraryIndex | None = None,
) -> Summary:
    """Roll per-model reports into the coverage headline."""
    taxonomy: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    for report in reports:
        taxonomy.update(report.taxonomy)
        unsupported.update(report.unsupported_parts)
    statuses = Counter(report.status for report in reports)
    by_kind: Counter[str] = Counter()
    real_parts: Counter[str] = Counter()
    if library is not None:
        for stem, count in unsupported.items():
            kind = library.kind(stem)
            by_kind[kind] += count
            if kind == "part":
                real_parts[stem] = count
    return Summary(
        models=len(reports),
        statuses=dict(statuses),
        occurrences=sum(report.occurrences for report in reports),
        placed=sum(report.placed for report in reports),
        fully_importable=statuses.get("ok", 0),
        taxonomy=dict(taxonomy.most_common()),
        unsupported_parts=dict(unsupported.most_common(50)),
        unsupported_part_kinds=len(unsupported),
        unsupported_by_kind=dict(by_kind),
        unsupported_real_parts=dict(real_parts.most_common(40)),
        bricknet_disagreements=[
            (report.name, report.occurrences, report.bricknet_parts)
            for report in reports
            if report.bricknet_parts is not None
            and report.bricknet_parts != report.occurrences
        ],
    )


def to_markdown(reports: Sequence[ModelReport], summary: Summary) -> str:
    """Render the coverage report."""
    lines = [
        "# LDraw catalog + parser coverage over the OMR",
        "",
        (
            f"models={summary.models} "
            f"fully-importable={summary.fully_importable} "
            f"occurrences={summary.occurrences:,} "
            f"placed={summary.placed:,} "
            f"coverage={summary.occurrence_coverage:.1%}"
        ),
        "",
        "`coverage` is the share of real part occurrences the importer placed.",
        "A model counts as `ok` only when it imported with zero problems; the",
        "OMR uses hundreds of part types outside our catalog, so `partial` is",
        "the expected outcome and the interesting number is which parts are",
        "missing and how often they appear.",
        "",
        "## Model status",
        "",
    ]
    lines += [
        f"- {status}: {count}" for status, count in sorted(summary.statuses.items())
    ]
    lines += ["", "## Problem taxonomy", ""]
    if summary.taxonomy:
        lines += ["| class | count |", "| --- | ---: |"]
        lines += [f"| {key} | {count:,} |" for key, count in summary.taxonomy.items()]
    else:
        lines.append("No problems recorded.")
    lines += [
        "",
        "## Unsupported references, by kind",
        "",
        f"{summary.unsupported_part_kinds} distinct LDraw codes were rejected as",
        '"part not in the catalog". **They are not all parts.** Some OMR MPDs',
        "inline unofficial part *definitions* as submodels, so the analysis",
        "descends into them and geometric primitives (`stud`, `4-4ring3`,",
        "`box4o8a`) enter the occurrence stream. Classified against the official",
        "library (`datasets/ldraw-library`), where `parts/` and `p/` are",
        "authoritative:",
        "",
    ]
    if summary.unsupported_by_kind:
        lines += ["| kind | occurrences |", "| --- | ---: |"]
        lines += [
            f"| {kind} | {count:,} |"
            for kind, count in sorted(
                summary.unsupported_by_kind.items(), key=lambda item: -item[1]
            )
        ]
        lines += [
            "",
            "Only the `part` row is addressable by extending the catalog;",
            "`primitive` occurrences are an artefact of inlined part definitions",
            "and would never be placed as bricks.",
        ]
    else:
        lines.append(
            "_No library index available - run `fetch_datasets.py --only "
            "ldraw-library` to classify these._"
        )
    lines += [
        "",
        "## Unsupported REAL parts, ranked by real-world occurrence",
        "",
        'This ranking is the answer to "which part should',
        '`extend-lego-part-support` add next".',
        "",
        "| ldraw part | occurrences |",
        "| --- | ---: |",
    ]
    lines += [
        f"| {code} | {count:,} |"
        for code, count in (
            summary.unsupported_real_parts or summary.unsupported_parts
        ).items()
    ]
    if summary.bricknet_disagreements:
        lines += [
            "",
            "## Parser cross-check against BrickNet",
            "",
            "Two independent parsers disagreeing on part count means one is wrong.",
            "",
            "| model | ours | bricknet |",
            "| --- | ---: | ---: |",
        ]
        lines += [
            f"| {name} | {ours:,} | {theirs:,} |"
            for name, ours, theirs in summary.bricknet_disagreements
        ]
    if failures := [report for report in reports if report.status == "load-failed"]:
        lines += ["", "## Models the parser could not load", ""]
        lines += [f"- {report.name}: {report.detail}" for report in failures[:40]]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--models", type=Path, default=_DEFAULT_MODELS)
    parser.add_argument(
        "--library",
        type=Path,
        default=_DEFAULT_LIBRARY,
        help="official LDraw tree used to tell real parts from primitives",
    )
    parser.add_argument("--limit", type=int, default=0, help="inspect at most N models")
    parser.add_argument(
        "--cross-check",
        action="store_true",
        help="also parse each model with bricknet and compare part counts",
    )
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the coverage sweep and write the report pair."""
    args = parse_args(argv)
    if not args.models.is_dir():
        print(f"{args.models} is not a directory", file=sys.stderr)
        return 1
    paths = sorted(args.models.glob("*.mpd")) + sorted(args.models.glob("*.ldr"))
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print(f"no .mpd/.ldr models under {args.models}", file=sys.stderr)
        return 1

    reports: list[ModelReport] = []
    for position, path in enumerate(paths, start=1):
        reports.append(inspect_model(path, cross_check=args.cross_check))
        if sys.stderr.isatty():
            print(
                f"\r{position}/{len(paths)} {path.name[:40]:<40}",
                end="",
                file=sys.stderr,
            )
    if sys.stderr.isatty():
        print(file=sys.stderr)

    if (library := LibraryIndex.load(args.library)) is None:
        print(
            f"no LDraw library at {args.library}; unsupported codes will not be "
            f"split into parts vs primitives",
            file=sys.stderr,
        )
    summary = aggregate(reports, library=library)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(
            {
                "generated": stamp,
                "models_dir": str(args.models),
                "summary": summary.to_dict(),
                "models": [asdict(report) for report in reports],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown = to_markdown(reports, summary)
    (out / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
