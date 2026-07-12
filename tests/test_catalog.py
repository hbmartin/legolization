"""Part abstraction and catalog expansion invariants."""

import pytest

from legolization.catalog import (
    Category,
    default_catalog,
    rotate_offset,
)


@pytest.fixture(scope="module")
def catalog():
    return default_catalog()


def test_brick_2x4_expansion(catalog):
    part = catalog["brick_2x4"]
    assert part.height_plates == 3
    assert part.cell_count == 2 * 4 * 3
    assert part.stud_count == 8
    assert len(part.bottom_connectors) == 8
    assert part.ldraw_part == "3001"
    assert part.filled_cells == part.occupied_cells


def test_tile_has_no_studs(catalog):
    part = catalog["tile_1x2"]
    assert part.stud_count == 0
    assert len(part.bottom_connectors) == 2


def test_slope_partial_fill(catalog):
    part = catalog["slope_45_2x1"]
    assert part.category is Category.SLOPE
    assert part.height_plates == 3
    # Full box for collision, stud column + slope toe for shape.
    assert part.cell_count == 2 * 3
    assert len(part.filled_cells) == 3 + 1
    assert part.stud_count == 1
    assert len(part.bottom_connectors) == 2
    # LDraw origin sits at the stud cell, 10 LDU behind footprint center.
    assert part.origin_offset == (0.0, 0.0, 10.0)


def test_orientations_reduced_for_squares(catalog):
    assert catalog["brick_1x1"].orientations == (0,)
    assert catalog["brick_2x2"].orientations == (0, 90)
    assert catalog["brick_2x4"].orientations == (0, 90, 180, 270)


@pytest.mark.parametrize("yaw", [0, 90, 180, 270])
def test_rotation_preserves_structure(catalog, yaw):
    part = catalog["brick_2x4"]
    cells = part.cells_at(5, 7, 2, yaw)
    assert len(cells) == part.cell_count
    assert len(set(cells)) == part.cell_count
    studs = part.connectors_at(5, 7, 2, yaw, top=True)
    assert all(conn.direction == (0, 0, 1) for conn in studs)
    assert {c.cell for c in studs} <= set(cells)


def test_rotate_offset_cycle():
    cell = (3, 1, 2)
    once = rotate_offset(cell, 90)
    assert once == (-1, 3, 2)
    assert rotate_offset(once, 270) == cell
    with pytest.raises(ValueError, match="multiple of 90"):
        rotate_offset(cell, 45)


def test_rect_key_lookup(catalog):
    assert catalog.rect_key(2, 4, 3) == "brick_2x4"
    assert catalog.rect_key(4, 2, 3) == "brick_2x4"
    assert catalog.rect_key(1, 6, 1) == "plate_1x6"
    assert catalog.rect_key(3, 3, 3) is None
    assert catalog.rect_key(1, 2, 1, category=Category.TILE) == "tile_1x2"
    assert catalog.rect_key(2, 8, 1, category=Category.TILE) is None


def test_masses_positive(catalog):
    for part in catalog.parts.values():
        assert part.mass_g > 0


def test_plate_masses_are_realistic(catalog):
    # Real plates weigh ~0.45-0.55x the same-footprint brick, never the
    # exact one-third the naive height ratio suggests (audit finding F8).
    for plate in catalog.by_category(Category.PLATE):
        width, length = _footprint_size(plate)
        brick_key = catalog.rect_key(width, length, 3, category=Category.BRICK)
        assert brick_key is not None
        brick = catalog[brick_key]
        assert brick.mass_g / 3 < plate.mass_g < 0.6 * brick.mass_g


def test_tile_masses_below_same_footprint_plate(catalog):
    for tile in catalog.by_category(Category.TILE):
        width, length = _footprint_size(tile)
        plate_key = catalog.rect_key(width, length, 1, category=Category.PLATE)
        assert plate_key is not None
        assert tile.mass_g < catalog[plate_key].mass_g


def _footprint_size(part) -> tuple[int, int]:
    xs = [dx for dx, _ in part.footprint]
    ys = [dy for _, dy in part.footprint]
    return max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
