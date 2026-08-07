"""The ``analyze`` command: argument registration and envelope wrapper.

The argument surface lives here (light imports keep ``--help`` fast);
the analysis engine in :mod:`legolization.analyze_cli` loads lazily
when the command actually runs and reuses :func:`configure_args` for
its standalone parser.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from legolization.cli.common import add_json_option
from legolization.cli.envelope import ArtifactRecord, ErrorRecord, ResultEnvelope

SCENARIOS = (
    "auto",
    "rest",
    "lift-body",
    "lift-chassis",
    "front-torsion",
    "rear-torsion",
    "side-load",
)


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``analyze`` arguments and handler."""
    configure_args(parser)
    add_json_option(parser)
    parser.set_defaults(handler=_run, command_name="analyze", analyze_parser=parser)


def configure_args(parser: argparse.ArgumentParser) -> None:
    """Register the analysis argument surface on any parser."""
    # Declarative argparse registration is intentionally kept contiguous.
    # lizard forgives(length)
    parser.add_argument("input", type=Path, help="input .ldr or .mpd model")
    parser.add_argument("--config", type=Path, default=None)
    manifest = parser.add_mutually_exclusive_group()
    manifest.add_argument("--manifest", type=Path, default=None)
    manifest.add_argument("--no-manifest", action="store_true")
    parser.add_argument(
        "--report",
        default=None,
        metavar="PATH",
        help="optional legacy schema-2 JSON view ('-' = stdout)",
    )
    parser.add_argument(
        "--assembly-report",
        default=None,
        metavar="PATH",
        help="optional legacy assembly schema-1 JSON view ('-' = stdout)",
    )
    parser.add_argument("--graph", type=Path, default=None, metavar="PATH")
    parser.add_argument("--diagnostic-mpd", type=Path, default=None, metavar="PATH")
    parser.add_argument("--floating-mpd", type=Path, default=None, metavar="PATH")
    parser.add_argument("--html-report", type=Path, default=None, metavar="PATH")
    parser.add_argument("--callout-dir", type=Path, default=None, metavar="DIR")
    parser.add_argument("--comparison-dir", type=Path, default=None, metavar="DIR")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="collect assembly artifacts and enable HTML/rendered diagnostics",
    )
    parser.add_argument(
        "--no-data-artifacts",
        action="store_true",
        help="suppress default graph, component MPD, and floating MPD outputs",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="best repair path (default: INPUT.repaired.ldr/.mpd when found)",
    )
    parser.add_argument(
        "--time-budget",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="hard repair-search budget (default: 300)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="legacy voxel catalog extension; may be repeated",
    )
    parser.add_argument(
        "--catalog-estimates",
        type=Path,
        action="append",
        default=[],
        dest="catalog_estimates",
        metavar="PATH",
        help="catalog estimate sidecar with labeled provenance; may be repeated",
    )
    parser.add_argument(
        "--connector-catalog",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="schema-1 connector catalog (mass, tags, custom kinds); may be repeated",
    )
    parser.add_argument(
        "--ldcad-metadata",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="LDCad shadow directory or .csl/.zip archive; may be repeated",
    )
    parser.add_argument(
        "--studio-metadata",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="Studio connectivity JSON export (connections only); may be repeated",
    )
    parser.add_argument(
        "--preserve-origin",
        action="store_true",
        help="keep LDraw layer zero authoritative in legacy and adaptive support",
    )
    parser.add_argument(
        "--repair",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable repaired-model search",
    )
    parser.add_argument(
        "--effort",
        choices=("fast", "balanced", "exhaustive"),
        default=None,
        help=(
            "repair effort tier: fast=60s, balanced=300s (default), "
            "exhaustive requires an explicit --time-budget"
        ),
    )
    parser.add_argument(
        "--repair-output",
        type=Path,
        default=None,
        dest="repair_output",
        metavar="DIR",
        help="repair bundle directory (default: INPUT '-repair' sibling)",
    )
    parser.add_argument(
        "--no-step-check",
        action="store_true",
        help="skip informational checks of source STEP prefixes",
    )
    parser.add_argument("--seed", type=int, default=None, help="repair search seed")
    parser.add_argument(
        "--topology-only",
        action="store_true",
        help="skip mass, support, and equilibrium analysis",
    )
    parser.add_argument(
        "--support",
        default=None,
        metavar="MODE",
        help=("auto, free, wheels, auto-ground, anchored-baseplate, or selected:IDS"),
    )
    parser.add_argument(
        "--path-between",
        nargs=2,
        action="append",
        default=[],
        metavar=("LEFT", "RIGHT"),
        help="region path selectors, e.g. pages:1-4 occurrences:20-30",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIOS,
        default=None,
        help="assembly load scenario; may be repeated",
    )
    parser.add_argument("--gravity-g", type=_nonnegative_float, default=None)
    parser.add_argument("--side-load-g", type=_nonnegative_float, default=None)
    parser.add_argument("--torsion-load-g", type=_nonnegative_float, default=None)


def _run(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.analyze_cli import run_analysis  # noqa: PLC0415

    outcome = run_analysis(args, args.analyze_parser, json_mode=args.json)
    error = (
        ErrorRecord(type="AnalysisError", message=outcome.error_message)
        if outcome.error_message is not None
        else None
    )
    data: dict[str, object] = {}
    if outcome.verdict is not None:
        data["verdict"] = outcome.verdict
    if outcome.status is not None:
        data["status"] = outcome.status
    if outcome.catalog is not None:
        data["catalog"] = outcome.catalog
    return ResultEnvelope(
        command="analyze",
        exit_code=outcome.exit_code,
        artifacts=tuple(
            ArtifactRecord(path=path, kind=kind)
            for kind, path in sorted(outcome.artifacts.items())
        ),
        warnings=outcome.warnings,
        error=error,
        data=data or None,
    )


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        msg = f"{value!r} is not a number"
        raise argparse.ArgumentTypeError(msg) from error
    if not math.isfinite(parsed) or parsed <= 0:
        msg = f"{value!r} must be finite and greater than zero"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        msg = f"{value!r} is not a number"
        raise argparse.ArgumentTypeError(msg) from error
    if not math.isfinite(parsed) or parsed < 0:
        msg = f"{value!r} must be finite and non-negative"
        raise argparse.ArgumentTypeError(msg)
    return parsed
