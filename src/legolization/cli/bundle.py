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

    config = resolve_config(args)
    envelope = run_bundle(
        BundleRequest(
            input_path=args.input,
            config=config,
            output_dir=args.output,
            fresh=args.fresh,
            cancel_pending=args.cancel_pending,
            retile=args.retile,
        )
    )
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
