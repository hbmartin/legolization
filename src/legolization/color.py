"""RGB → nearest LDraw colour code quantization.

The palette is introspected from the generated ``ldraw.library.colours``
module, restricted to opaque solid colours (no metallic/chrome/glitter
finishes, no transparency) so every quantized code is a colour real bricks
come in. Distance uses the "redmean" weighted-Euclidean approximation, a
cheap stand-in for perceptual distance that needs no extra dependencies.
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
        """Return the LDraw code whose colour is redmean-closest to ``rgb``."""
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
        mean_red = (pixels[:, None, 0] + self.rgbs[None, :, 0]) / 2.0
        delta = pixels[:, None, :] - self.rgbs[None, :, :]
        distance = (
            (2.0 + mean_red / 256.0) * delta[:, :, 0] ** 2
            + 4.0 * delta[:, :, 1] ** 2
            + (2.0 + (255.0 - mean_red) / 256.0) * delta[:, :, 2] ** 2
        )
        return self.codes[np.argmin(distance, axis=1)]


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
