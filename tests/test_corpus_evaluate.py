"""Corpus evaluate operation and the ``corpus`` CLI command surface."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from legolization.cli import main
from legolization.compare import Candidate, CandidateMetrics
from legolization.corpus import collect
from legolization.corpus import evaluate as evaluate_mod
from legolization.corpus import storage as corpus_storage
from legolization.corpus.manifest import CorpusModel
from legolization.eval_artifacts import SourceIdentity

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route corpus storage into tmp_path with no legacy tree to migrate."""
    root = tmp_path / "store"
    monkeypatch.setenv("LEGOLIZATION_DATA_HOME", str(root))
    monkeypatch.setattr(corpus_storage, "legacy_data_dir", lambda: None)
    return root


def _metrics() -> CandidateMetrics:
    return CandidateMetrics(
        buildable=True,
        stable=True,
        component_count=1,
        floating_count=0,
        objective_total=1.0,
        maximin_feasible=True,
        maximin_capacity=1.0,
        max_score=0.1,
        min_capacity=1.0,
        brick_count=10,
        mass_g=20.0,
        step_count=2,
        cost=0.5,
        aesthetics=0.0,
        colour_error=0.0,
        perpendicularity=0.0,
        symmetry=0.0,
    )


def _fake_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace placement and identity with fast deterministic fakes."""
    monkeypatch.setattr(
        collect,
        "source_identity",
        lambda _repo: SourceIdentity(
            git_sha="a" * 40,
            source_hash="b" * 64,
            dirty=True,
        ),
    )

    def run_all(*_args: object, **kwargs: object) -> list[Candidate]:
        skipped = set(cast("set[tuple[str, int]]", kwargs["skip"]))
        callback = cast("Callable[[Candidate], None]", kwargs["on_complete"])
        candidates = []
        for strategy in cast("tuple[str, ...]", kwargs["names"]):
            for seed in cast("tuple[int, ...]", kwargs["seeds"]):
                if (strategy, seed) in skipped:
                    continue
                candidate = Candidate(strategy, 0.1, metrics=_metrics(), seed=seed)
                callback(candidate)
                candidates.append(candidate)
        return candidates

    monkeypatch.setattr(collect, "run_all", run_all)


def _models(storage_root: Path) -> list[CorpusModel]:
    """One synthetic model plus one absent and one hash-invalid mesh."""
    (storage_root / "corpus" / "meshes").mkdir(parents=True)
    (storage_root / "corpus" / "meshes" / "tampered.obj").write_bytes(b"wrong bytes")
    return [
        CorpusModel(
            name="cantilever",
            kind="synthetic",
            path=Path("synthetic/cantilever.npy"),
            generator="cantilever",
            traits=("fast",),
        ),
        CorpusModel(
            name="ghost",
            kind="mesh",
            path=Path("meshes/ghost.obj"),
            source_url="https://example.com/ghost.obj",
            sha256="0" * 64,
        ),
        CorpusModel(
            name="tampered",
            kind="mesh",
            path=Path("meshes/tampered.obj"),
            source_url="https://example.com/tampered.obj",
            sha256="1" * 64,
        ),
    ]


def test_unavailable_meshes_are_skipped_not_failed(
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models(storage_root)
    monkeypatch.setattr(evaluate_mod, "load_manifest", lambda: models)
    _fake_collect(monkeypatch)
    forwarded: dict[str, object] = {}
    real_assemble = evaluate_mod.assemble_collection

    def spying_assemble(collection: Path, **kwargs: object) -> object:
        forwarded.update(kwargs)
        assert set(kwargs) == {"out"}  # evaluation may only scope the output root
        return real_assemble(collection, out=cast("Path", kwargs["out"]))

    monkeypatch.setattr(evaluate_mod, "assemble_collection", spying_assemble)

    result = evaluate_mod.evaluate(
        strategies="greedy",
        jobs=1,
        out=tmp_path / "runs",
    )

    assert result.exit_code == 0
    reasons = {skip.name: skip.reason for skip in result.skipped}
    assert reasons["ghost"] == "not downloaded"
    assert "sha256 mismatch" in reasons["tampered"]
    # Synthetic input files regenerate on demand.
    assert result.generated == ("cantilever",)
    assert (storage_root / "corpus" / "synthetic" / "cantilever.npy").exists()
    # Only the synthetic kind actually ran; nothing was recorded failed.
    assert [run.kind for run in result.runs] == ["synthetic"]
    run = result.runs[0]
    assert run.collection_complete
    assert run.failures == ()
    assert run.scorecard_path is not None
    assert run.scorecard_path.exists()
    assert result.failures == ()
    # Evaluation must never forward a baseline mutation to assembly.
    assert "write_baseline" not in forwarded
    assert "baseline" not in forwarded


def test_synthetic_inputs_generate_only_when_missing(
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [model for model in _models(storage_root) if model.kind == "synthetic"]
    monkeypatch.setattr(evaluate_mod, "load_manifest", lambda: models)
    _fake_collect(monkeypatch)
    out = tmp_path / "runs"

    first = evaluate_mod.evaluate(strategies="greedy", jobs=1, out=out)
    assert first.generated == ("cantilever",)
    second = evaluate_mod.evaluate(strategies="greedy", jobs=1, out=out)
    assert second.generated == ()
    assert second.exit_code == 0


def test_mesh_only_evaluation_with_nothing_available_is_complete(
    storage_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _models(storage_root)
    monkeypatch.setattr(evaluate_mod, "load_manifest", lambda: models)
    _fake_collect(monkeypatch)

    result = evaluate_mod.evaluate(
        kind="mesh",
        strategies="greedy",
        jobs=1,
        out=tmp_path / "runs",
    )
    assert result.runs == ()
    assert result.generated == ()  # synthetic kind was out of scope
    assert {skip.name for skip in result.skipped} == {"ghost", "tampered"}
    assert result.exit_code == 0


def test_evaluate_has_no_write_baseline_surface() -> None:
    parameters = inspect.signature(evaluate_mod.evaluate).parameters
    assert "write_baseline" not in parameters
    assert "baseline" not in parameters


def test_cli_evaluate_rejects_write_baseline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["corpus", "evaluate", "--write-baseline"]) == 1
    assert "unrecognized arguments" in capsys.readouterr().err


def test_cli_corpus_list_json_single_envelope(
    storage_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del storage_root
    assert main(["corpus", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "legolization.result/v1"
    assert payload["command"] == "corpus list"
    assert payload["status"] == "complete"
    names = {row["name"] for row in payload["data"]["models"]}
    assert {"cantilever", "spot"} <= names


def test_cli_corpus_generate_writes_artifact(
    storage_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del storage_root
    assert main(["corpus", "generate", "--models", "cantilever", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["models"][0]["status"] == "generated"
    artifact = Path(payload["artifacts"][0]["path"])
    assert artifact.exists()


def test_cli_collect_then_assemble_round_trip(
    storage_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del storage_root
    out = tmp_path / "runs"
    assert (
        main(
            [
                "corpus",
                "collect",
                "--models",
                "cantilever,letter-t",
                "--strategies",
                "bond",
                "--jobs",
                "1",
                "--out",
                str(out),
                "--json",
            ]
        )
        == 0
    )
    collect_payload = json.loads(capsys.readouterr().out)
    assert collect_payload["command"] == "corpus collect"
    assert collect_payload["status"] == "complete"
    collection = Path(collect_payload["data"]["collection"])
    assert collection.name == "collection.json"
    assert collection.exists()

    assert main(["corpus", "assemble", "--runs", str(out), "--out", str(out)]) == 0
    capsys.readouterr()
    assert (
        main(["corpus", "assemble", "--runs", str(out), "--out", str(out), "--json"])
        == 0
    )
    assemble_payload = json.loads(capsys.readouterr().out)
    assert assemble_payload["command"] == "corpus assemble"
    assert assemble_payload["status"] == "complete"
    scorecard = Path(cast("str", assemble_payload["data"]["scorecard"]))
    assert scorecard.exists()
    rows = {row["model"]: row for row in assemble_payload["data"]["models"]}
    assert rows["cantilever"]["expectation_ok"] is True
    assert rows["letter-t"]["expectation_ok"] is True
    assert assemble_payload["data"]["baseline_written"] is False

    # --write-baseline keeps the canonical-scope gate: a bond-only sweep
    # must never become the committed baseline.
    baseline = tmp_path / "baseline.json"
    assert (
        main(
            [
                "corpus",
                "assemble",
                "--runs",
                str(out),
                "--out",
                str(out),
                "--baseline",
                str(baseline),
                "--write-baseline",
                "--json",
            ]
        )
        == 1
    )
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["error"]["type"] == "CorpusAssemblyError"
    assert "baseline assembly requires" in refusal["error"]["message"]
    assert not baseline.exists()
