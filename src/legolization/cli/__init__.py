"""Explicit command-line interface for the 0.6 command hierarchy.

Hosts the shared result envelope, exit-code contract, command runner,
and the :func:`main` entry point that dispatches every command.
"""

from __future__ import annotations

import sys
from pathlib import Path

from legolization.cli.exit_codes import OPERATIONAL_ERROR, CliUsageError
from legolization.version import __version__

_INPUT_SUFFIXES = {".vox", ".npy", ".obj", ".stl", ".ply", ".ldr", ".mpd"}

_MIGRATION_GUIDANCE = """\
error: the bare 'legolization INPUT' invocation was removed in 0.6.
Use one of:
  legolization bundle {input}              # full pipeline: model, BOM, instructions
  legolization build {input} -o OUT.ldr    # single-strategy model build
  legolization analyze {input}             # analyze an existing LDraw model
Run 'legolization --help' for the complete command list.\
"""


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns a process exit code."""
    from legolization.cli.parser import build_parser  # noqa: PLC0415

    effective = list(sys.argv[1:] if argv is None else argv)
    if effective[:1] == ["--version"]:
        print(f"legolization {__version__}")
        return 0
    if (guidance := _bare_invocation_code(effective)) is not None:
        return guidance
    parser = build_parser()
    try:
        args = parser.parse_args(effective)
    except CliUsageError as error:
        return _usage_failure(effective, error)
    from legolization.cli.runner import run_command  # noqa: PLC0415

    return run_command(
        args.command_name,
        lambda: args.handler(args),
        json_output=bool(getattr(args, "json", False)),
    )


def _bare_invocation_code(argv: list[str]) -> int | None:
    """Detect the removed bare-input invocation and print migration help."""
    from legolization.cli.parser import COMMAND_NAMES  # noqa: PLC0415

    if not argv:
        return None
    first = argv[0]
    if first.startswith("-") or first in COMMAND_NAMES:
        return None
    candidate = Path(first)
    if candidate.suffix.lower() not in _INPUT_SUFFIXES and not candidate.exists():
        return None
    print(_MIGRATION_GUIDANCE.format(input=first), file=sys.stderr)
    return OPERATIONAL_ERROR


def _usage_failure(argv: list[str], error: CliUsageError) -> int:
    """Report a parse-time usage failure, honouring a requested --json."""
    from legolization.cli.envelope import emit, envelope_for_error  # noqa: PLC0415

    print(f"error: {error}", file=sys.stderr)
    if "--json" in argv:
        emit(envelope_for_error(_guess_command(argv), error))
    return OPERATIONAL_ERROR


def _guess_command(argv: list[str]) -> str:
    """Best-effort command name for a parse-failure envelope."""
    from legolization.cli.parser import COMMAND_NAMES  # noqa: PLC0415

    for token in argv:
        if not token.startswith("-"):
            return token if token in COMMAND_NAMES else "cli"
    return "cli"


__all__ = ["main"]
