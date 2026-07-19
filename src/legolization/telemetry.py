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
from pathlib import Path
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Iterator

_ACTIVE: ContextVar[Telemetry | None] = ContextVar("legolization_telemetry")
_NOOP: AbstractContextManager[None] = nullcontext()


@dataclass(slots=True)
class _Bucket:
    """Calls and wall seconds of one power-of-two size bucket."""

    calls: int = 0
    seconds: float = 0.0


@dataclass(slots=True)
class SpanStats:
    """Accumulated calls and wall seconds for one span name."""

    calls: int = 0
    seconds: float = 0.0
    buckets: dict[int, _Bucket] = field(default_factory=dict)

    def add(self, seconds: float, n: int | None) -> None:
        """Fold one finished call into the totals."""
        self.calls += 1
        self.seconds += seconds
        if n is not None:
            bucket = self.buckets.setdefault(1 << max(n - 1, 0).bit_length(), _Bucket())
            bucket.calls += 1
            bucket.seconds += seconds


@dataclass(slots=True)
class Telemetry:
    """One recording session's span accumulators and exact-value gauges."""

    spans: dict[str, SpanStats] = field(default_factory=dict)
    values: dict[str, list[float]] = field(default_factory=dict)
    events: list[tuple[str, float]] = field(default_factory=list)
    """Every gauge reading in emission order — the global sequence the
    per-name ``values`` lists cannot reconstruct (PR #18 review: phase
    rows printed placed-before-repaired because names were ordered
    independently)."""

    def add(self, name: str, seconds: float, n: int | None = None) -> None:
        """Record one finished call of ``name``."""
        self.spans.setdefault(name, SpanStats()).add(seconds, n)

    def record_value(self, name: str, value: float) -> None:
        """Append one exact gauge reading.

        Spans bucket ``n`` by powers of two; phase-boundary quantities
        like brick counts need this lossless channel. The reading joins
        both the per-name ``values`` list and the global ``events``
        sequence.
        """
        self.values.setdefault(name, []).append(value)
        self.events.append((name, value))

    def to_dict(self) -> dict[str, object]:
        """JSON-safe span view: ``{name: {calls, seconds, buckets}}``.

        Bucket entries stay ``[calls, seconds]`` pairs — the profile JSON
        schema is unchanged by the typed internal representation. Gauge
        readings live in :meth:`values_dict`, a separate channel, so
        span consumers never meet a shape they do not expect.
        """
        return {
            name: {
                "calls": stats.calls,
                "seconds": round(stats.seconds, 6),
                "buckets": {
                    str(size): [bucket.calls, round(bucket.seconds, 6)]
                    for size, bucket in sorted(stats.buckets.items())
                },
            }
            for name, stats in sorted(self.spans.items())
        }

    def values_dict(self) -> dict[str, list[float]]:
        """JSON-safe gauge view: ``{name: [reading, ...]}`` in order."""
        return {name: list(entries) for name, entries in sorted(self.values.items())}

    def events_list(self) -> list[tuple[str, float]]:
        """Gauge readings in global emission order (JSON-safe pairs)."""
        return list(self.events)


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


_SHA_LENGTH = 40
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _git_dirs(root: Path) -> tuple[Path, Path] | None:
    """Resolve (git_dir, common_dir) for a checkout, following worktrees.

    In a linked ``git worktree``, ``.git`` is a FILE containing a
    ``gitdir:`` indirection to a per-worktree directory whose
    ``commondir`` file points back at the shared object store where
    refs and packed-refs live (PR #18 review: the directory assumption
    returned None in every worktree, stripping artifacts of their
    code-state identity).
    """
    dot_git = root / ".git"
    if dot_git.is_dir():
        git_dir = dot_git
    else:
        try:
            content = dot_git.read_text().strip()
        except OSError:
            return None
        if not content.startswith("gitdir:"):
            return None
        target = Path(content.removeprefix("gitdir:").strip())
        git_dir = target if target.is_absolute() else (root / target).resolve()
    common_dir = git_dir
    try:
        common = (git_dir / "commondir").read_text().strip()
    except OSError:
        pass
    else:
        target = Path(common)
        common_dir = target if target.is_absolute() else (git_dir / target).resolve()
    return git_dir, common_dir


def git_sha(repo: Path | None = None) -> str | None:
    """Read the current commit sha from ``.git`` without spawning a process.

    Profile artifacts stamp this so before/after comparisons are pinned
    to code states; both profile writers (the script and the CLI
    ``--profile``) share it. Linked worktrees resolve through their
    ``gitdir:``/``commondir`` indirections.
    """
    root = repo if repo is not None else _REPO_ROOT
    if (dirs := _git_dirs(root)) is None:
        return None
    git_dir, common_dir = dirs
    try:
        content = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if not content.startswith("ref:"):
        return content if len(content) == _SHA_LENGTH else None
    return _resolve_ref(content.removeprefix("ref:").strip(), git_dir, common_dir)


def _resolve_ref(ref: str, git_dir: Path, common_dir: Path) -> str | None:
    for base in (common_dir, git_dir):
        try:
            return (base / ref).read_text().strip()
        except OSError:
            continue
    try:
        packed = (common_dir / "packed-refs").read_text()
    except OSError:
        return None
    for line in packed.splitlines():
        if not line.startswith("#") and line.endswith(ref):
            return line.split()[0]
    return None


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


def value(name: str, reading: float) -> None:
    """Record one exact gauge reading; a no-op when not recording."""
    telemetry = _ACTIVE.get(None)
    if telemetry is not None:
        telemetry.record_value(name, reading)
