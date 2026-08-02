"""Typed failures shared by CLI and domain boundaries."""

from __future__ import annotations


class LegolizationError(Exception):
    """Base class for expected user-facing failures."""


class ConfigurationError(LegolizationError, ValueError):
    """Raised when configuration is malformed or inconsistent."""


class ManifestError(LegolizationError, ValueError):
    """Raised when an assembly manifest fails validation."""


class UnsupportedCapabilityError(LegolizationError):
    """Raised when exact analysis is unavailable for requested geometry."""


class ExactPlacementLimitError(LegolizationError):
    """Raised when exact placement reaches a configured hard policy limit."""


class PlacementInfeasibleError(LegolizationError):
    """Raised when no placement satisfies the requested hard constraints."""
