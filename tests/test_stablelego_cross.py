"""Cross-validation against the StableLego release's test fixtures.

The nine layouts under ``tests/data/stablelego/`` are vendored verbatim from
the StableLego release (Liu et al., RA-L 2024, MIT licensed — see the LICENSE
file alongside), where each has a documented verdict: the 19-level stair
stands while the 20-level one collapses, the light stick stands while the
heavy one collapses until reinforced, and the external-weight pair brackets
the 200 g payload capacity. They are the best available ground truth for the
whole RBE stack — geometry, contact patterns, masses, and solver together.

The release-format loaders live in ``legolization.stablelego`` (shared with
``scripts/stablelego_sweep.py``); this file pins the verdicts and that the
loader reproduces the release's masses exactly.
"""

import json
from pathlib import Path

import pytest

from legolization.catalog import Catalog
from legolization.layout import Layout
from legolization.stability import analyze
from legolization.stablelego import (
    Library,
    layout_from_task_graph,
    load_library,
    load_task_graph,
    stablelego_catalog,
)

_DATA = Path(__file__).parent / "data" / "stablelego"

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
def library() -> Library:
    return load_library(_DATA / "lego_library.json")


@pytest.fixture(scope="module")
def catalog(library: Library) -> Catalog:
    return stablelego_catalog(library)


def _load_fixture(name: str, catalog: Catalog, library: Library) -> Layout:
    return layout_from_task_graph(
        load_task_graph(_DATA / f"{name}.json"),
        catalog=catalog,
        library=library,
    )


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
        expected_g = sum(
            library[str(entry["brick_id"])]["mass"] * 1000.0
            for entry in entries.values()
        )
        assert layout.total_mass_g() == pytest.approx(expected_g, rel=1e-6)


def test_stair_19_is_near_collapse(catalog, library):
    # The paper's headline example: 19 levels stand but with slim margin.
    result = analyze(_load_fixture("stair_19", catalog, library))
    assert result.stable
    assert result.max_score > 0.8
