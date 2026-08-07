"""Synthetic stress-test shape generators for the evaluation corpus.

Pure deterministic geometry (no RNG): regeneration is byte-identical.
Each generator returns an int16 LDraw-code array indexed (x, y,
layer-voxel). The generators are the committed source of truth for the
gitignored ``.npy`` files — never the files themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable

_EMPTY = -1  # legolization.grid.EMPTY, inlined so generators stay numpy-pure


def cantilever(length: int = 8, thickness: int = 2) -> np.ndarray:
    """Arm cantilevered over a base slab: stresses joint capacity + repair.

    The slab keeps the centre of mass over the support polygon so the model
    cannot topple; what remains is the knob-joint stress along the arm and
    (from ``length >= 8``) an unstable mid-build prefix for the sequencer.
    """
    base_depth = 4
    nx = 2 + length
    height = 7
    codes = np.full((nx, base_depth, height), _EMPTY, dtype=np.int16)
    codes[:, :, 0] = 7
    codes[:2, 1:3, 1 : height - thickness] = 4
    codes[:, 1:3, height - thickness :] = 14
    return codes


def topple_arm(length: int = 6, thickness: int = 2) -> np.ndarray:
    """One-sided arm on a narrow column: the whole model must topple.

    No brick joint fails here — the RBE verdict is global tipping (centre
    of mass beyond the 2x2 base). Expected unbuildable by physics; pins the
    solver's torque handling.
    """
    height = 6
    codes = np.full((2 + length, 2, height), _EMPTY, dtype=np.int16)
    codes[:2, :, :] = 4
    codes[2:, :, height - thickness :] = 14
    return codes


def mushroom(cap_radius: int = 6, stem_radius: int = 2) -> np.ndarray:
    """Wide cap on a thin stem: stresses overhang sequencing."""
    size = 2 * cap_radius + 1
    stem_height, cap_height = 5, 3
    codes = np.full((size, size, stem_height + cap_height), _EMPTY, dtype=np.int16)
    xs, ys = np.mgrid[0:size, 0:size]
    dist2 = (xs - cap_radius) ** 2 + (ys - cap_radius) ** 2
    codes[dist2 <= stem_radius**2, :stem_height] = 15
    codes[dist2 <= cap_radius**2, stem_height:] = 4
    return codes


def two_towers_bridge(gap: int = 6) -> np.ndarray:
    """Deck spanning two towers: stresses connectivity + mid-build spans."""
    tower, height, deck = 3, 8, 2
    nx = tower * 2 + gap
    codes = np.full((nx, tower, height + deck), _EMPTY, dtype=np.int16)
    codes[:tower, :, :height] = 1
    codes[-tower:, :, :height] = 1
    codes[:, :, height:] = 14
    return codes


def thin_shell(radius: int = 8) -> np.ndarray:
    """One-voxel-thick open dome: stresses fragmentation + seed variance."""
    size = 2 * radius + 1
    codes = np.full((size, size, radius + 1), _EMPTY, dtype=np.int16)
    xs, ys, zs = np.mgrid[0:size, 0:size, 0 : radius + 1]
    dist2 = (xs - radius) ** 2 + (ys - radius) ** 2 + zs**2
    shell = (dist2 <= radius**2) & (dist2 >= (radius - 1) ** 2)
    codes[shell] = 2
    return codes


def letter_t() -> np.ndarray:
    """Top-heavy T: bar ends dangle until the bar row completes."""
    depth, stem_width, stem_height = 2, 3, 7
    bar_width, bar_height = 11, 3
    codes = np.full(
        (bar_width, depth, stem_height + bar_height), _EMPTY, dtype=np.int16
    )
    x0 = (bar_width - stem_width) // 2
    codes[x0 : x0 + stem_width, :, :stem_height] = 1
    codes[:, :, stem_height:] = 4
    return codes


def letter_h() -> np.ndarray:
    """H crossbar suspended mid-air between posts: dangling-step risk.

    Single colour on purpose: under the hard colour constraint a
    two-colour H is *impossible* (no brick may span the colour boundary,
    so the bar never gets stud support). Monochrome, merged bricks anchor
    the bar into both posts and the challenge is purely sequencing.
    """
    depth, post_width, height, gap, bar_height = 2, 2, 10, 4, 2
    nx = post_width * 2 + gap
    codes = np.full((nx, depth, height), _EMPTY, dtype=np.int16)
    codes[:post_width, :, :] = 14
    codes[-post_width:, :, :] = 14
    mid = (height - bar_height) // 2
    codes[post_width : post_width + gap, :, mid : mid + bar_height] = 14
    return codes


def letter_h_bicolour() -> np.ndarray:
    """Two-colour H: hard colour constraint severs the bar's stud support.

    No brick may span the post/bar colour boundary, so the bar can never
    be anchored - expected unbuildable; pins the colour-constraint gate.
    """
    codes = letter_h()
    bar = codes[2:6, :, :]
    bar[bar != _EMPTY] = 4
    return codes


def staircase_overhang(offset: int = 1, steps: int = 8) -> np.ndarray:
    """Each tread shifts sideways: progressive overhang limit."""
    depth, tread = 4, 3
    nx = tread + offset * (steps - 1)
    codes = np.full((nx, depth, steps), _EMPTY, dtype=np.int16)
    for i in range(steps):
        x0 = i * offset
        codes[x0 : x0 + tread, :, i] = 2
    return codes


def wide_arch(span: int = 10) -> np.ndarray:
    """Flat lintel over a wide gap: keystone instability during build."""
    pier, depth, height, lintel = 2, 3, 6, 2
    nx = pier * 2 + span
    codes = np.full((nx, depth, height + lintel), _EMPTY, dtype=np.int16)
    codes[:pier, :, :height] = 15
    codes[-pier:, :, :height] = 15
    codes[:, :, height:] = 1
    return codes


def torsion_bridge(arm: int = 18) -> np.ndarray:
    """Twin corner towers joined by a one-stud dog-leg deck: yaw torque.

    The single-stud-wide deck leaves tower A along +x, turns the far
    corner, and runs along +y into tower B: each tower grips a long
    eccentric beam through a handful of knobs, the lateral-chain class
    where the τz (yaw) residual row measurably moves scores even with
    side presses placed at the physical face edges (kollsker seed 0:
    0.2025 → 0.2272 with ``torque_z=True``) while gravity-only verdicts
    never flip.
    """
    tower, height, deck = 2, 6, 2
    size = tower + arm
    codes = np.full((size, size, height + deck), _EMPTY, dtype=np.int16)
    codes[:tower, :tower, :height] = 1  # tower A at the origin corner
    codes[-tower:, -tower:, :height] = 1  # tower B at the far corner
    codes[:, 0, height:] = 14  # deck: one-stud +x run out of A
    codes[-1, :, height:] = 14  # deck: corner turn, one-stud +y run into B
    return codes


def press_tower(arms: int = 3) -> np.ndarray:
    """Stacked short cantilever arms on a slim column: press-fragile steps.

    Every prefix is statically stable (arms are short, the mass stays
    over the base), but each arm row anchors through just two knobs on
    the column — seating it under Liu's 1 kg virtual press tears the
    joint, so ``--insertion-check`` must flag arm steps while the plain
    audit stays clean.
    """
    column, arm_len, spacing = 2, 3, 2
    height = 1 + arms * spacing + 1
    nx = column + arm_len
    codes = np.full((nx, column, height), _EMPTY, dtype=np.int16)
    codes[:column, :, :] = 1  # the column
    for i in range(arms):
        z = 1 + i * spacing
        codes[:, 0, z] = 4  # arm row overlaps the column: stud-anchored
    return codes


def sparse_pillars() -> np.ndarray:
    """Four disconnected pillars: exercises the least-bad selection path."""
    codes = np.full((10, 10, 5), _EMPTY, dtype=np.int16)
    for x0, y0 in ((0, 0), (8, 0), (0, 8), (8, 8)):
        codes[x0 : x0 + 2, y0 : y0 + 2, :] = 7
    return codes


GENERATORS: dict[str, Callable[[], np.ndarray]] = {
    "cantilever": cantilever,
    "topple_arm": topple_arm,
    "mushroom": mushroom,
    "two_towers_bridge": two_towers_bridge,
    "thin_shell": thin_shell,
    "letter_t": letter_t,
    "letter_h": letter_h,
    "letter_h_bicolour": letter_h_bicolour,
    "staircase_overhang": staircase_overhang,
    "wide_arch": wide_arch,
    "torsion_bridge": torsion_bridge,
    "press_tower": press_tower,
    "sparse_pillars": sparse_pillars,
}
