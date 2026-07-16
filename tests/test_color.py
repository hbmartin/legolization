"""LDraw colour palette extraction and quantization."""

import numpy as np
import pytest
from ldraw.colour import Colour

from legolization.color import _is_solid, default_palette


def test_primary_colours_quantize_exactly():
    palette = default_palette()
    assert palette.nearest((201, 26, 9)) == 4  # LDraw Red #C91A09
    assert palette.nearest((255, 255, 255)) == 15  # White
    assert palette.nearest((0, 0, 0)) == 0  # Black


def test_palette_is_solid_only():
    palette = default_palette()
    banned = (
        "Trans",
        "Glitter",
        "Chrome",
        "Speckle",
        "Milky",
        "Rubber",
        "Modulex",
        "Canvas",
    )
    for name in palette.names:
        assert not any(token in name for token in banned), name
    assert 16 not in palette.codes
    assert 24 not in palette.codes


def test_non_system_brick_lines_are_filtered():
    # Newer LDConfig releases add Modulex (an incompatible brick line) and
    # Canvas (fabric-part) colours; both must stay out of the palette even
    # when the local library predates them, so test the filter directly.
    non_system = (
        ("Modulex_Clear", 30_000, "#FCFCFC"),
        ("Modulex_Dark_Brown", 30_054, "#330000"),
        ("Canvas_White", 20_015, "#F4F4F4"),
    )
    for name, code, rgb in non_system:
        colour = Colour(code=code, name=name, rgb=rgb, alpha=255)
        assert not _is_solid(name, colour), name
    kept = Colour(code=15, name="White", rgb="#F4F4F4", alpha=255)
    assert _is_solid("White", kept)


def test_quantize_array_shape():
    palette = default_palette()
    rgbs = np.asarray([[255, 0, 0], [10, 10, 10], [250, 250, 250]])
    codes = palette.quantize(rgbs)
    assert codes.shape == (3,)
    assert codes[1] == 0
    assert codes[2] == 15


def test_rgb_roundtrip():
    palette = default_palette()
    for code in (0, 1, 4, 14, 15):
        rgb = palette.rgb_of(code)
        assert palette.nearest(rgb) == code


def test_missing_palette_code_has_clear_error():
    palette = default_palette()
    with pytest.raises(ValueError, match="Colour code 9999 not in palette"):
        palette.rgb_of(9999)
    with pytest.raises(ValueError, match="Colour code 9999 not in palette"):
        palette.name_of(9999)
