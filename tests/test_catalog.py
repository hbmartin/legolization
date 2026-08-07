"""Part abstraction, catalog expansion, and estimate-sidecar invariants."""

import json
from pathlib import Path

import pytest

from legolization.catalog import (
    CATALOG_ESTIMATES_SCHEMA,
    Catalog,
    Category,
    catalog_content_hash,
    catalog_hash,
    default_catalog,
    load_catalog,
    load_estimate_sidecar,
    resolve_catalog,
    rotate_offset,
)
from legolization.errors import ConfigurationError


@pytest.fixture(scope="module")
def catalog():
    return default_catalog()


def _write_extension(tmp_path: Path, *, with_mass: bool) -> Path:
    spec: dict[str, object] = {
        "key": "brick_1x5_custom",
        "ldraw_part": "9998",
        "category": "brick",
        "size": [1, 5],
        "height_plates": 3,
    }
    if with_mass:
        spec["mass_g"] = 1.9
    path = tmp_path / "extension.json"
    path.write_text(json.dumps({"schema": 2, "parts": [spec]}))
    return path


def _write_sidecar(
    tmp_path: Path, document: dict, name: str = "estimates.json"
) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document))
    return path


def _estimate_document(**overrides) -> dict:
    estimate = {
        "part": "brick_1x5_custom",
        "fields": {"mass_g": 2.4},
        "provenance": {
            "method": "volumetric",
            "basis": "scaled from brick_1x4 volume",
            "source_url": "https://example.test/brick",
            "confidence": 0.8,
            "retrieved_at": "2026-08-06",
        },
    }
    estimate.update(overrides)
    return {"schema": CATALOG_ESTIMATES_SCHEMA, "estimates": [estimate]}


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


def test_load_catalog_without_extensions_reuses_default():
    assert load_catalog() is default_catalog()


@pytest.mark.parametrize("key", ["brick_1x2_legacy", "plate_1x2_legacy"])
def test_legacy_rectangles_are_explicitly_replaceable(catalog, key):
    assert catalog[key].replaceable_geometry is True


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


def test_snot_specs_validate_role(catalog):
    from legolization.catalog import _snot_part

    with pytest.raises(ValueError, match="snot_role"):
        _snot_part({"key": "mystery_bracket", "category": "snot"})
    with pytest.raises(ValueError, match="snot_role"):
        _snot_part({"key": "bad", "category": "snot", "snot_role": "sideways"})


def test_estimate_sidecar_loads_labeled_records(tmp_path: Path):
    path = _write_sidecar(tmp_path, _estimate_document())

    (record,) = load_estimate_sidecar(path)

    assert record.part == "brick_1x5_custom"
    assert record.fields == {"mass_g": 2.4}
    assert record.provenance.method == "volumetric"
    assert record.provenance.basis == "scaled from brick_1x4 volume"
    assert record.provenance.confidence == 0.8
    assert record.provenance.retrieved_at == "2026-08-06"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"estimates": []}, "must declare schema"),
        (
            {"schema": CATALOG_ESTIMATES_SCHEMA, "estimates": [], "extra": 1},
            "unknown keys: extra",
        ),
        ({"schema": CATALOG_ESTIMATES_SCHEMA, "estimates": {}}, "must be a list"),
        (_estimate_document(unexpected=True), "unknown keys: unexpected"),
        (
            _estimate_document(fields={"volume_ml": 3.0}),
            "unknown fields: volume_ml",
        ),
        (_estimate_document(fields={}), "non-empty fields map"),
        (_estimate_document(fields={"mass_g": 0}), "finite and positive"),
        (
            _estimate_document(provenance={"method": "guessing", "basis": "vibes"}),
            "method must be one of",
        ),
        (
            _estimate_document(provenance={"method": "volumetric", "basis": ""}),
            "non-empty basis",
        ),
        (
            _estimate_document(
                provenance={
                    "method": "volumetric",
                    "basis": "ok",
                    "confidence": 1.5,
                }
            ),
            r"within \[0, 1\]",
        ),
    ],
)
def test_estimate_sidecar_rejects_malformed_documents(
    tmp_path: Path, document, message
):
    path = _write_sidecar(tmp_path, document)

    with pytest.raises(ConfigurationError, match=message):
        load_estimate_sidecar(path)


