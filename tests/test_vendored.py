"""Guards over the vendored third-party code in ``tools/vendored``.

Vendoring is a licence obligation and a measurement anchor at the same time:
the MIT notice must travel with the copied source, and the copy must stay
byte-identical to the release the committed analyses were measured against.
Both properties are trivial to break silently, so both are pinned here.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).parent.parent
_VENDORED = _REPO / "tools" / "vendored"
_BRICKNET = _VENDORED / "bricknet"
_BRICKNET_TABLES = _REPO / "references" / "bricknet-data"

# The four reference tables vendored twice on purpose: once inside the package
# snapshot, once as attribution-carrying reference data. They must never drift.
_SHARED_TABLES = (
    "color_names.json",
    "labels.json.xz",
    "part_aliases.json.xz",
    "part_names.json",
)


def _pyproject_bricknet_pin() -> str:
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data["dependency-groups"]["dev"]
    specs = [entry for entry in dev if entry.startswith("bricknet")]
    assert specs, "bricknet missing from the dev dependency group"
    return specs[0]


def test_bricknet_licence_travels_with_the_vendored_source():
    licence = (_VENDORED / "LICENSE-bricknet.txt").read_text(encoding="utf-8")
    assert "MIT License" in licence
    assert "Peter Kulits" in licence


def test_bricknet_vendor_is_documented_and_noted_in_the_root_readme():
    vendored_readme = (_VENDORED / "README.md").read_text(encoding="utf-8")
    assert "bricknet" in vendored_readme
    assert "0.1.0" in vendored_readme
    assert "LICENSE-bricknet.txt" in vendored_readme

    root_readme = (_REPO / "README.md").read_text(encoding="utf-8")
    assert "LICENSE-bricknet.txt" in root_readme
    assert "Peter Kulits" in root_readme


def test_bricknet_vendored_version_matches_the_exact_pin():
    source = (_BRICKNET / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "(?P<version>[^"]+)"$', source, re.MULTILINE)
    assert match is not None, "vendored bricknet/__init__.py lost its __version__"
    version = match.group("version")

    pin = _pyproject_bricknet_pin()
    assert pin == f"bricknet=={version}", (
        f"pyproject pins {pin!r} but tools/vendored/bricknet is {version}; "
        "the installed release and the frozen snapshot must be the same release"
    )


def test_bricknet_data_tables_are_byte_identical_across_both_vendored_copies():
    for name in _SHARED_TABLES:
        packaged = (_BRICKNET / "_data" / "v1" / name).read_bytes()
        reference = (_BRICKNET_TABLES / name).read_bytes()
        assert packaged == reference, (
            f"{name} differs between tools/vendored/bricknet/_data/v1 and "
            "references/bricknet-data; re-vendor both from the same release"
        )
