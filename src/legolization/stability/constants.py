"""Physical constants for the Rigid-Block-Equilibrium model.

Transcribed from StableLego (Liu et al.) and its released implementation —
values are measured/fitted there, not re-derived here.
"""

from __future__ import annotations

T_CAPACITY_N = 0.98
"""Per-contact-point friction capacity in newtons (100 g x g)."""

ALPHA = 1e-3
"""Objective weight on each brick's maximum drag force."""

BETA = 1e-6
"""Objective weight on the total drag force across the structure."""

GRAVITY = 9.8
"""Gravitational acceleration in N/kg."""

KNOB_PITCH_M = 0.0078
"""In-plane lever unit: one knob pitch in meters (StableLego value)."""

PLATE_HEIGHT_M = 0.0032
"""One plate height in meters (9.6 mm brick / 3)."""

FOUR_POINT_OFFSETS: tuple[tuple[float, float], ...] = (
    (0.0, -0.25),
    (-0.25, 0.0),
    (0.0, 0.25),
    (0.25, 0.0),
)
"""Contact points per knob for 1xX cavities: a diamond of four points at
±0.25 pitch (±1.95 mm) around the stud center."""

THREE_POINT_OFFSETS: tuple[tuple[float, float], ...] = (
    (0.125, -0.125),
    (-0.25, 0.0),
    (0.125, 0.125),
)
"""Contact points per knob for wider cavities: a triangle with its apex at
-0.25 pitch in x (StableLego keeps it axis-aligned regardless of yaw)."""

K_DIRECTIONS: tuple[tuple[float, float], ...] = (
    (1.0, 0.0),
    (-1.0, 0.0),
    (0.0, 1.0),
    (0.0, -1.0),
)
"""The four horizontal knob-press directions inside a mated knob."""
