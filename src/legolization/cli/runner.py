"""Command execution wrapper enforcing the one-envelope contract.

Handlers never write to stdout under ``--json``; the runner's ``emit``
is the sole stdout writer in that mode. In human mode handlers print
freely and the runner emits nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.cli.envelope import ResultEnvelope, emit, envelope_for_error
from legolization.errors import LegolizationError

if TYPE_CHECKING:
    from collections.abc import Callable


def run_command(
    command: str,
    handler: Callable[[], ResultEnvelope],
    *,
    json_output: bool,
) -> int:
    """Run a command handler and report its envelope and exit code."""
    try:
        envelope = handler()
    except KeyboardInterrupt as error:
        envelope = envelope_for_error(command, error)
    except (LegolizationError, OSError, ValueError) as error:
        envelope = envelope_for_error(command, error)
    if json_output:
        emit(envelope)
    return envelope.exit_code