def test_builtin_style_catalog_still_requires_mass(tmp_path: Path):
    path = _write_extension(tmp_path, with_mass=False)

    with pytest.raises(ValueError, match="missing mass_g"):
        Catalog.load(path)


def test_massless_extension_part_is_a_draft_until_resolved(tmp_path: Path):
    path = _write_extension(tmp_path, with_mass=False)

    merged = load_catalog(path)

    assert merged["brick_1x5_custom"].has_mass is False
    assert merged["brick_2x4"].has_mass is True


def test_resolve_catalog_applies_sidecar_masses_verbatim(tmp_path: Path):
    extension = _write_extension(tmp_path, with_mass=False)
    sidecar = _write_sidecar(tmp_path, _estimate_document())

    resolved = resolve_catalog((extension,), (sidecar,))

    part = resolved.catalog["brick_1x5_custom"]
    assert part.mass_g == 2.4
    assert part.has_mass is True
    provenance = resolved.provenance
    assert provenance.extensions == (extension,)
    assert provenance.estimate_sidecars == (sidecar,)
    assert set(provenance.estimated_parts) == {"brick_1x5_custom"}
    assert provenance.content_hash == catalog_content_hash((extension,), (sidecar,))


def test_resolve_catalog_names_uncovered_massless_parts(tmp_path: Path):
    extension = _write_extension(tmp_path, with_mass=False)

    with pytest.raises(
        ConfigurationError,
        match="--catalog-estimates covering: brick_1x5_custom",
    ):
        resolve_catalog((extension,), ())


def test_resolve_catalog_ignores_estimates_for_massful_parts(tmp_path: Path):
    extension = _write_extension(tmp_path, with_mass=True)
    sidecar = _write_sidecar(tmp_path, _estimate_document())

    resolved = resolve_catalog((extension,), (sidecar,))

    assert resolved.catalog["brick_1x5_custom"].mass_g == 1.9
    assert resolved.provenance.estimated_parts == {}


def test_catalog_provenance_payload_shape(tmp_path: Path):
    extension = _write_extension(tmp_path, with_mass=False)
    sidecar = _write_sidecar(tmp_path, _estimate_document())

    payload = resolve_catalog((extension,), (sidecar,)).provenance.to_payload()

    assert set(payload) == {
        "builtin_sha256",
        "extensions",
        "estimate_sidecars",
        "estimated_parts",
    }
    assert payload["builtin_sha256"] == catalog_hash()
    assert payload["extensions"] == [str(extension)]
    assert payload["estimate_sidecars"] == [str(sidecar)]
    (estimate,) = payload["estimated_parts"]["brick_1x5_custom"]
    assert estimate["fields"] == {"mass_g": 2.4}
    assert estimate["provenance"]["method"] == "volumetric"


def test_catalog_content_hash_is_stable_and_extends_catalog_hash(tmp_path: Path):
    extension = _write_extension(tmp_path, with_mass=True)
    sidecar = _write_sidecar(tmp_path, _estimate_document())

    assert catalog_content_hash() == catalog_hash()
    assert catalog_content_hash((extension,)) == catalog_content_hash((extension,))
    assert catalog_content_hash((extension,)) != catalog_content_hash()
    assert catalog_content_hash((extension,), (sidecar,)) != catalog_content_hash(
        (extension,)
    )


def test_cladding_mount_matrices_pinned(catalog):
    # The emission rotations are catalog data; every cladding must pin
    # all four outward directions and expose them via mount_matrix.
    for part in catalog.by_category(Category.SNOT):
        if part.mount_normal is None:
            continue
        outwards = {direction for direction, _ in part.mount_matrices}
        assert outwards == {(1, 0), (-1, 0), (0, 1), (0, -1)}
        for direction in outwards:
            rows = part.mount_matrix(direction)
            assert rows is not None
            assert len(rows) == 9
        assert part.mount_matrix((2, 0)) is None
