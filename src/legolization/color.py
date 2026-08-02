"""RGB → nearest LDraw colour code quantization.

The palette is introspected from the generated ``ldraw.library.colours``
module, restricted to opaque solid colours (no metallic/chrome/glitter
finishes, no transparency) so every quantized code is a colour real bricks
come in. Distance is Euclidean in Oklab with the chroma axes over-weighted
so desaturated inputs stay on desaturated bricks (``_OKLAB_CHROMA_WEIGHT``).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from ldraw.colour import Colour

_PSEUDO_CODES = frozenset({16, 24})  # LDraw main/edge placeholder colours
_RGB_CHANNELS = 3

# Oklab is perceptually uniform, but the current LDConfig palette makes plain
# Euclidean Oklab wrong for builds: Black is #1B2A34, so near-black inputs sit
# in a perceptual tie between achromatic and dark saturated bricks (pure black
# lands on Dark_Brown #352100). Over-weighting the a/b axes keeps achromatic
# inputs on achromatic bricks; 2.5 is the threshold where the black/white/red
# anchors all resolve to their own codes, 3.0 adds margin.
_OKLAB_CHROMA_WEIGHT = 3.0

# Björn Ottosson's reference sRGB → Oklab matrices.
_SRGB_TO_LMS = np.asarray(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ]
)
_LMS_TO_OKLAB = np.asarray(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ]
)


def _srgb_to_oklab(rgbs: np.ndarray) -> np.ndarray:
    """Convert an ``(n, 3)`` array of 0-255 sRGB rows to Oklab rows."""
    scaled = rgbs / 255.0
    linear = np.where(
        scaled <= 0.04045,
        scaled / 12.92,
        ((scaled + 0.055) / 1.055) ** 2.4,
    )
    return np.cbrt(linear @ _SRGB_TO_LMS.T) @ _LMS_TO_OKLAB.T


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
    # real brick colours in nearest-colour lookups — Modulex_Dark_Brown
    # #330000 out-quantizes Black for near-black inputs.
    "Modulex",
    "Canvas",
)


@dataclass(frozen=True, slots=True)
class Palette:
    """An ordered LDraw colour palette with vectorized nearest-code lookup."""

    codes: np.ndarray
    rgbs: np.ndarray
    names: tuple[str, ...]
    oklabs: np.ndarray

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
        """Return the LDraw code whose colour is closest to ``rgb``."""
        return int(self.quantize(np.asarray([rgb], dtype=np.float64))[0])

    def _index_of(self, code: int) -> int:
        index = int(np.searchsorted(self.codes, code))
        if index >= len(self.codes) or self.codes[index] != code:
            msg = f"Colour code {code} not in palette"
            raise ValueError(msg)
        return index

    def quantize(self, rgbs: np.ndarray) -> np.ndarray:
        """Map an ``(n, 3)`` array of RGB values to LDraw colour codes."""
        pixels = _srgb_to_oklab(
            np.asarray(rgbs, dtype=np.float64).reshape(-1, _RGB_CHANNELS)
        )
        delta = pixels[:, None, :] - self.oklabs[None, :, :]
        distance = delta[:, :, 0] ** 2 + _OKLAB_CHROMA_WEIGHT * (
            delta[:, :, 1] ** 2 + delta[:, :, 2] ** 2
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
    rgbs = np.asarray([seen[int(c)][1] for c in codes], dtype=np.float64)
    return Palette(
        codes=codes,
        rgbs=rgbs,
        names=tuple(seen[int(c)][0] for c in codes),
        oklabs=_srgb_to_oklab(rgbs),
    )
