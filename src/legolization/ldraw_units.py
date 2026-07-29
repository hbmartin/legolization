"""Shared LDraw coordinate scales and import tolerances."""

from typing import Final

STUD_LDU: Final[float] = 20.0
"""LDraw units in one horizontal stud pitch."""

PLATE_LDU: Final[float] = 8.0
"""LDraw units in one vertical plate height."""

GRID_TOLERANCE_LDU: Final[float] = 0.2
"""Maximum positional transform noise accepted on every axis."""

ROTATION_TOLERANCE: Final[float] = 1e-2
"""Maximum dimensionless matrix-entry noise accepted during yaw decoding."""
