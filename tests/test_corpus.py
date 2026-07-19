"""Corpus manifest and synthetic-generator tests."""

import hashlib
import importlib.util
import io
import sys
import urllib.error
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from legolization.grid import EMPTY, VoxelGrid

_SCRIPT = Path(__file__).parent.parent / "scripts" / "corpus.py"


def _load_corpus() -> ModuleType:
    spec = importlib.util.spec_from_file_location("corpus_script", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # slots=True dataclasses resolve their module via sys.modules at exec time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def corpus() -> ModuleType:
    return _load_corpus()


def test_manifest_parses_and_matches_registry(corpus: ModuleType) -> None:
    models = corpus.load_manifest()
    assert len(models) >= 14
    for model in models:
        assert model.kind in ("mesh", "synthetic")
        match model.kind:
            case "synthetic":
                assert model.generator in corpus.GENERATORS, model.name
            case "mesh":
                assert model.source_url is not None, model.name
                assert model.source_url.startswith("https://"), model.name
                assert model.sha256 is not None, model.name
                assert len(model.sha256) == 64, model.name
                assert set(model.sha256) <= set("0123456789abcdef"), model.name
                assert model.up is not None, model.name


def test_manifest_names_unique(corpus: ModuleType) -> None:
    models = corpus.load_manifest()
    names = [model.name for model in models]
    assert len(names) == len(set(names))


def test_generators_deterministic_and_nonempty(corpus: ModuleType) -> None:
    for name, generator in corpus.GENERATORS.items():
        first = generator()
        second = generator()
        assert np.array_equal(first, second), name
        assert first.dtype == np.int16, name
        assert (first != EMPTY).any(), name
        # Every generator output must load as a valid grid.
        grid = VoxelGrid.from_array(first, plates_per_voxel=3)
        assert grid.filled_count > 0, name


def test_mushroom_has_overhang(corpus: ModuleType) -> None:
    codes = corpus.mushroom()
    filled = codes != EMPTY
    # Overhang: some filled voxel above an empty column bottom.
    above = filled[:, :, 1:] & ~filled[:, :, :-1]
    assert above.any()


def test_bridge_splits_without_deck(corpus: ModuleType) -> None:
    codes = corpus.two_towers_bridge()
    filled = codes != EMPTY
    deck_layers = 2
    from scipy import ndimage

    structure = ndimage.generate_binary_structure(rank=3, connectivity=1)
    _labels, whole = ndimage.label(filled, structure=structure)
    assert whole == 1
    _labels, without_deck = ndimage.label(
        filled[:, :, :-deck_layers], structure=structure
    )
    assert without_deck == 2


def test_sparse_pillars_are_disconnected(corpus: ModuleType) -> None:
    from scipy import ndimage

    codes = corpus.sparse_pillars()
    structure = ndimage.generate_binary_structure(rank=3, connectivity=1)
    _labels, components = ndimage.label(codes != EMPTY, structure=structure)
    assert components == 4


def test_select_rejects_unknown_names(corpus: ModuleType) -> None:
    models = corpus.load_manifest()
    with pytest.raises(SystemExit, match="unknown corpus model"):
        corpus.select_models(models, "no-such-model")


def test_generate_writes_files(
    corpus: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(corpus, "_REPO", tmp_path)
    models = [
        corpus.CorpusModel(
            name="cantilever",
            kind="synthetic",
            path=Path("synthetic/cantilever.npy"),
            generator="cantilever",
        )
    ]
    assert corpus.generate(models) == 0
    saved = np.load(tmp_path / "synthetic" / "cantilever.npy")
    assert np.array_equal(saved, corpus.cantilever())


def test_verify_flags_stale_synthetic(
    corpus: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(corpus, "_REPO", tmp_path)
    model = corpus.CorpusModel(
        name="cantilever",
        kind="synthetic",
        path=Path("synthetic/cantilever.npy"),
        generator="cantilever",
    )
    (tmp_path / "synthetic").mkdir(parents=True)
    np.save(tmp_path / "synthetic" / "cantilever.npy", corpus.letter_t())
    assert corpus.verify([model]) == 1
    assert "STALE" in capsys.readouterr().out


def test_verify_flags_missing(
    corpus: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(corpus, "_REPO", tmp_path)
    model = corpus.CorpusModel(
        name="cantilever",
        kind="synthetic",
        path=Path("synthetic/cantilever.npy"),
        generator="cantilever",
    )
    assert corpus.verify([model]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_verify_reports_corrupt_synthetic_and_continues(
    corpus: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(corpus, "_REPO", tmp_path)
    synthetic = tmp_path / "synthetic"
    synthetic.mkdir()
    (synthetic / "corrupt.npy").write_bytes(b"not a numpy file")
    np.save(synthetic / "valid.npy", corpus.letter_t())
    models = [
        corpus.CorpusModel(
            name="corrupt",
            kind="synthetic",
            path=Path("synthetic/corrupt.npy"),
            generator="cantilever",
        ),
        corpus.CorpusModel(
            name="valid",
            kind="synthetic",
            path=Path("synthetic/valid.npy"),
            generator="letter_t",
        ),
    ]

    assert corpus.verify(models) == 1
    output = capsys.readouterr().out
    assert "CORRUPT corrupt" in output
    assert "ok valid" in output


def test_download_isolates_failures_and_replaces_atomically(
    corpus: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(corpus, "_REPO", tmp_path)
    good_data = b"verified mesh"
    good_hash = hashlib.sha256(good_data).hexdigest()
    old_path = tmp_path / "meshes" / "failed.stl"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"existing artifact")

    def fake_urlopen(url: str, *, timeout: float) -> io.BytesIO:
        assert timeout == 30.0
        if url.endswith("failed.stl"):
            raise urllib.error.URLError("offline")
        return io.BytesIO(good_data)

    monkeypatch.setattr(corpus.urllib.request, "urlopen", fake_urlopen)
    models = [
        corpus.CorpusModel(
            name="failed",
            kind="mesh",
            path=Path("meshes/failed.stl"),
            source_url="https://example.com/failed.stl",
            sha256="0" * 64,
        ),
        corpus.CorpusModel(
            name="good",
            kind="mesh",
            path=Path("meshes/good.stl"),
            source_url="https://example.com/good.stl",
            sha256=good_hash,
        ),
    ]

    assert corpus.download(models) == 1
    assert old_path.read_bytes() == b"existing artifact"
    assert (tmp_path / "meshes" / "good.stl").read_bytes() == good_data
    output = capsys.readouterr().out
    assert "download failed" in output
    assert "ok meshes/good.stl" in output


def test_largest_component_only_field(corpus: ModuleType) -> None:
    models = {model.name: model for model in corpus.load_manifest()}
    assert models["homer"].largest_component_only is True
    assert models["spot"].largest_component_only is False
    assert models["cantilever"].largest_component_only is False
