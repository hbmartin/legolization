"""Cross-validation against the StableLego release's test fixtures.

The nine layouts under ``tests/data/stablelego/`` are vendored verbatim from
the StableLego release (Liu et al., RA-L 2024, MIT licensed — see the LICENSE
file alongside), where each has a documented verdict: the 19-level stair
stands while the 20-level one collapses, the light stick stands while the
heavy one collapses until reinforced, and the external-weight pair brackets
the 200 g payload capacity. They are the best available ground truth for the
whole RBE stack — geometry, contact patterns, masses, and solver together.

Fixture format: ``{step: {x, y, z, ori, brick_id}}`` with ``brick_id``
resolved through ``lego_library.json`` to a ``height x width`` stud footprint
(``ori`` swaps the axes) and a mass in kg. ``z`` counts brick heights with
the lowest layer resting on the baseplate. ``brick_id`` 1 is a 200 g payload
block with a 2x2 footprint.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from legolization.catalog import Catalog, Part, default_catalog
from legolization.layout import Layout
from legolization.stability import analyze

_DATA = Path(__file__).parent / "data" / "stablelego"
_COLOUR = 4
_PLATES_PER_BRICK = 3

_STABLE_FIXTURES = (
    "stair_19",
    "stair_20_good",
    "stick_light",
    "stick_heavy_good",
    "stick_heavy_good_test_horizontal_force",
    "external_weight_good",
)
_UNSTABLE_FIXTURES = ("stair_20", "stick_heavy", "external_weight_fail")


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    base = default_catalog()
    payload = replace(base["brick_2x2"], key="weight_2x2", mass_g=200.0)
    return Catalog(parts={**base.parts, "weight_2x2": payload})


@pytest.fixture(scope="module")
def library() -> dict[str, dict[str, float]]:
    return json.loads((_DATA / "lego_library.json").read_text())


def _extents(part: Part) -> tuple[int, int]:
    xs = [dx for dx, _ in part.footprint]
    ys = [dy for _, dy in part.footprint]
    return max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def _load_fixture(
    name: str,
    catalog: Catalog,
    library: dict[str, dict[str, float]],
) -> Layout:
    layout = Layout(catalog=catalog)
    entries = json.loads((_DATA / f"{name}.json").read_text())
    for entry in entries.values():
        spec = library[str(entry["brick_id"])]
        x_extent, y_extent = int(spec["height"]), int(spec["width"])
        if entry["ori"]:
            x_extent, y_extent = y_extent, x_extent
        key = (
            "weight_2x2"
            if entry["brick_id"] == 1
            else catalog.rect_key(x_extent, y_extent, _PLATES_PER_BRICK)
        )
        assert key is not None, f"no part for {x_extent}x{y_extent}"
        layer = _PLATES_PER_BRICK * entry["z"]
        if _extents(catalog[key]) == (x_extent, y_extent):
            layout.add(key, entry["x"], entry["y"], layer, 0, _COLOUR)
        else:
            # Yaw 90 rotates (dx, dy) to (-dy, dx): anchor at the max-x cell.
            layout.add(key, entry["x"] + x_extent - 1, entry["y"], layer, 90, _COLOUR)
    return layout


def _fixture_masses_match_catalog(
    layout: Layout,
    entries: dict,
    library: dict[str, dict[str, float]],
) -> None:
    expected_g = sum(
        library[str(entry["brick_id"])]["mass"] * 1000.0 for entry in entries.values()
    )
    assert layout.total_mass_g() == pytest.approx(expected_g, rel=1e-6)


@pytest.mark.parametrize("name", _STABLE_FIXTURES)
def test_stablelego_fixture_is_stable(name, catalog, library):
    layout = _load_fixture(name, catalog, library)
    result = analyze(layout)
    assert result.stable
    assert result.max_score < 1.0


@pytest.mark.parametrize("name", _UNSTABLE_FIXTURES)
def test_stablelego_fixture_collapses(name, catalog, library):
    layout = _load_fixture(name, catalog, library)
    result = analyze(layout)
    assert not result.stable
    assert result.unstable_ids


def test_fixture_masses_match_release_library(catalog, library):
    # Guards the loader itself: every brick resolved to a part whose mass
    # matches the release's library entry (StableLego masses are in kg).
    for name in (*_STABLE_FIXTURES, *_UNSTABLE_FIXTURES):
        entries = json.loads((_DATA / f"{name}.json").read_text())
        layout = _load_fixture(name, catalog, library)
        _fixture_masses_match_catalog(layout, entries, library)


def test_stair_19_is_near_collapse(catalog, library):
    # The paper's headline example: 19 levels stand but with slim margin.
    result = analyze(_load_fixture("stair_19", catalog, library))
    assert result.stable
    assert result.max_score > 0.8
