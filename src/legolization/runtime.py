"""Shared monotonic deadline and structured progress contracts."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal, Self

type ProgressLevel = Literal["debug", "info", "warning", "error"]


class Deadline(float):
    """One absolute monotonic deadline propagated through expensive phases.

    ``Deadline`` subclasses ``float`` so older numerical solver seams can consume
    the same object without converting it into scattered local timestamps.
    """

    @classmethod
    def after(cls, seconds: float | None) -> Self | None:
        """Return an absolute deadline, or ``None`` for an unlimited run."""
        return None if seconds is None else cls(time.monotonic() + seconds)

    @property
    def expired(self) -> bool:
        """Return whether the deadline has elapsed."""
        return time.monotonic() >= self

    @property
    def remaining_s(self) -> float:
        """Return the non-negative remaining duration."""
        return max(float(self) - time.monotonic(), 0.0)

    def tightened(self, seconds: float) -> Self:
        """Return the earlier of this deadline and a local duration cap."""
        return type(self)(min(float(self), time.monotonic() + seconds))


class ProgressEvent(str):
    """Structured progress event with string-compatible presentation."""

    __slots__ = ("completed", "level", "phase", "total")

    phase: str
    level: ProgressLevel
    completed: int | None
    total: int | None

    def __new__(
        cls,
        message: str,
        *,
        phase: str,
        level: ProgressLevel = "info",
        completed: int | None = None,
        total: int | None = None,
    ) -> Self:
        """Create an event carrying a readable message and typed metadata."""
        instance = super().__new__(cls, message)
        instance.phase = phase
        instance.level = level
        instance.completed = completed
        instance.total = total
        return instance


type ProgressCallback = Callable[[ProgressEvent], None]

__all__ = [
    "Deadline",
    "ProgressCallback",
    "ProgressEvent",
    "ProgressLevel",
    "deadline_share",
    "logical_cpu_count",
]


def deadline_share(*, deadline: float | None, fraction: float) -> float | None:
    """Return ``fraction`` of the remaining time without extending ``deadline``."""
    if deadline is None:
        return None
    now = time.monotonic()
    return min(deadline, now + fraction * max(deadline - now, 0.0))


def logical_cpu_count() -> int:
    """Logical CPUs available to this process (the worker-cap authority)."""
    import os  # noqa: PLC0415

    counter = getattr(os, "process_cpu_count", None) or os.cpu_count
    return counter() or 1
