"""Opt-in span telemetry for profiling pipeline runs.

Recording is ambient: wrap a run in :func:`record` and every instrumented
call site (``with span("stability.lp", n=bricks):``) accumulates call
counts and wall seconds into the active :class:`Telemetry`. Outside a
``record()`` block, :func:`span` returns a shared no-op context manager,
so instrumentation costs one ``ContextVar.get`` per call when disabled
and never changes behaviour.

Span names are flat and deliberately overlap (``stability.analyze``
contains ``stability.build_model`` and ``stability.lp``); attribution is
by family leaf, not by tree. Optional ``n`` (brick count) buckets each
call by power of two so seconds-vs-size scaling can be read from one run.

Caveat: :mod:`contextvars` state does not cross spawn process boundaries,
so ``compare.run_all`` workers record nothing — profile in-process
``run()`` calls (the profiling script does exactly that).
"""

from __future__ import annotations

import time
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Iterator

_ACTIVE: ContextVar[Telemetry | None] = ContextVar("legolization_telemetry")
_NOOP: AbstractContextManager[None] = nullcontext()


@dataclass(slots=True)
class SpanStats:
    """Accumulated calls and wall seconds for one span name."""

    calls: int = 0
    seconds: float = 0.0
    buckets: dict[int, list[float]] = field(default_factory=dict)

    def add(self, seconds: float, n: int | None) -> None:
        """Fold one finished call into the totals."""
        self.calls += 1
        self.seconds += seconds
        if n is not None:
            bucket = 1 << max(n - 1, 0).bit_length()
            entry = self.buckets.setdefault(bucket, [0, 0.0])
            entry[0] += 1
            entry[1] += seconds


@dataclass(slots=True)
class Telemetry:
    """One recording session's span accumulators."""

    spans: dict[str, SpanStats] = field(default_factory=dict)

    def add(self, name: str, seconds: float, n: int | None = None) -> None:
        """Record one finished call of ``name``."""
        self.spans.setdefault(name, SpanStats()).add(seconds, n)

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view: ``{name: {calls, seconds, buckets}}``."""
        return {
            name: {
                "calls": stats.calls,
                "seconds": round(stats.seconds, 6),
                "buckets": {
                    str(bucket): [int(entry[0]), round(entry[1], 6)]
                    for bucket, entry in sorted(stats.buckets.items())
                },
            }
            for name, stats in sorted(self.spans.items())
        }


class _Span:
    """Timing context manager bound to an active :class:`Telemetry`."""

    __slots__ = ("_n", "_name", "_started", "_telemetry")

    def __init__(self, telemetry: Telemetry, name: str, n: int | None) -> None:
        self._telemetry = telemetry
        self._name = name
        self._n = n
        self._started = 0.0

    def __enter__(self) -> Self:
        self._started = time.perf_counter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Record even when the body raised: failed solves still cost time.
        self._telemetry.add(
            self._name,
            time.perf_counter() - self._started,
            self._n,
        )


def current() -> Telemetry | None:
    """Return the active recording session, or None when disabled."""
    return _ACTIVE.get(None)


@contextmanager
def record() -> Iterator[Telemetry]:
    """Enable span recording for the duration of the block."""
    telemetry = Telemetry()
    token = _ACTIVE.set(telemetry)
    try:
        yield telemetry
    finally:
        _ACTIVE.reset(token)


def span(name: str, n: int | None = None) -> AbstractContextManager[object]:
    """Return a timing context for ``name``; a shared no-op when not recording."""
    telemetry = _ACTIVE.get(None)
    if telemetry is None:
        return _NOOP
    return _Span(telemetry, name, n)
