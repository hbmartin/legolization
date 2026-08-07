"""Portable bundle foundation: identity, naming, resume, drift, interrupt."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Never

import numpy as np
import pytest

from legolization.bundle import orchestrator
from legolization.bundle.identity import bundle_identity, result_affecting_config
from legolization.bundle.paths import (
    default_bundle_dir,
    numbered_file,
    numbered_sibling,
    resolve_output_dir,
)
from legolization.bundle.record import read_identity_key, read_record
from legolization.configuration import (
    CacheConfig,
    OutputConfig,
    ProjectConfig,
    merge_overrides,
)
from legolization.errors import ConfigurationError


def _write_box(path: Path, *, islands: int = 1) -> Path:
    codes = np.full((4, 3, 2), -1, dtype=np.int16)
    codes[0:2, :, :] = 4
    if islands == 2:
        codes[3:4, :, :] = 4
    else:
        codes[2:4, :, :] = 4
    np.save(path, codes)
    return path


@pytest.fixture
def box_npy(tmp_path) -> Path:
    return _write_box(tmp_path / "box.npy")


@pytest.fixture
def config() -> ProjectConfig:
    return merge_overrides(ProjectConfig(), {"placement.strategy": "bond"})


def _request(
    box_npy: Path,
    config: ProjectConfig,
    **kwargs,
) -> orchestrator.BundleRequest:
    return orchestrator.BundleRequest(input_path=box_npy, config=config, **kwargs)


def test_identity_is_stable_and_ignores_non_result_config(box_npy, config, tmp_path):
    first = bundle_identity(box_npy, config)
    assert first == bundle_identity(box_npy, config)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_changed = replace(config, cache=CacheConfig(path=cache_dir))
    output_changed = replace(config, output=OutputConfig(manifest=False))
    assert bundle_identity(box_npy, cache_changed) == first
    assert bundle_identity(box_npy, output_changed) == first
    values = result_affecting_config(config)
    assert "cache" not in values
    assert "output" not in values


def test_identity_flips_on_config_input_and_invocation(box_npy, config, tmp_path):
    base = bundle_identity(box_npy, config)
    seeded = merge_overrides(config, {"placement.seed": 7})
    assert bundle_identity(box_npy, seeded) != base
    other_input = _write_box(tmp_path / "other.npy", islands=2)
    assert bundle_identity(other_input, config) != base
    assert bundle_identity(box_npy, config, invocation={"retile": True}) != base


def test_sibling_naming_and_numbering(tmp_path):
    model = tmp_path / "spot.obj"
    base = default_bundle_dir(model, "legolization")
    assert base == tmp_path / "spot-legolization"
    assert numbered_sibling(base) == base
    base.mkdir()
    assert numbered_sibling(base) == tmp_path / "spot-legolization-2"
    (tmp_path / "spot-legolization-2").mkdir()
    assert numbered_sibling(base) == tmp_path / "spot-legolization-3"


def test_numbered_file_variants(tmp_path):
    image = tmp_path / "spot.iso.png"
    assert numbered_file(image) == image
    image.write_bytes(b"png")
    assert numbered_file(image) == tmp_path / "spot.iso.2.png"


def test_run_bundle_writes_portable_layout(box_npy, config):
    envelope = orchestrator.run_bundle(_request(box_npy, config))
    assert envelope.exit_code == 0
    bundle_dir = box_npy.parent / "box-legolization"
    assert (bundle_dir / "bundle.json").is_file()
    assert (bundle_dir / "model" / "model.mpd").is_file()
    assert (bundle_dir / "model" / "model.ldr").is_file()
    assert (bundle_dir / "bom" / "bom.json").is_file()
    record = read_record(bundle_dir)
    assert record is not None
    assert record.status == "complete"
    assert record.verdicts["buildable"] is True
    assert {entry.kind for entry in record.artifacts} == {"model", "bom"}
    assert all(entry.sha256 is not None for entry in record.artifacts)
    source = record.source
    assert source["filename"] == "box.npy"
    assert not Path(str(source.get("relative_path", ""))).is_absolute()
    payload = json.loads((bundle_dir / "bundle.json").read_text())
    assert payload["schema"] == "legolization.bundle/v1"


def test_run_bundle_unbuildable_islands_exit_two(tmp_path, config):
    islands = _write_box(tmp_path / "islands.npy", islands=2)
    envelope = orchestrator.run_bundle(
        orchestrator.BundleRequest(input_path=islands, config=config)
    )
    assert envelope.exit_code == 2
    record = read_record(tmp_path / "islands-legolization")
    assert record is not None
    assert record.status == "unbuildable"
    assert record.verdicts["buildable"] is False


def test_resume_reuses_identity_matched_bundle(box_npy, config, monkeypatch):
    first = orchestrator.run_bundle(_request(box_npy, config))
    assert first.exit_code == 0

    def _fail_write(*args, **kwargs) -> Never:
        msg = "model stage must not rerun on a clean resume"
        raise AssertionError(msg)

    monkeypatch.setattr("legolization.ldraw_out.write_model", _fail_write)
    second = orchestrator.run_bundle(_request(box_npy, config))
    assert second.exit_code == 0
    assert second.data is not None
    assert second.data["resume"] is True
    assert second.data["regenerated_stages"] == []


def test_fresh_forces_numeric_sibling(box_npy, config):
    orchestrator.run_bundle(_request(box_npy, config))
    envelope = orchestrator.run_bundle(_request(box_npy, config, fresh=True))
    assert envelope.exit_code == 0
    assert envelope.data is not None
    assert envelope.data["resume"] is False
    assert str(envelope.data["bundle_dir"]).endswith("box-legolization-2")


def test_identity_mismatch_scans_to_new_sibling(box_npy, config):
    orchestrator.run_bundle(_request(box_npy, config))
    reseeded = merge_overrides(config, {"placement.seed": 5})
    envelope = orchestrator.run_bundle(_request(box_npy, reseeded))
    assert envelope.data is not None
    assert str(envelope.data["bundle_dir"]).endswith("box-legolization-2")
    resumed = orchestrator.run_bundle(_request(box_npy, config))
    assert resumed.data is not None
    assert resumed.data["resume"] is True
    assert str(resumed.data["bundle_dir"]).endswith("box-legolization")


def test_explicit_output_mismatch_gets_numbered_sibling(box_npy, config, tmp_path):
    explicit = tmp_path / "chosen"
    explicit.mkdir()
    (explicit / "junk.txt").write_text("not a bundle")
    envelope = orchestrator.run_bundle(_request(box_npy, config, output_dir=explicit))
    assert envelope.data is not None
    assert str(envelope.data["bundle_dir"]).endswith("chosen-2")
    assert (explicit / "junk.txt").is_file()


def test_interrupt_marks_resumable_state_and_resumes(box_npy, config, monkeypatch):
    original = orchestrator._stage_model  # noqa: SLF001

    def _interrupt(context) -> Never:
        raise KeyboardInterrupt

    monkeypatch.setattr(orchestrator, "_stage_model", _interrupt)
    interrupted = orchestrator.run_bundle(_request(box_npy, config))
    assert interrupted.exit_code == 130
    bundle_dir = box_npy.parent / "box-legolization"
    record = read_record(bundle_dir)
    assert record is not None
    assert record.status == "interrupted"
    assert record.stages["model"].status == "interrupted"
    identity_before = record.identity.key()

    monkeypatch.setattr(orchestrator, "_stage_model", original)
    resumed = orchestrator.run_bundle(_request(box_npy, config))
    assert resumed.exit_code == 0
    record = read_record(bundle_dir)
    assert record is not None
    assert record.status == "complete"
    assert record.identity.key() == identity_before


def test_artifact_drift_regenerates_only_the_affected_stage(box_npy, config):
    orchestrator.run_bundle(_request(box_npy, config))
    bundle_dir = box_npy.parent / "box-legolization"
    model_path = bundle_dir / "model" / "model.ldr"
    model_path.write_text(model_path.read_text() + "\n0 drift\n")
    envelope = orchestrator.run_bundle(_request(box_npy, config))
    assert envelope.exit_code == 0
    assert envelope.data is not None
    assert envelope.data["regenerated_stages"] == ["model"]
    record = read_record(bundle_dir)
    assert record is not None
    assert record.stages["model"].detail.get("regenerated") is True
    from legolization.eval_artifacts import input_sha256

    recorded = {entry.path: entry.sha256 for entry in record.artifacts}
    assert input_sha256(model_path) == recorded["model/model.ldr"]


def test_read_identity_key_rejects_corruption(box_npy, config):
    orchestrator.run_bundle(_request(box_npy, config))
    bundle_dir = box_npy.parent / "box-legolization"
    assert read_identity_key(bundle_dir) is not None
    (bundle_dir / "bundle.json").write_text("{not json")
    assert read_identity_key(bundle_dir) is None
    decision = resolve_output_dir(
        box_npy,
        None,
        flavor="legolization",
        identity_key="anything",
        fresh=False,
        read_key=read_identity_key,
    )
    assert decision.resume is False


def test_cancel_pending_without_bundle_is_an_error(box_npy, config):
    with pytest.raises(ConfigurationError):
        orchestrator.run_bundle(_request(box_npy, config, cancel_pending=True))


GOLDEN_LDR = Path(__file__).parent / "data" / "golden" / "simple.ldr"


@pytest.fixture
def golden_ldr(tmp_path) -> Path:
    target = tmp_path / "simple.ldr"
    target.write_text(GOLDEN_LDR.read_text())
    return target


def test_ldraw_preserve_skips_generation(golden_ldr, config):
    envelope = orchestrator.run_bundle(
        orchestrator.BundleRequest(input_path=golden_ldr, config=config)
    )
    bundle_dir = golden_ldr.parent / "simple-instructions"
    record = read_record(bundle_dir)
    assert record is not None
    assert record.stages["ingest"].detail["mode"] == "preserve"
    assert record.stages["ingest"].detail["shape_authority"] == "imported-assembly"
    assert record.stages["generate"].status == "skipped"
    assert record.verdicts["winner"]["strategy"] == "imported"
    assert (bundle_dir / "model" / "model.mpd").is_file()
    assert (bundle_dir / "bom" / "bom.json").is_file()
    assert envelope.exit_code in {0, 2}
    resumed = orchestrator.run_bundle(
        orchestrator.BundleRequest(input_path=golden_ldr, config=config)
    )
    assert resumed.data is not None
    assert resumed.data["resume"] is True
    assert resumed.data["regenerated_stages"] == []


def test_ldraw_retile_regenerates_from_occupancy(golden_ldr, config):
    envelope = orchestrator.run_bundle(
        orchestrator.BundleRequest(input_path=golden_ldr, config=config, retile=True)
    )
    bundle_dir = golden_ldr.parent / "simple-optimized"
    record = read_record(bundle_dir)
    assert record is not None
    assert record.stages["ingest"].detail["mode"] == "retile"
    assert record.stages["ingest"].detail["shape_authority"] == "imported-assembly"
    assert record.stages["generate"].status == "complete"
    assert record.verdicts["winner"]["strategy"] != "imported"
    assert envelope.exit_code in {0, 2}
    preserve_identity = bundle_identity(golden_ldr, config)
    retile_identity = bundle_identity(
        golden_ldr,
        config,
        invocation={"retile": True},
    )
    assert preserve_identity != retile_identity


def test_retile_rejected_for_native_input(box_npy, config):
    with pytest.raises(ConfigurationError, match="retile"):
        orchestrator.run_bundle(_request(box_npy, config, retile=True))


def test_bundle_cli_emits_single_envelope(box_npy, capsys):
    from legolization.cli import main

    code = main(
        [
            "bundle",
            str(box_npy),
            "--quality",
            "direct",
            "--set",
            "placement.strategy=bond",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "legolization.result/v1"
    assert payload["command"] == "bundle"
    assert payload["status"] == "complete"
    assert payload["data"]["stages"]["model"] == "complete"
