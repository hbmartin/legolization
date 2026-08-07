"""Multi-view model rendering: outcomes, suffixes, bundle inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legolization.errors import ConfigurationError
from legolization.instructions.render import Renderer, Runner
from legolization.model_render import (
    render_model_views,
    resolve_model,
    write_render_info,
)

GOLDEN = Path(__file__).parent / "data" / "golden" / "simple.ldr"

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # enough to be a non-empty file
)


@pytest.fixture
def model(tmp_path) -> Path:
    target = tmp_path / "simple.ldr"
    target.write_text(GOLDEN.read_text())
    return target


def _runner(succeed_views: set[str]) -> Runner:
    """Fake runner writing PNG bytes only for the selected views."""

    def _run(cmd: list[str], timeout_s: float) -> str:
        output = _output_path(cmd)
        if output is not None and any(
            f".{view}." in output.name for view in succeed_views
        ):
            output.write_bytes(PNG_BYTES)
        return ""

    return _run


def _output_path(cmd: list[str]) -> Path | None:
    for index, token in enumerate(cmd):
        if token.startswith("-SaveSnapshot="):
            return Path(token.split("=", 1)[1])
        if token in {"--image", "-i"}:
            return Path(cmd[index + 1])
    return None


RENDERER = Renderer(kind="ldview", executable=Path("/usr/bin/false"))


def test_all_views_succeed_exit_zero(model, tmp_path):
    report = render_model_views(
        model,
        renderer=RENDERER,
        ldraw_dir=tmp_path,
        runner=_runner({"front", "iso", "top"}),
    )
    assert [outcome.status for outcome in report.outcomes] == ["ok"] * 3
    assert (model.parent / "simple.front.png").is_file()
    assert report.renderer_kind == "ldview"
    info = write_render_info(report, out_dir=model.parent)
    payload = json.loads(info.read_text())
    assert payload["schema"] == "legolization.render-info/v1"
    assert payload["renderer"]["kind"] == "ldview"


def test_partial_and_zero_success_reported(model, tmp_path):
    report = render_model_views(
        model,
        renderer=RENDERER,
        ldraw_dir=tmp_path,
        runner=_runner({"iso"}),
    )
    statuses = {outcome.view: outcome.status for outcome in report.outcomes}
    assert statuses == {"front": "failed", "iso": "ok", "top": "failed"}
    report = render_model_views(
        model,
        views=("front",),
        renderer=RENDERER,
        ldraw_dir=tmp_path,
        runner=_runner(set()),
    )
    assert report.succeeded == ()
    assert report.outcomes[0].detail is not None


def test_collisions_get_numeric_suffixes(model, tmp_path):
    (model.parent / "simple.iso.png").write_bytes(b"existing")
    report = render_model_views(
        model,
        views=("iso",),
        renderer=RENDERER,
        ldraw_dir=tmp_path,
        runner=_runner({"iso"}),
    )
    assert report.outcomes[0].path == model.parent / "simple.iso.2.png"
    assert (model.parent / "simple.iso.png").read_bytes() == b"existing"


def test_bundle_directory_resolves_primary_model(model, tmp_path):
    from legolization.bundle.identity import BundleIdentity
    from legolization.bundle.record import BundleRecord, write_record

    bundle_dir = tmp_path / "bundle"
    model_dir = bundle_dir / "model"
    model_dir.mkdir(parents=True)
    (model_dir / "model.mpd").write_text(GOLDEN.read_text())
    record = BundleRecord(
        identity=BundleIdentity(
            input_sha256="a" * 64,
            config_sha256="b" * 64,
            legolization_version="0.6.0",
            catalog_sha256="c" * 64,
        ),
        source={"filename": "simple.ldr", "sha256": "a" * 64},
        configuration={"sha256": "b" * 64, "values": {}},
        versions={
            "legolization": "0.6.0",
            "python": "3",
            "libraries": {},
            "catalog_sha256": "c" * 64,
        },
        quality="direct",
    )
    record.record_artifact(path="model/model.mpd", stage="model", kind="model")
    write_record(record, bundle_dir)
    assert resolve_model(bundle_dir) == model_dir / "model.mpd"


def test_non_bundle_directory_is_rejected(tmp_path):
    empty = tmp_path / "not-a-bundle"
    empty.mkdir()
    with pytest.raises(ConfigurationError, match="bundle"):
        resolve_model(empty)


def test_unknown_view_is_rejected(model):
    with pytest.raises(ConfigurationError, match="unknown view"):
        render_model_views(model, views=("side",), renderer=RENDERER)


def test_missing_renderer_fails_every_view(model, monkeypatch, tmp_path):
    monkeypatch.setenv("LEGOLIZATION_RENDERER", "none")
    monkeypatch.setenv("LEGOLIZATION_DATA_HOME", str(tmp_path / "empty"))
    report = render_model_views(model, ldraw_dir=tmp_path)
    assert all(outcome.status == "failed" for outcome in report.outcomes)
    assert report.renderer_kind is None
    assert "no renderer found" in (report.outcomes[0].detail or "")


def test_model_render_cli_exit_codes(model, tmp_path, monkeypatch, capsys):
    from legolization import model_render as model_render_module
    from legolization.cli import main

    def _fake_single(model_path, output, **kwargs) -> None:
        output.write_bytes(PNG_BYTES)

    monkeypatch.setattr(model_render_module, "detect_renderer", lambda: RENDERER)
    monkeypatch.setattr(model_render_module, "detect_ldraw_dir", lambda: tmp_path)
    monkeypatch.setattr(model_render_module, "render_single_image", _fake_single)
    code = main(["model", "render", str(model), "--views", "iso", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["command"] == "model render"
    assert payload["exit_code"] == 0
    assert payload["data"]["outcomes"][0]["status"] == "ok"
