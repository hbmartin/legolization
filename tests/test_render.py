"""Renderer detection, camera math, and per-step PNG collection."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from legolization.instructions import (
    BillOfMaterials,
    BuildStep,
    InstructionPlan,
    RotStep,
)
from legolization.instructions import render as render_mod
from legolization.instructions.render import (
    _YAW_SIGN,
    RenderConfig,
    Renderer,
    _sanitized,
    _step_longitudes,
    _view_segments,
    detect_ldraw_dir,
    detect_renderer,
    render_step_images,
)


def _plan(steps: int, rotsteps: dict[int, RotStep] | None = None) -> InstructionPlan:
    rotsteps = rotsteps or {}
    return InstructionPlan(
        steps=tuple(
            BuildStep(
                index=index,
                brick_ids=(index,),
                prefix_stable=True,
                prefix_max_score=0.1,
                rotstep=rotsteps.get(index),
            )
            for index in range(1, steps + 1)
        ),
        warnings=(),
        bom=BillOfMaterials(total=(), per_step=()),
    )


def _model_file(tmp_path: Path, steps: int) -> Path:
    lines = ["0 test model"]
    for index in range(steps):
        lines.append(f"1 4 {20 * index} -24 0 1 0 0 0 1 0 0 0 1 3005.dat")
        lines.append("0 STEP")
    path = tmp_path / "model.ldr"
    path.write_text("\n".join(lines) + "\n")
    return path


def _ldraw_dir(tmp_path: Path) -> Path:
    library = tmp_path / "ldraw"
    (library / "parts").mkdir(parents=True)
    return library


def _leocad(tmp_path: Path) -> Renderer:
    exe = tmp_path / "leocad"
    exe.write_text("#!/bin/sh\n")
    return Renderer(kind="leocad", executable=exe)


def _leocad_runner(content: bytes = b"png-") -> Callable[[list[str], float], str]:
    """Emulate LeoCAD naming: absolute step numbers for ranges, else verbatim."""

    def run(cmd: list[str], timeout_s: float) -> str:
        out = Path(cmd[cmd.index("-i") + 1])
        first = int(cmd[cmd.index("-f") + 1])
        last = int(cmd[cmd.index("-t") + 1])
        if first == last:
            out.write_bytes(content + str(first).encode())
        else:
            for step_no in range(first, last + 1):
                target = out.parent / f"step{step_no:02d}.png"
                target.write_bytes(content + str(step_no).encode())
        return ""

    return run


# --- detection ---


def test_env_override_none_disables(monkeypatch):
    monkeypatch.setattr(render_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert detect_renderer(env={"LEGOLIZATION_RENDERER": "none"}) is None


def test_env_override_explicit_path_infers_kind(tmp_path):
    exe = tmp_path / "MyLDView"
    exe.write_text("#!/bin/sh\n")
    renderer = detect_renderer(env={"LEGOLIZATION_RENDERER": str(exe)})
    assert renderer == Renderer(kind="ldview", executable=exe)


def test_env_override_command_name(monkeypatch, tmp_path):
    exe = tmp_path / "leocad"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        render_mod.shutil,
        "which",
        lambda name: str(exe) if name == "leocad" else None,
    )
    renderer = detect_renderer(env={"LEGOLIZATION_RENDERER": "leocad"})
    assert renderer == Renderer(kind="leocad", executable=exe)


def test_path_prefers_leocad(monkeypatch):
    monkeypatch.setattr(render_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    renderer = detect_renderer(env={})
    assert renderer is not None
    assert renderer.kind == "leocad"


def test_app_bundle_fallback(monkeypatch, tmp_path):
    exe = tmp_path / "LDView"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(render_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(render_mod, "_APP_BUNDLES", (("ldview", str(exe)),))
    renderer = detect_renderer(env={})
    assert renderer == Renderer(kind="ldview", executable=exe)


def test_no_renderer_found(monkeypatch):
    monkeypatch.setattr(render_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(render_mod, "_APP_BUNDLES", ())
    assert detect_renderer(env={}) is None


def test_detect_ldraw_dir_explicit(tmp_path):
    library = _ldraw_dir(tmp_path)
    assert detect_ldraw_dir(library) == library
    assert detect_ldraw_dir(tmp_path / "missing") is None


def test_detect_ldraw_dir_env(monkeypatch, tmp_path):
    library = _ldraw_dir(tmp_path)
    monkeypatch.setattr(render_mod, "_LDRAW_GLOBS", ())
    monkeypatch.setattr(render_mod, "_LDRAW_DIRS", ())
    assert detect_ldraw_dir(env={"LDRAWDIR": str(library)}) == library
    assert detect_ldraw_dir(env={}) is None


# --- camera math and sanitizing ---


def test_step_longitudes_accumulate_rel_rotsteps():
    plan = _plan(4, rotsteps={2: RotStep(yaw=90), 4: RotStep(yaw=90)})
    longitudes = _step_longitudes(plan, base=45.0)
    assert longitudes[0] == 45.0
    assert longitudes[1] == longitudes[2] == (45.0 + _YAW_SIGN * 90) % 360.0
    assert longitudes[3] == (45.0 + _YAW_SIGN * 180) % 360.0


def test_step_longitudes_abs_and_end():
    plan = _plan(
        3,
        rotsteps={2: RotStep(yaw=270, mode="ABS"), 3: RotStep(yaw=0, mode="END")},
    )
    longitudes = _step_longitudes(plan, base=45.0)
    assert longitudes[0] == 45.0
    assert longitudes[1] == (45.0 + _YAW_SIGN * 270) % 360.0
    assert longitudes[2] == 45.0


def test_view_segments_group_consecutive_runs():
    assert _view_segments((45.0, 45.0, 315.0, 315.0, 45.0)) == [
        (1, 2, 45.0),
        (3, 4, 315.0),
        (5, 5, 45.0),
    ]


def test_sanitized_strips_only_rotstep_lines():
    lines = [
        "0 FILE m.ldr",
        "0 ROTSTEP 0 90 0 REL",
        "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3005.dat",
        "0 STEP",
        "0 ROTSTEP END",
        "0 NOFILE",
    ]
    cleaned = _sanitized("\n".join(lines) + "\n")
    assert "ROTSTEP" not in cleaned
    assert "0 FILE m.ldr" in cleaned
    assert "0 STEP" in cleaned
    assert "0 NOFILE" in cleaned


# --- LeoCAD flow ---


def test_leocad_renders_segments_and_orders_images(tmp_path):
    model = _model_file(tmp_path, steps=3)
    plan = _plan(3, rotsteps={3: RotStep(yaw=90)})
    config = RenderConfig(renderer=_leocad(tmp_path), ldraw_dir=_ldraw_dir(tmp_path))
    images = render_step_images(model, plan, config=config, runner=_leocad_runner())
    assert images.complete
    assert images.renderer is not None
    tails = [image[-1:] for image in images.images if image is not None]
    assert tails == [b"1", b"2", b"3"]
    assert images.warnings == ()


def test_leocad_command_shape(tmp_path):
    model = _model_file(tmp_path, steps=2)
    library = _ldraw_dir(tmp_path)
    calls: list[list[str]] = []

    def run(cmd: list[str], timeout_s: float) -> str:
        calls.append(cmd)
        return _leocad_runner()(cmd, timeout_s)

    config = RenderConfig(renderer=_leocad(tmp_path), ldraw_dir=library)
    render_step_images(model, _plan(2), config=config, runner=run)
    (cmd,) = calls
    assert "--highlight" in cmd
    assert cmd[cmd.index("-l") + 1] == str(library)
    assert cmd[cmd.index("-f") + 1] == "1"
    assert cmd[cmd.index("-t") + 1] == "2"
    lat_at = cmd.index("--camera-angles")
    assert cmd[lat_at + 1 : lat_at + 3] == ["30", "45"]


def test_zero_byte_images_are_failures(tmp_path):
    model = _model_file(tmp_path, steps=2)
    config = RenderConfig(renderer=_leocad(tmp_path), ldraw_dir=_ldraw_dir(tmp_path))

    def run(cmd: list[str], timeout_s: float) -> str:
        out = Path(cmd[cmd.index("-i") + 1])
        first = int(cmd[cmd.index("-f") + 1])
        last = int(cmd[cmd.index("-t") + 1])
        for step_no in range(first, last + 1):
            (out.parent / f"step{step_no:02d}.png").write_bytes(b"")
        return ""

    images = render_step_images(model, _plan(2), config=config, runner=run)
    assert images.images == (None, None)
    assert len(images.warnings) == 2
    assert "produced no image" in images.warnings[0]


def test_renderer_writing_nothing_yields_warnings(tmp_path):
    model = _model_file(tmp_path, steps=2)
    config = RenderConfig(renderer=_leocad(tmp_path), ldraw_dir=_ldraw_dir(tmp_path))
    images = render_step_images(
        model, _plan(2), config=config, runner=lambda cmd, timeout_s: "boom"
    )
    assert images.images == (None, None)
    assert all("boom" in warning for warning in images.warnings)


def test_timeout_is_a_warning_not_a_crash(tmp_path):
    model = _model_file(tmp_path, steps=2)
    config = RenderConfig(renderer=_leocad(tmp_path), ldraw_dir=_ldraw_dir(tmp_path))

    def run(cmd: list[str], timeout_s: float) -> str:
        raise subprocess.TimeoutExpired(cmd=cmd[0], timeout=timeout_s)

    images = render_step_images(model, _plan(2), config=config, runner=run)
    assert images.images == (None, None)
    assert any("timed out" in warning for warning in images.warnings)


def test_missing_ldraw_library_warns_but_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(render_mod, "_LDRAW_GLOBS", ())
    monkeypatch.setattr(render_mod, "_LDRAW_DIRS", ())
    monkeypatch.delenv("LDRAWDIR", raising=False)
    model = _model_file(tmp_path, steps=1)
    config = RenderConfig(renderer=_leocad(tmp_path))
    images = render_step_images(model, _plan(1), config=config, runner=_leocad_runner())
    assert images.complete
    assert any("parts library not found" in warning for warning in images.warnings)


# --- LDView flow ---


def test_ldview_one_invocation_per_step(tmp_path):
    model = _model_file(tmp_path, steps=2)
    calls: list[list[str]] = []

    def run(cmd: list[str], timeout_s: float) -> str:
        calls.append(cmd)
        snapshot = next(arg for arg in cmd if arg.startswith("-SaveSnapshot="))
        Path(snapshot.split("=", 1)[1]).write_bytes(b"png")
        return ""

    exe = tmp_path / "ldview"
    exe.write_text("#!/bin/sh\n")
    config = RenderConfig(
        renderer=Renderer(kind="ldview", executable=exe),
        ldraw_dir=_ldraw_dir(tmp_path),
    )
    images = render_step_images(model, _plan(2), config=config, runner=run)
    assert images.complete
    assert len(calls) == 2
    for step_no, cmd in enumerate(calls, start=1):
        assert f"-Step={step_no}" in cmd
        assert "-AutoCrop=0" in cmd
        assert any(arg.startswith("-DefaultLatLong=30,") for arg in cmd)
        assert any(arg.startswith("-LDrawDir=") for arg in cmd)


# --- degradation and integration ---


def test_no_renderer_degrades_to_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGOLIZATION_RENDERER", "none")
    model = _model_file(tmp_path, steps=3)
    images = render_step_images(model, _plan(3))
    assert images.images == (None, None, None)
    assert images.renderer is None
    assert not images.complete
    assert any("no LDraw renderer" in warning for warning in images.warnings)


@pytest.mark.slow
@pytest.mark.skipif(
    detect_renderer() is None or detect_ldraw_dir() is None,
    reason="no LDraw renderer/library installed",
)
def test_real_renderer_end_to_end(tmp_path):
    model = _model_file(tmp_path, steps=2)
    images = render_step_images(model, _plan(2, rotsteps={2: RotStep(yaw=90)}))
    assert images.complete
    for image in images.images:
        assert image is not None
        assert image.startswith(b"\x89PNG")
