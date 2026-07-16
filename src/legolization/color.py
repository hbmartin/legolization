"""RGB → nearest LDraw colour code quantization.

The palette is introspected from the generated ``ldraw.library.colours``
module, restricted to opaque solid colours (no metallic/chrome/glitter
finishes, no transparency) so every quantized code is a colour real bricks
come in. Distance is CIE76 (squared Euclidean in CIELAB, D65): cheap,
dependency-free, and chroma-aware where it matters — RGB-space
approximations like redmean under-penalize chroma near black, mapping pure
black inputs to Dark Brown #352100 instead of Black #1B2A34 under the
measured colour values LDConfig ships nowadays.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from ldraw.colour import Colour

_PSEUDO_CODES = frozenset({16, 24})  # LDraw main/edge placeholder colours
_RGB_CHANNELS = 3

# The generated colours module encodes finish only in the colour *name*
# (attributes and alpha are unreliable), so filter by name tokens.
_NON_SOLID_TOKENS = (
    "Trans",
    "Chrome",
    "Pearl",
    "Metallic",
    "Milky",
    "Glitter",
    "Speckle",
    "Rubber",
    "Glow",
    "Magnet",
    "Electric",
    "Undefined",
    "Colour",
    # Present in newer LDConfig releases but not System bricks: Modulex is
    # a separate, incompatible brick line (and Modulex_Clear is translucent
    # despite its name), Canvas colours belong to fabric parts. Both shadow
    # real brick colours under redmean — Modulex_Dark_Brown #330000
    # out-quantizes Black for near-black inputs.
    "Modulex",
    "Canvas",
)


@dataclass(frozen=True, slots=True)
class Palette:
    """An ordered LDraw colour palette with vectorized nearest-code lookup."""

    codes: np.ndarray
    rgbs: np.ndarray
    names: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.codes)

    def rgb_of(self, code: int) -> tuple[int, int, int]:
        """Return the palette RGB of an LDraw colour code."""
        index = self._index_of(code)
        r, g, b = (int(v) for v in self.rgbs[index])
        return (r, g, b)

    def name_of(self, code: int) -> str:
        """Return the LDraw name of a colour code."""
        return self.names[self._index_of(code)]

    def nearest(self, rgb: tuple[int, int, int]) -> int:
        """Return the LDraw code whose colour is CIELAB-closest to ``rgb``."""
        return int(self.quantize(np.asarray([rgb], dtype=np.float64))[0])

    def _index_of(self, code: int) -> int:
        index = int(np.searchsorted(self.codes, code))
        if index >= len(self.codes) or self.codes[index] != code:
            msg = f"Colour code {code} not in palette"
            raise ValueError(msg)
        return index

    def quantize(self, rgbs: np.ndarray) -> np.ndarray:
        """Map an ``(n, 3)`` array of RGB values to LDraw colour codes."""
        pixels = np.asarray(rgbs, dtype=np.float64).reshape(-1, _RGB_CHANNELS)
        delta = _srgb_to_lab(pixels)[:, None, :] - _srgb_to_lab(self.rgbs)[None, :, :]
        distance = (delta**2).sum(axis=2)
        return self.codes[np.argmin(distance, axis=1)]


_SRGB_TO_XYZ = np.array(
    [
        [0.412_456_4, 0.357_576_1, 0.180_437_5],
        [0.212_672_9, 0.715_152_2, 0.072_175_0],
        [0.019_333_9, 0.119_192_0, 0.950_304_1],
    ]
)
_D65_WHITE = np.array([0.950_47, 1.0, 1.088_83])
_LAB_EPSILON = (6.0 / 29.0) ** 3


def _srgb_to_lab(rgbs: np.ndarray) -> np.ndarray:
    """Convert an ``(..., 3)`` array of 0-255 sRGB values to CIELAB (D65)."""
    srgb = np.asarray(rgbs, dtype=np.float64) / 255.0
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    xyz = linear @ _SRGB_TO_XYZ.T / _D65_WHITE
    f = np.where(
        xyz > _LAB_EPSILON,
        np.cbrt(xyz),
        xyz / (3.0 * (6.0 / 29.0) ** 2) + 4.0 / 29.0,
    )
    return np.stack(
        [
            116.0 * f[..., 1] - 16.0,
            500.0 * (f[..., 0] - f[..., 1]),
            200.0 * (f[..., 1] - f[..., 2]),
        ],
        axis=-1,
    )


def _is_solid(name: str, colour: Colour) -> bool:
    return (
        colour.code is not None
        and colour.code not in _PSEUDO_CODES
        and colour.rgb is not None
        and all(token not in name for token in _NON_SOLID_TOKENS)
    )


@lru_cache(maxsize=1)
def default_palette() -> Palette:
    """Build the opaque solid-colour LDraw palette from pyldraw3."""
    colours_module = importlib.import_module("ldraw.library.colours")
    seen: dict[int, tuple[str, tuple[int, int, int]]] = {}
    for name, value in vars(colours_module).items():
        if (
            isinstance(value, Colour)
            and _is_solid(name, value)
            and value.code not in seen
        ):
            rgb_hex = str(value.rgb).removeprefix("#")
            rgb = (
                int(rgb_hex[0:2], 16),
                int(rgb_hex[2:4], 16),
                int(rgb_hex[4:6], 16),
            )
            seen[int(value.code or 0)] = (name, rgb)
    codes = np.asarray(sorted(seen), dtype=np.int16)
    return Palette(
        codes=codes,
        rgbs=np.asarray([seen[int(c)][1] for c in codes], dtype=np.float64),
        names=tuple(seen[int(c)][0] for c in codes),
    )
