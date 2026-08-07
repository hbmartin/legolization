"""Operation-specific sibling naming and numeric collision suffixes.

Bundles default to a sibling directory named after the input with an
operation-specific suffix. Existing non-matching outputs are never
overwritten: a numeric suffix selects the first free sibling instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

type BundleFlavor = Literal[
    "legolization",
    "prepared",
    "optimized",
    "instructions",
    "analysis",
    "repair",
    "support",
]

FLAVOR_SUFFIXES: dict[BundleFlavor, str] = {
    "legolization": "-legolization",
    "prepared": "-prepared",
    "optimized": "-optimized",
    "instructions": "-instructions",
    "analysis": "-analysis",
    "repair": "-repair",
    "support": "-legolization-support",
}

_MAX_SIBLINGS = 10_000


@dataclass(frozen=True, slots=True, kw_only=True)
class ResumeDecision:
    """Where a bundle runs and whether it resumes existing work."""

    directory: Path
    resume: bool
    reason: str


def default_bundle_dir(input_path: Path, flavor: BundleFlavor) -> Path:
    """Return the unnumbered operation-specific sibling directory."""
    return input_path.parent / f"{input_path.stem}{FLAVOR_SUFFIXES[flavor]}"


def numbered_sibling(path: Path) -> Path:
    """Return the first nonexistent numeric sibling (``name-2``, ...)."""
    if not path.exists():
        return path
    for index in range(2, _MAX_SIBLINGS):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
    msg = f"no free numeric sibling for {path}"
    raise OSError(msg)


def numbered_file(path: Path) -> Path:
    """Return the first nonexistent numeric file variant (``stem.2.ext``)."""
    if not path.exists():
        return path
    for index in range(2, _MAX_SIBLINGS):
        candidate = path.with_name(f"{path.stem}.{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    msg = f"no free numeric variant for {path}"
    raise OSError(msg)


def sibling_candidates(base: Path) -> tuple[Path, ...]:
    """Return the existing numbered family of a bundle directory."""
    candidates = [base] if base.is_dir() else []
    for index in range(2, _MAX_SIBLINGS):
        candidate = base.with_name(f"{base.name}-{index}")
        if not candidate.exists():
            break
        if candidate.is_dir():
            candidates.append(candidate)
    return tuple(candidates)


def resolve_output_dir(  # noqa: PLR0913 - the resume policy's full input set
    input_path: Path,
    explicit: Path | None,
    *,
    flavor: BundleFlavor,
    identity_key: str,
    fresh: bool,
    read_key: Callable[[Path], str | None],
) -> ResumeDecision:
    """Pick the bundle directory, resuming identity-matched work.

    ``read_key`` returns the stored identity key of an existing bundle
    directory, or ``None`` when the directory holds no readable bundle.
    """
    base = explicit if explicit is not None else default_bundle_dir(input_path, flavor)
    if fresh:
        return ResumeDecision(
            directory=numbered_sibling(base),
            resume=False,
            reason="--fresh forces a new sibling run",
        )
    if explicit is not None:
        if not explicit.exists():
            return ResumeDecision(
                directory=explicit,
                resume=False,
                reason="explicit output directory is new",
            )
        if read_key(explicit) == identity_key:
            return ResumeDecision(
                directory=explicit,
                resume=True,
                reason="explicit output matches the bundle identity",
            )
        return ResumeDecision(
            directory=numbered_sibling(explicit),
            resume=False,
            reason="existing output does not match the bundle identity",
        )
    for candidate in sibling_candidates(base):
        if read_key(candidate) == identity_key:
            return ResumeDecision(
                directory=candidate,
                resume=True,
                reason="identity-matched sibling bundle resumes",
            )
    return ResumeDecision(
        directory=numbered_sibling(base),
        resume=False,
        reason="no identity-matched sibling exists",
    )
