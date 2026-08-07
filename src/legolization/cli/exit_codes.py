"""Fixed process exit codes shared by every CLI command.

The mapping is part of the public 0.6 contract:

===== ==========================================================
Code  Meaning
===== ==========================================================
0     Complete.
1     Operational error, including invalid usage.
2     Unbuildable or failed physics.
3     Partial or indeterminate outcome.
4     Exact-placement limit error.
130   Interrupted after recording resumable partial state.
===== ==========================================================
"""

from __future__ import annotations

from legolization.errors import (
    ExactPlacementLimitError,
    LegolizationError,
    PlacementInfeasibleError,
)

COMPLETE = 0
OPERATIONAL_ERROR = 1
UNBUILDABLE = 2
PARTIAL = 3
EXACT_LIMIT = 4
INTERRUPTED = 130

_STATUS_BY_CODE = {
    COMPLETE: "complete",
    OPERATIONAL_ERROR: "error",
    UNBUILDABLE: "unbuildable",
    PARTIAL: "partial",
    EXACT_LIMIT: "error",
    INTERRUPTED: "interrupted",
}


class CliUsageError(LegolizationError):
    """Raised for invalid command-line usage; maps to exit code 1."""


def exit_code_for_exception(error: BaseException) -> int:
    """Map an exception to the fixed exit-code contract."""
    match error:
        case ExactPlacementLimitError():
            return EXACT_LIMIT
        case PlacementInfeasibleError():
            return UNBUILDABLE
        case KeyboardInterrupt():
            return INTERRUPTED
        case _:
            return OPERATIONAL_ERROR


def status_for_exit_code(code: int) -> str:
    """Return the machine-readable status word for an exit code."""
    return _STATUS_BY_CODE.get(code, "error")
