"""Winner selection across strategies, seeds, and colour variants.

Every candidate's selection objective is evaluated against one
canonical reference — the hard/no-dither source-colour grid — so a
dithered-target candidate cannot win by scoring against its own
softened target. The comparison report records both the
self-referenced metrics and the canonical selection numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from legolization.compare import CandidateMetrics

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REPORT_SCHEMA = "legolization.bundle-comparison/v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class BundleCandidate:
    """One harvested candidate outcome, scored for selection."""

    strategy: str
    seed: int
    variant: str
    status: str
    seconds: float
    error: str | None = None
    metrics: CandidateMetrics | None = None
    selection_objective: float | None = None
    cross_colour_error: float | None = None
    model_relpath: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the candidate finished and was scored."""
        return self.status == "ok" and self.metrics is not None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """Rehydrate a candidate from a worker result payload."""
        metrics_payload = payload.get("metrics")
        metrics = (
            CandidateMetrics(**metrics_payload)
            if isinstance(metrics_payload, dict)
            else None
        )
        return cls(
            strategy=str(payload["strategy"]),
            seed=int(payload["seed"]),
            variant=str(payload.get("variant", "hard")),
            status=str(payload.get("status", "error")),
            seconds=float(payload.get("seconds", 0.0)),
            error=(str(payload["error"]) if payload.get("error") is not None else None),
            metrics=metrics,
            selection_objective=_optional_float(payload.get("selection_objective")),
            cross_colour_error=_optional_float(payload.get("cross_colour_error")),
            model_relpath=(
                str(payload["model_relpath"])
                if payload.get("model_relpath") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for the comparison report."""
        from dataclasses import asdict  # noqa: PLC0415

        return {
            "strategy": self.strategy,
            "seed": self.seed,
            "variant": self.variant,
            "status": self.status,
            "seconds": round(self.seconds, 3),
            "error": self.error,
            "selection_objective": self.selection_objective,
            "cross_colour_error": self.cross_colour_error,
            "metrics": asdict(self.metrics) if self.metrics is not None else None,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class BundleSelection:
    """The winner, why it won, and the complete comparison field."""

    winner: BundleCandidate | None
    reason: str
    candidates: tuple[BundleCandidate, ...]

    def to_report(self) -> dict[str, Any]:
        """Return the ``comparison/report.json`` payload."""
        return {
            "schema": REPORT_SCHEMA,
            "winner": (
                {
                    "strategy": self.winner.strategy,
                    "seed": self.winner.seed,
                    "variant": self.winner.variant,
                }
                if self.winner is not None
                else None
            ),
            "reason": self.reason,
            "buildable": (
                self.winner is not None
                and self.winner.metrics is not None
                and self.winner.metrics.buildable
            ),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def select_bundle_winner(
    candidates: Sequence[BundleCandidate],
) -> BundleSelection:
    """Pick the winner lexicographically over the canonical objective."""
    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (item.strategy, item.seed, item.variant),
        )
    )
    scored = [item for item in ordered if item.ok and item.metrics is not None]
    if not scored:
        return BundleSelection(
            winner=None,
            reason="no candidate completed successfully",
            candidates=ordered,
        )
    buildable = [
        item for item in scored if item.metrics is not None and item.metrics.buildable
    ]
    if buildable:
        winner = min(buildable, key=_winner_key)
        metrics = winner.metrics
        assert metrics is not None  # noqa: S101 - ok gate above
        colour_error = (
            winner.cross_colour_error
            if winner.cross_colour_error is not None
            else metrics.colour_error
        )
        reason = (
            f"buildable, best canonical objective "
            f"{_objective(winner):.4f} (colour error {colour_error:.4f}, "
            f"{metrics.brick_count} bricks) among {len(buildable)} "
            f"buildable candidate(s)"
        )
        return BundleSelection(winner=winner, reason=reason, candidates=ordered)
    winner = min(scored, key=_least_bad_key)
    return BundleSelection(
        winner=winner,
        reason="no candidate is buildable; least-bad retained for diagnostics",
        candidates=ordered,
    )


def _objective(candidate: BundleCandidate) -> float:
    if candidate.selection_objective is not None:
        return candidate.selection_objective
    assert candidate.metrics is not None  # noqa: S101 - ok gate upstream
    return candidate.metrics.objective_total


def _winner_key(
    candidate: BundleCandidate,
) -> tuple[float, float, int, str, int, str]:
    metrics = candidate.metrics
    assert metrics is not None  # noqa: S101 - ok gate upstream
    return (
        _objective(candidate),
        -metrics.maximin_capacity,
        metrics.brick_count,
        candidate.strategy,
        candidate.seed,
        candidate.variant,
    )


def _least_bad_key(
    candidate: BundleCandidate,
) -> tuple[int, float, float, str, int, str]:
    metrics = candidate.metrics
    assert metrics is not None  # noqa: S101 - ok gate upstream
    return (
        metrics.component_count,
        metrics.max_score,
        _objective(candidate),
        candidate.strategy,
        candidate.seed,
        candidate.variant,
    )


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
