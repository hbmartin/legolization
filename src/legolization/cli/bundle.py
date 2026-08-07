"""The ``bundle`` command: complete noninteractive generation pipeline.

Phase 2 delivers the resumable portable-bundle foundation; candidate
sweeps, quality tiers, and the retry ladder land with phase 4.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from legolization.cli.common import (
    add_catalog_options,
    add_config_options,
    add_json_option,
    resolve_config,
)

if TYPE_CHECKING:
    import argparse

    from legolization.cli.envelope import ResultEnvelope


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``bundle`` arguments and handler."""
    parser.add_argument(
        "input",
        type=Path,
        help="input .vox/.npy/.obj/.stl/.ply model or .ldr/.mpd assembly",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="DIR",
        help="bundle directory (default: operation-specific sibling)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="force a fresh numeric-sibling run instead of resuming",
    )
    parser.add_argument(
        "--retile",
        action="store_true",
        help=(
            "turn an imported .ldr/.mpd assembly into a coloured occupancy "
            "target and regenerate it (default preserves the assembly)"
        ),
    )
    parser.add_argument(
        "--quality",
        choices=("fast", "balanced", "exhaustive", "direct"),
        default="balanced",
        help=(
            "candidate policy: fast (greedy, 2 min), balanced (full sweep "
            "plus eligible exact, 15 min; default), exhaustive (seeds 0-2, "
            "requires --duration), direct (single configured strategy)"
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "candidate time budget; required for exhaustive quality and "
            "for --retry-materials, overrides the tier default otherwise"
        ),
    )
    parser.add_argument(
        "--retry-materials",
        action="store_true",
        help=(
            "run the approved retry ladder (four-plate shell, six-plate "
            "shell, solid) sharing --duration fairly between rungs"
        ),
    )
    parser.add_argument(
        "--cancel-pending",
        action="store_true",
        help=(
            "terminate this bundle's detached candidate workers, keeping "
            "completed artifacts for a later resume"
        ),
    )
    add_config_options(parser)
    add_catalog_options(parser)
    add_json_option(parser)
    parser.set_defaults(handler=_run, command_name="bundle")


def _run(args: argparse.Namespace) -> ResultEnvelope:
    from legolization.bundle.orchestrator import (  # noqa: PLC0415
        BundleRequest,
        run_bundle,
    )
    from legolization.errors import ConfigurationError  # noqa: PLC0415

    if args.retry_materials and args.duration is None:
        msg = "--retry-materials requires a total --duration budget"
        raise ConfigurationError(msg)
    config = resolve_config(args)
    request = BundleRequest(
        input_path=args.input,
        config=config,
        output_dir=args.output,
        quality=args.quality,
        duration_s=args.duration,
        fresh=args.fresh,
        cancel_pending=args.cancel_pending,
        retile=args.retile,
    )
    if args.retry_materials:
        from legolization.bundle.retry import run_retry  # noqa: PLC0415

        envelope = run_retry(request, total_budget_s=args.duration)
    else:
        envelope = run_bundle(request)
    if not args.json:
        _print_summary(envelope)
    return envelope


def _print_summary(envelope: ResultEnvelope) -> None:
    data = envelope.data or {}
    print(f"bundle: {data.get('bundle_dir')} ({data.get('status')})")
    stages = data.get("stages")
    if isinstance(stages, dict):
        line = "   ".join(f"{name}: {status}" for name, status in stages.items())
        print(f"  {line}")
    for warning in envelope.warnings:
        print(f"  warning: {warning}")


__all__ = ["configure"]
