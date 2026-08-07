"""Corpus manifest, storage, and synthetic-generator tests."""

import hashlib
import io
import urllib.error
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

import legolization
import legolization.corpus
from legolization.corpus import manifest as corpus_manifest
from legolization.corpus import ops as corpus_ops
from legolization.corpus import storage as corpus_storage
from legolization.grid import EMPTY, VoxelGrid


@pytest.fixture(scope="module")
def corpus() -> ModuleType:
    return legolization.corpus


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route corpus storage into tmp_path with no legacy tree to migrate."""
    monkeypatch.setenv("LEGOLIZATION_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(corpus_storage, "legacy_data_dir", lambda: None)
    return tmp_path


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
    with pytest.raises(ValueError, match="unknown corpus model"):
        corpus.select_models(models, "no-such-model")


def test_manifest_defaults_to_packaged_file() -> None:
    packaged = Path(legolization.__file__).parent / "data" / "corpus" / "manifest.toml"
    assert packaged == corpus_manifest.MANIFEST
    assert corpus_manifest.MANIFEST.is_file()


def test_storage_root_honours_data_home_override(
    storage_root: Path,
) -> None:
    assert corpus_storage.corpus_root() == storage_root
    resolved = corpus_storage.resolve_input(Path("synthetic/cantilever.npy"))
    assert resolved == storage_root / "corpus" / "synthetic" / "cantilever.npy"


def test_migrate_legacy_copies_once_and_never_deletes(tmp_path: Path) -> None:
    legacy = tmp_path / "data" / "corpus"
    (legacy / "meshes").mkdir(parents=True)
    (legacy / "synthetic").mkdir()
    (legacy / "meshes" / "spot.obj").write_bytes(b"mesh bytes")
    (legacy / "synthetic" / "cantilever.npy").write_bytes(b"grid bytes")
    root = tmp_path / "store"

    migrated = corpus_storage.migrate_legacy(legacy, root)
    assert migrated == ("meshes/spot.obj", "synthetic/cantilever.npy")
    assert (root / "corpus" / "meshes" / "spot.obj").read_bytes() == b"mesh bytes"
    assert (legacy / "meshes" / "spot.obj").exists()  # one-way copy, no delete

    # Existing targets are never overwritten.
    (root / "corpus" / "meshes" / "spot.obj").write_bytes(b"user edit")
    assert corpus_storage.migrate_legacy(legacy, root) == ()
    assert (root / "corpus" / "meshes" / "spot.obj").read_bytes() == b"user edit"
    assert corpus_storage.migrate_legacy(None, root) == ()


def test_resolve_input_migrates_legacy_lazily(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEGOLIZATION_DATA_HOME", str(tmp_path / "store"))
    legacy = tmp_path / "data" / "corpus"
    (legacy / "synthetic").mkdir(parents=True)
    (legacy / "synthetic" / "x.npy").write_bytes(b"payload")
    monkeypatch.setattr(corpus_storage, "legacy_data_dir", lambda: legacy)

    resolved = corpus_storage.resolve_input(Path("synthetic/x.npy"))
    assert resolved == tmp_path / "store" / "corpus" / "synthetic" / "x.npy"
    assert resolved.read_bytes() == b"payload"


def test_generate_writes_files(
    corpus: ModuleType,
    storage_root: Path,
) -> None:
    models = [
        corpus.CorpusModel(
            name="cantilever",
            kind="synthetic",
            path=Path("synthetic/cantilever.npy"),
            generator="cantilever",
        )
    ]
    reports = corpus.generate(models)
    assert corpus.exit_code(reports) == 0
    assert [report.status for report in reports] == ["generated"]
    saved = np.load(storage_root / "corpus" / "synthetic" / "cantilever.npy")
    assert np.array_equal(saved, corpus.cantilever())


def test_verify_flags_stale_synthetic(
    corpus: ModuleType,
    storage_root: Path,
) -> None:
    model = corpus.CorpusModel(
        name="cantilever",
        kind="synthetic",
        path=Path("synthetic/cantilever.npy"),
        generator="cantilever",
    )
    (storage_root / "corpus" / "synthetic").mkdir(parents=True)
    np.save(storage_root / "corpus" / "synthetic" / "cantilever.npy", corpus.letter_t())
    reports = corpus.verify([model])
    assert corpus.exit_code(reports) == 1
    assert any("STALE" in report.line for report in reports)


def test_verify_flags_missing(
    corpus: ModuleType,
    storage_root: Path,
) -> None:
    del storage_root
    model = corpus.CorpusModel(
        name="cantilever",
        kind="synthetic",
        path=Path("synthetic/cantilever.npy"),
        generator="cantilever",
    )
    reports = corpus.verify([model])
    assert corpus.exit_code(reports) == 1
    assert any("MISSING" in report.line for report in reports)


def test_verify_reports_corrupt_synthetic_and_continues(
    corpus: ModuleType,
    storage_root: Path,
) -> None:
    synthetic = storage_root / "corpus" / "synthetic"
    synthetic.mkdir(parents=True)
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

    reports = corpus.verify(models)
    assert corpus.exit_code(reports) == 1
    lines = [report.line for report in reports]
    assert any("CORRUPT corrupt" in line for line in lines)
    assert any("ok valid" in line for line in lines)


def test_download_isolates_failures_and_replaces_atomically(
    corpus: ModuleType,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_data = b"verified mesh"
    good_hash = hashlib.sha256(good_data).hexdigest()
    old_path = storage_root / "corpus" / "meshes" / "failed.stl"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"existing artifact")

    def fake_urlopen(url: str, *, timeout: float) -> io.BytesIO:
        assert timeout == 30.0
        if url.endswith("failed.stl"):
            raise urllib.error.URLError("offline")
        return io.BytesIO(good_data)

    monkeypatch.setattr(corpus_ops.urllib.request, "urlopen", fake_urlopen)
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

    reports = corpus.download(models)
    assert corpus.exit_code(reports) == 1
    assert old_path.read_bytes() == b"existing artifact"
    assert (storage_root / "corpus" / "meshes" / "good.stl").read_bytes() == good_data
    by_name = {report.name: report for report in reports}
    assert "download failed" in by_name["failed"].line
    assert by_name["good"].status == "downloaded"
    assert by_name["good"].line.endswith("meshes/good.stl")


def test_largest_component_only_field(corpus: ModuleType) -> None:
    models = {model.name: model for model in corpus.load_manifest()}
    assert models["homer"].largest_component_only is True
    assert models["spot"].largest_component_only is False
    assert models["cantilever"].largest_component_only is False


def test_torsion_bridge_dogleg_geometry(corpus: ModuleType) -> None:
    from scipy import ndimage

    codes = corpus.torsion_bridge()
    filled = codes != EMPTY
    structure = ndimage.generate_binary_structure(rank=3, connectivity=1)
    _labels, whole = ndimage.label(filled, structure=structure)
    assert whole == 1
    deck_layers = 2
    _labels, without_deck = ndimage.label(
        filled[:, :, :-deck_layers], structure=structure
    )
    assert without_deck == 2  # towers rejoin only through the deck
    # The deck is one stud wide along both runs: the eccentric-beam trait.
    deck = filled[:, :, -deck_layers:]
    assert deck[:, 0, :].all()  # the +x run at y=0
    assert deck[-1, :, :].all()  # the +y run at x=max
    assert not deck[:-1, 1:, :].any()  # nothing else


def test_torsion_bridge_yaw_row_preserves_the_score(corpus: ModuleType) -> None:
    # Exact physical contact points make this symmetric bridge invariant to
    # enabling the explicit yaw-equilibrium row; the historical coarse contact
    # pattern produced an artificial score increase here.
    from legolization.pipeline import PipelineConfig, run
    from legolization.stability.solver import SolverConfig, analyze

    grid = VoxelGrid.from_array(corpus.torsion_bridge(), plates_per_voxel=3)
    result = run(grid, PipelineConfig(strategy="kollsker", seed=0, hollow=False))
    base = analyze(result.layout, SolverConfig(torque_z=False))
    yaw = analyze(result.layout, SolverConfig(torque_z=True))
    assert yaw.stable == base.stable
    assert yaw.max_score == pytest.approx(base.max_score)


def test_press_tower_arms_overhang(corpus: ModuleType) -> None:
    codes = corpus.press_tower()
    filled = codes != EMPTY
    # Each arm row cantilevers past the column: filled above empty.
    above = filled[:, :, 1:] & ~filled[:, :, :-1]
    assert above.any()
    from scipy import ndimage

    structure = ndimage.generate_binary_structure(rank=3, connectivity=1)
    _labels, components = ndimage.label(filled, structure=structure)
    assert components == 1
