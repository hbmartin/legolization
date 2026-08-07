"""Root argument parser and command registry for the 0.6 hierarchy."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, NoReturn

from legolization.cli import (
    analyze,
    build,
    bundle,
    cache,
    catalog_cmd,
    corpus_cmd,
    input_cmd,
    instructions_cmd,
    model_cmd,
    parts_cmd,
    validate,
)
from legolization.cli.exit_codes import CliUsageError
from legolization.version import __version__

if TYPE_CHECKING:
    from collections.abc import Callable


class CliParser(argparse.ArgumentParser):
    """Argument parser whose usage failures raise instead of exiting.

    argparse's default ``error`` exits the process with status 2, which
    the 0.6 contract reserves for unbuildable results; usage problems
    are operational errors (exit 1) reported through the envelope.
    """

    def error(self, message: str) -> NoReturn:
        """Raise the usage failure for the runner to map to exit 1."""
        msg = f"{self.prog}: {message}"
        raise CliUsageError(msg)


COMMANDS: tuple[tuple[str, str, Callable[[argparse.ArgumentParser], None]], ...] = (
    ("build", "build an LDraw model from 3D input", build.configure),
    ("validate", "validate an assembly manifest", validate.configure),
    ("cache", "inspect or clear the template cache", cache.configure),
    (
        "analyze",
        "analyze LDraw geometry and search for a valid repair",
        analyze.configure,
    ),
    (
        "bundle",
        "run the complete generation pipeline into a portable bundle",
        bundle.configure,
    ),
    ("input", "inspect and normalize source models", input_cmd.configure),
    ("model", "operate on generated models", model_cmd.configure),
    (
        "instructions",
        "audit step-by-step build instructions",
        instructions_cmd.configure,
    ),
    (
        "catalog",
        "infer and validate parts-catalog extensions",
        catalog_cmd.configure,
    ),
    ("corpus", "manage and evaluate the placement corpus", corpus_cmd.configure),
    ("parts", "manage the official LDraw parts library", parts_cmd.configure),
)

COMMAND_NAMES: frozenset[str] = frozenset(name for name, _, _ in COMMANDS)


def build_parser() -> CliParser:
    """Build the root parser with every command registered."""
    parser = CliParser(
        prog="legolization",
        description=(
            "Convert 3D models into physically stable LEGO-style models "
            "in LDraw format, with analysis, rendering, instruction, "
            "catalog, and evaluation commands."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"legolization {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for name, help_text, configure in COMMANDS:
        configure(subparsers.add_parser(name, help=help_text))
    return parser
