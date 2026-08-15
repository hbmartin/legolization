"""Tests for the external-dataset adapters and the fetch registry.

These pin the *conventions* the adapters encode, using small inline fixtures
rather than the bulk payloads (which are gitignored and multi-gigabyte). The
conventions are the part that is easy to get silently wrong: a flipped score
sense or a transposed footprint produces plausible numbers and wrong answers.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from legolization.datasets import stabletext2brick as s2b
from legolization.stability import analyze

if TYPE_CHECKING:
    from types import ModuleType

_REPO = Path(__file__).parent.parent
_REGISTRY = _REPO / "scripts" / "datasets.toml"

# Every path a dataset fetch or an analysis writes to. None may ever be tracked:
# the payloads run to gigabytes, several carry licences that forbid
# redistribution, and `datasets` is a symlink to an external volume on at least
# one machine, so committing it would also commit a path meaningless elsewhere.
_MUST_STAY_UNTRACKED = (
    "datasets",  # scripts/fetch_datasets.py, scripts/fetch_omr.py
    "eval/datasets",  # every analysis report
    "eval/stablelego",  # scripts/stablelego_sweep.py
    "eval/preferences",  # pair renders and review pages; the log is tracked
)

# The release's complete brick library, from the BrickGPT paper's appendix:
# "Allowed brick dimensions are 2x4, 4x2, 2x6, 6x2, 1x2, 2x1, 1x4, 4x1, 1x6,
# 6x1, 1x8, 8x1, 1x1, 2x2. All bricks are 1 unit tall."
_RELEASE_LIBRARY = (
    (1, 1), (1, 2), (2, 1), (1, 4), (4, 1), (1, 6), (6, 1),
    (1, 8), (8, 1), (2, 2), (2, 4), (4, 2), (2, 6), (6, 2),
)  # fmt: skip


def _structure(bricks: str, scores: np.ndarray | None = None) -> s2b.Structure:
    grid = s2b.GRID
    return s2b.Structure(
        structure_id="test",
        object_id="obj",
        category_id="cat",
        captions=("a caption",),
        bricks=s2b.parse_bricks(bricks),
        scores=np.ones((grid, grid, grid)) if scores is None else scores,
    )


def test_parses_the_release_line_format():
    rows = s2b.parse_bricks("1x1 (16,15,0)\n2x4 (16,9,1)\n")
    assert rows == (
        s2b.BrickRow(x_extent=1, y_extent=1, x=16, y=15, z=0),
        s2b.BrickRow(x_extent=2, y_extent=4, x=16, y=9, z=1),
    )


def test_h_is_the_x_extent_and_w_the_y_extent():
    # Verified against the real release: a brick occupies x..x+h-1 by y..y+w-1.
    # A transposition here would be invisible in any aggregate number.
    (row,) = s2b.parse_bricks("2x4 (3,5,0)")
    assert set(row.cells) == {(x, y, 0) for x in (3, 4) for y in range(5, 9)}


def test_rejects_an_undecodable_line_rather_than_skipping_it():
    with pytest.raises(s2b.BrickParseError, match="cannot parse brick"):
        s2b.parse_bricks("1x1 (1,2,0)\nnot a brick\n")


def test_rejects_an_empty_structure():
    with pytest.raises(s2b.BrickParseError, match="no bricks"):
        s2b.parse_bricks("\n \n")


def test_layout_cells_match_the_parsed_bricks():
    structure = _structure("2x4 (0,0,0)\n1x2 (0,0,1)\n4x2 (2,4,0)")
    layout = s2b.layout_from_bricks(structure.bricks)
    placed = {(x, y, z // 3) for brick in layout for x, y, z in layout.cells_of(brick)}
    assert placed == {cell for brick in structure.bricks for cell in brick.cells}


def test_z_counts_brick_heights_so_layers_step_by_three():
    layout = s2b.layout_from_bricks(s2b.parse_bricks("2x2 (0,0,0)\n2x2 (0,0,2)"))
    assert sorted(brick.layer for brick in layout) == [0, 6]


def test_the_whole_release_brick_library_resolves():
    library = "\n".join(
        f"{h}x{w} ({10 * index},0,0)" for index, (h, w) in enumerate(_RELEASE_LIBRARY)
    )
    layout = s2b.layout_from_bricks(s2b.parse_bricks(library))
    assert sum(1 for _ in layout) == len(_RELEASE_LIBRARY)


def test_a_footprint_outside_the_catalog_is_an_error():
    with pytest.raises(ValueError, match="no catalog part for 5x7"):
        s2b.layout_from_bricks(s2b.parse_bricks("5x7 (0,0,0)"))


def _row(bricks: str, scores: object | None = None) -> dict[str, object]:
    grid = s2b.GRID
    return {
        "structure_id": "test",
        "object_id": "obj",
        "category_id": "cat",
        "captions": ["a caption"],
        "bricks": bricks,
        "stability_scores": np.ones((grid, grid, grid)) if scores is None else scores,
    }


def test_from_row_accepts_a_valid_edge_hugging_row() -> None:
    structure = s2b.Structure.from_row(_row("1x1 (19,19,19)"))
    assert structure.bricks[0].z == 19


def test_from_row_rejects_a_mis_shaped_score_array() -> None:
    grid = s2b.GRID
    with pytest.raises(s2b.BrickParseError, match="shape"):
        s2b.Structure.from_row(_row("1x1 (0,0,0)", scores=np.ones((grid, grid))))


def test_from_row_rejects_a_brick_that_leaves_the_grid() -> None:
    # `occupancy` applies coordinates as NumPy slices, so an oversized extent
    # would silently truncate instead of failing.
    with pytest.raises(s2b.BrickParseError, match="leaves"):
        s2b.Structure.from_row(_row("2x4 (19,0,0)"))


def test_from_row_rejects_a_zero_extent_brick() -> None:
    with pytest.raises(s2b.BrickParseError, match="leaves"):
        s2b.Structure.from_row(_row("0x2 (0,0,0)"))


def test_margin_masks_to_occupied_cells():
    # A naive min() over the whole array is meaningless: unoccupied voxels are
    # padded with 1.0, so it would read 1.0 for any sparse structure.
    structure = _structure("2x2 (0,0,0)")
    structure.scores[0, 0, 0] = 0.4
    structure.scores[10, 10, 10] = 0.01  # unoccupied: must be ignored
    assert s2b.release_margin(structure) == pytest.approx(0.4)


def test_margin_is_higher_is_better_with_a_greater_than_zero_rule():
    # The release's score is a MARGIN; ours and StableLego's are STRESS.
    # Reading this backwards would invert every verdict while still producing
    # numbers in [0, 1], so it is pinned explicitly.
    structure = _structure("1x1 (0,0,0)")
    structure.scores[0, 0, 0] = 1e-6
    stands, margin = s2b.release_verdict(structure)
    assert stands
    assert margin == pytest.approx(1e-6)

    structure.scores[0, 0, 0] = 0.0
    stands, _ = s2b.release_verdict(structure)
    assert not stands


def test_unoccupied_voxels_are_padded_with_one_in_the_release():
    # Measured over the real test shard: all 355,660 unoccupied voxels across
    # 50 structures are exactly 1.0.
    structure = _structure("1x1 (0,0,0)")
    assert float(structure.scores[~structure.occupancy].min()) == 1.0


def test_a_solid_release_stack_is_stable_under_our_solver():
    layout = s2b.layout_from_bricks(
        s2b.parse_bricks("\n".join(f"2x4 (0,0,{z})" for z in range(4)))
    )
    result = analyze(layout)
    assert result.stable
    # Near-zero stress for us corresponds to a near-1.0 margin for them: the
    # same physical fact on an inverted scale.
    assert result.max_score < 0.5


def test_registry_parses_and_every_entry_is_licensed():
    entries = tomllib.loads(_REGISTRY.read_text(encoding="utf-8"))["dataset"]
    assert entries
    for entry in entries:
        assert entry["license"], f"{entry['name']} has no licence"
        assert entry["source"].startswith("https://")


def test_every_registry_payload_url_is_https():
    # fetch_datasets.py refuses non-https at download time; this keeps the
    # registry itself from accumulating URLs that would only fail later.
    entries = tomllib.loads(_REGISTRY.read_text(encoding="utf-8"))["dataset"]
    for entry in entries:
        for item in entry.get("file", []):
            assert item["url"].startswith("https://"), item["name"]


def test_unavailable_sources_explain_themselves():
    entries = tomllib.loads(_REGISTRY.read_text(encoding="utf-8"))["dataset"]
    for entry in entries:
        if not entry.get("available", True):
            assert "UNAVAILABLE" in entry["notes"]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


# A source archive has no .git, and a minimal environment has no git binary;
# either way the integrity guards below have nothing to check and must skip
# rather than fail on the missing tool.
_REQUIRES_GIT = pytest.mark.skipif(
    shutil.which("git") is None or not (_REPO / ".git").exists(),
    reason="requires git and a git checkout",
)


@_REQUIRES_GIT
@pytest.mark.parametrize("path", _MUST_STAY_UNTRACKED)
def test_dataset_payloads_are_not_tracked(path: str) -> None:
    # The guarantee that matters. An ignore rule can be edited away or bypassed
    # with `git add -f`; this fails loudly if a payload ever lands in the index.
    tracked = [line for line in _git("ls-files", "--", path).splitlines() if line]
    assert not tracked, f"{path} must never be committed; tracked: {tracked[:5]}"


@_REQUIRES_GIT
@pytest.mark.parametrize("path", _MUST_STAY_UNTRACKED)
def test_dataset_paths_are_ignored_however_they_exist(path: str) -> None:
    # `datasets` is a SYMLINK on at least one machine, and a trailing-slash
    # pattern (`datasets/`) does not match a symlink - git treats it as a file.
    # `check-ignore --no-index` answers for the pattern itself, so this holds
    # whether the path is currently a directory, a symlink, or absent.
    assert _git("check-ignore", "--no-index", "-v", path).strip(), (
        f"{path} is not covered by .gitignore; a fetch would leave it stageable"
    )


def _load_omr_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "omr", _REPO / "scripts" / "fetch_omr.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["omr"] = module
    spec.loader.exec_module(module)
    return module


def test_omr_licence_is_read_from_the_file_header():
    omr = _load_omr_module()
    body = (
        b"0 Title\r\n"
        b"0 !LICENSE Redistributable under CCAL version 2.0 : see CAreadme.txt\r\n"
        b"1 4 0 0 0\r\n"
    )
    assert (
        omr.license_of(body)
        == "Redistributable under CCAL version 2.0 : see CAreadme.txt"
    )


def test_omr_licence_survives_a_run_together_meta_command():
    # Real case: 6386-1.mpd has no line break between the !LICENSE value and
    # the following 0 !HELP command. Capturing to end-of-line would fold a
    # copyright notice into the licence and split the attribution histogram.
    omr = _load_omr_module()
    body = (
        b"0 !LICENSE Redistributable under CCAL version 2.0 : see CAreadme.txt"
        b"0 !HELP Copyright (c) 2002-2017, Robert Paciorek\r\n"
    )
    assert (
        omr.license_of(body)
        == "Redistributable under CCAL version 2.0 : see CAreadme.txt"
    )


def test_omr_licence_is_empty_when_the_header_is_absent():
    omr = _load_omr_module()
    assert omr.license_of(b"0 Title\r\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\r\n") == ""


def test_omr_discover_rejects_path_bearing_model_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The captured name is joined onto the crawl directory as a filename; a
    # page offering a separator must not be able to write outside it.
    omr = _load_omr_module()
    html = (
        b"<h1>Set</h1>"
        b'<a href="https://library.ldraw.org/library/omr/../escape.mpd">a</a>'
        b'<a href="https://library.ldraw.org/library/omr/good-1.mpd">b</a>'
    )
    monkeypatch.setattr(omr, "_get", lambda _url: html)
    discovered = omr.discover(1)
    assert discovered is not None
    _, models = discovered
    assert [model.name for model in models] == ["good-1.mpd"]


@pytest.mark.parametrize(
    ("contents", "expected_downloads"),
    [
        (None, 1),
        (b"bad", 1),
        (b"expected", 0),
    ],
)
def test_omr_resume_skips_only_matching_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes | None,
    expected_downloads: int,
) -> None:
    omr = _load_omr_module()
    model = omr.OmrModel(
        set_id=1,
        set_title="Set",
        name="model.mpd",
        url="https://example.test/model.mpd",
    )
    recorded = omr.OmrModel(
        set_id=1,
        set_title="Set",
        name="model.mpd",
        url="https://example.test/model.mpd",
        bytes=len(b"expected"),
    )
    if contents is not None:
        target = tmp_path / "ldraw" / model.filename
        target.parent.mkdir(parents=True)
        target.write_bytes(contents)
    downloads: list[Path] = []

    def fake_download(candidate: object, *, dest: Path) -> object:
        downloads.append(dest)
        return candidate

    monkeypatch.setattr(omr, "download", fake_download)
    index = omr.Index(models={model.name: recorded})
    assert (
        omr._record_models(  # noqa: SLF001
            [model],
            index,
            index_only=False,
            dest=tmp_path,
            delay=0.0,
        )
        == []
    )
    assert len(downloads) == expected_downloads


def test_omr_crawl_does_not_mark_a_transient_failure_visited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A timeout recorded as visited would silently drop that set from every
    # future resumed crawl.
    omr = _load_omr_module()
    monkeypatch.setattr(omr, "discover", lambda _set_id: None)
    monkeypatch.setattr(omr, "_MAX_SET_ID", 3)
    index, failures = omr.crawl(
        dest=tmp_path, delay=0.0, limit=0, index_only=True, progress=False
    )
    assert index.visited == set()
    assert len(failures) == 3


def test_omr_crawl_marks_a_definitively_absent_set_visited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    omr = _load_omr_module()
    monkeypatch.setattr(omr, "discover", lambda _set_id: ("", []))
    monkeypatch.setattr(omr, "_MAX_SET_ID", 2)
    index, failures = omr.crawl(
        dest=tmp_path, delay=0.0, limit=0, index_only=True, progress=False
    )
    assert index.visited == {1, 2}
    assert failures == []
