"""Per-step PNG rendering of an instruction plan via LeoCAD or LDView.

The renderer draws the *already written* ``.ldr``/``.mpd`` (through a
temporary copy with ``0 ROTSTEP`` lines stripped) so the camera framing stays
constant across steps: renderers fit the full-model bounding box, whereas
per-prefix temp models would re-fit — and zoom-jump — every step. View
rotations still happen, but deterministically: the camera longitude for each
step is computed here from the plan's own :class:`RotStep` data, so LeoCAD
and LDView produce the same story.

Success is decided by a non-empty PNG on disk, never by the exit code — an
unconfigured LDView exits 0 without writing a file. Rendering is strictly
optional: with no renderer installed every image is ``None`` and callers
degrade to placeholders.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from legolization.ldraw_out import _submodel_file

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from legolization.instructions.sequencer import BuildStep, InstructionPlan

type RendererKind = Literal["leocad", "ldview"]
type Runner = Callable[[list[str], float], str]
"""Run a renderer command with a timeout; returns captured stderr.

Success is judged by the files left on disk, never by this callable's
return value. May raise ``subprocess.TimeoutExpired`` or ``OSError``.
"""

_ENV_RENDERER = "LEGOLIZATION_RENDERER"
_ENV_LDRAW_DIR = "LDRAWDIR"

# Presented azimuth after rotating the model by ``view`` degrees is
# ``base - view`` (see ``sequencer._facing``); the camera longitude moves the
# opposite way. Verified against real renders — flip here if a renderer
# convention ever disagrees.
_YAW_SIGN = -1

_PATH_CANDIDATES: tuple[RendererKind, ...] = ("leocad", "ldview")

# macOS installs neither binary on PATH; monkeypatchable in tests.
_APP_BUNDLES: tuple[tuple[RendererKind, str], ...] = (
    ("leocad", "/Applications/LeoCAD.app/Contents/MacOS/LeoCAD"),
    ("ldview", "/Applications/LDView.app/Contents/MacOS/LDView"),
)

# pyldraw3's cache first: it is the library this project's parts came from.
_LDRAW_GLOBS: tuple[tuple[str, str], ...] = (
    ("~/Library/Caches/pyldraw3", "*/ldraw"),
    ("~/.cache/pyldraw3", "*/ldraw"),
)
_LDRAW_DIRS: tuple[str, ...] = (
    "/usr/share/ldraw",
    "/usr/local/share/ldraw",
    "~/.ldraw",
    "~/ldraw",
    "~/Library/Application Support/LDraw",
)

_NO_RENDERER_WARNING = (
    f"no LDraw renderer found (checked ${_ENV_RENDERER}, PATH, /Applications); "
    "step images will be placeholders"
)


@dataclass(frozen=True, slots=True)
class Renderer:
    """A resolved renderer executable and how to talk to it."""

    kind: RendererKind
    executable: Path


@dataclass(frozen=True, slots=True)
class RenderConfig:
    """Knobs for per-step image rendering."""

    width: int = 800
    height: int = 600
    latitude: float = 30.0
    longitude: float = 45.0
    highlight: bool = True
    timeout_s: float = 240.0
    ldraw_dir: Path | None = None
    renderer: Renderer | None = None


@dataclass(frozen=True, slots=True)
class StepImages:
    """PNG bytes per plan step (``None`` where rendering failed)."""

    images: tuple[bytes | None, ...]
    renderer: Renderer | None
    warnings: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """Whether every step produced an image."""
        return all(image is not None for image in self.images)


def detect_renderer(*, env: Mapping[str, str] | None = None) -> Renderer | None:
    """Locate a renderer: env override, then PATH, then macOS app bundles."""
    env = env if env is not None else os.environ
    if (override := env.get(_ENV_RENDERER)) is not None:
        if override.strip().lower() == "none":
            return None
        return _renderer_from(override)
    for name in _PATH_CANDIDATES:
        if (found := shutil.which(name)) is not None:
            return Renderer(kind=name, executable=Path(found))
    for kind, bundle in _APP_BUNDLES:
        if (path := Path(bundle)).is_file():
            return Renderer(kind=kind, executable=path)
    return None


def _renderer_from(value: str) -> Renderer | None:
    """Resolve an explicit renderer path or command name."""
    kind: RendererKind = "ldview" if "ldview" in Path(value).name.lower() else "leocad"
    if (candidate := Path(value).expanduser()).is_file():
        return Renderer(kind=kind, executable=candidate)
    if (found := shutil.which(value)) is not None:
        return Renderer(kind=kind, executable=Path(found))
    return None


def detect_ldraw_dir(
    explicit: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Locate the LDraw parts library (a directory holding ``parts/``)."""
    if explicit is not None:
        return explicit if _is_ldraw_dir(explicit) else None
    env = env if env is not None else os.environ
    if (value := env.get(_ENV_LDRAW_DIR)) and _is_ldraw_dir(candidate := Path(value)):
        return candidate
    for base, pattern in _LDRAW_GLOBS:
        for match in sorted(Path(base).expanduser().glob(pattern)):
            if _is_ldraw_dir(match):
                return match
    for candidate_dir in _LDRAW_DIRS:
        if _is_ldraw_dir(expanded := Path(candidate_dir).expanduser()):
            return expanded
    return None


def _is_ldraw_dir(path: Path) -> bool:
    return (path / "parts").is_dir()


def render_step_images(
    model_path: Path,
    plan: InstructionPlan,
    *,
    config: RenderConfig | None = None,
    runner: Runner | None = None,
    progress: Callable[[str], None] | None = None,
) -> StepImages:
    """Render one PNG per plan step from the written model file.

    Plans with subassemblies render in passes: the main model's steps
    from the written ``.mpd`` (whose main-file step count equals the
    plan's main+attach steps), then each subassembly's FILE section
    sliced into its own temp ``.ldr`` — renderer-agnostic, no submodel
    CLI flags needed. The returned ``images`` tuple stays aligned with
    the flat ``plan.steps``.
    """
    config = config or RenderConfig()
    renderer = config.renderer or detect_renderer()
    if renderer is None:
        return StepImages(
            images=(None,) * len(plan.steps),
            renderer=None,
            warnings=(_NO_RENDERER_WARNING,),
        )
    warnings: list[str] = []
    ldraw_dir = detect_ldraw_dir(config.ldraw_dir)
    if ldraw_dir is None:
        warnings.append(
            "LDraw parts library not found; slopes and tiles may render blank "
            f"(set ${_ENV_LDRAW_DIR} or RenderConfig.ldraw_dir)"
        )
    run = runner or _run_subprocess
    text = model_path.read_text(encoding="utf-8")
    if not plan.subassemblies:
        longitudes = _step_longitudes(plan.steps, base=config.longitude)
        collected = _render_text(
            model_path.name,
            text,
            longitudes,
            renderer,
            config,
            ldraw_dir,
            run,
            progress,
            warnings,
        )
        images = tuple(
            collected.get(step_no) for step_no in range(1, len(longitudes) + 1)
        )
        return StepImages(images=images, renderer=renderer, warnings=tuple(warnings))
    return _render_with_subassemblies(
        model_path,
        plan,
        text,
        renderer,
        config,
        ldraw_dir,
        run,
        progress,
        warnings,
    )


def _render_with_subassemblies(  # noqa: PLR0913 - one bag of render state
    model_path: Path,
    plan: InstructionPlan,
    text: str,
    renderer: Renderer,
    config: RenderConfig,
    ldraw_dir: Path | None,
    run: Runner,
    progress: Callable[[str], None] | None,
    warnings: list[str],
) -> StepImages:
    main = plan.main_steps()
    main_longitudes = _step_longitudes(main, base=config.longitude)
    collected_main = _render_text(
        model_path.name,
        text,
        main_longitudes,
        renderer,
        config,
        ldraw_dir,
        run,
        progress,
        warnings,
    )
    sub_collected: dict[str, dict[int, bytes]] = {}
    stem = model_path.stem
    if "0 FILE" in text:
        for sub in plan.subassemblies:
            sub_file = _submodel_file(stem, sub.name)
            sub_text = _extract_submodel(text, sub_file)
            if sub_text is None:
                warnings.append(f"submodel {sub_file} not found in the model file")
                continue
            n_steps = len(plan.sub_steps(sub.name))
            sub_collected[sub.name] = _render_text(
                sub_file,
                sub_text,
                (config.longitude,) * n_steps,
                renderer,
                config,
                ldraw_dir,
                run,
                progress,
                warnings,
            )
    else:
        warnings.append(
            "subassembly step images need .mpd output (the .ldr fallback "
            "flattens submodels)"
        )
    images: list[bytes | None] = []
    main_ordinal = 0
    sub_ordinals: dict[str, int] = dict.fromkeys(sub_collected, 0)
    for step in plan.steps:
        if step.submodel is not None:
            if step.submodel in sub_collected:
                sub_ordinals[step.submodel] += 1
                images.append(
                    sub_collected[step.submodel].get(sub_ordinals[step.submodel])
                )
            else:
                images.append(None)
        else:
            main_ordinal += 1
            images.append(collected_main.get(main_ordinal))
    return StepImages(images=tuple(images), renderer=renderer, warnings=tuple(warnings))


def _extract_submodel(text: str, file_name: str) -> str | None:
    """Slice one ``0 FILE`` section's body out of an ``.mpd`` document."""
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == f"0 FILE {file_name}":
            start = i + 1
        elif start is not None and line.strip() == "0 NOFILE":
            return "\n".join(lines[start:i]) + "\n"
    return None


def _render_text(  # noqa: PLR0913 - one bag of render state, locally owned
    name: str,
    text: str,
    longitudes: tuple[float, ...],
    renderer: Renderer,
    config: RenderConfig,
    ldraw_dir: Path | None,
    run: Runner,
    progress: Callable[[str], None] | None,
    warnings: list[str],
) -> dict[int, bytes]:
    with tempfile.TemporaryDirectory(prefix="legolization-render-") as tmp:
        sanitized = Path(tmp) / name
        sanitized.write_text(_sanitized(text), encoding="utf-8")
        match renderer.kind:
            case "leocad":
                return _render_leocad(
                    renderer=renderer,
                    model=sanitized,
                    longitudes=longitudes,
                    config=config,
                    ldraw_dir=ldraw_dir,
                    run=run,
                    progress=progress,
                    warnings=warnings,
                )
            case _:
                return _render_ldview(
                    renderer=renderer,
                    model=sanitized,
                    longitudes=longitudes,
                    config=config,
                    ldraw_dir=ldraw_dir,
                    run=run,
                    progress=progress,
                    warnings=warnings,
                )


def _step_longitudes(
    steps: tuple[BuildStep, ...],
    *,
    base: float,
) -> tuple[float, ...]:
    """Camera longitude per step, accumulated from the steps' ROTSTEP hints."""
    view = 0.0
    longitudes: list[float] = []
    for step in steps:
        if (rotstep := step.rotstep) is not None:
            match rotstep.mode:
                case "REL":
                    view = (view + rotstep.yaw) % 360.0
                case "ABS":
                    view = float(rotstep.yaw) % 360.0
                case "END":
                    view = 0.0
        longitudes.append((base + _YAW_SIGN * view) % 360.0)
    return tuple(longitudes)


def _view_segments(longitudes: tuple[float, ...]) -> list[tuple[int, int, float]]:
    """Maximal runs of consecutive steps sharing a longitude, 1-based inclusive."""
    segments: list[tuple[int, int, float]] = []
    for index, longitude in enumerate(longitudes, start=1):
        if segments and segments[-1][2] == longitude:
            first, _, _ = segments[-1]
            segments[-1] = (first, index, longitude)
        else:
            segments.append((index, index, longitude))
    return segments


def _sanitized(text: str) -> str:
    """Strip ``0 ROTSTEP`` lines; renderers get their camera from us instead."""
    kept = [line for line in text.splitlines() if line.split()[:2] != ["0", "ROTSTEP"]]
    return "\n".join(kept) + "\n"


def _render_leocad(  # noqa: PLR0913 - one bag of render state, locally owned
    *,
    renderer: Renderer,
    model: Path,
    longitudes: tuple[float, ...],
    config: RenderConfig,
    ldraw_dir: Path | None,
    run: Runner,
    progress: Callable[[str], None] | None,
    warnings: list[str],
) -> dict[int, bytes]:
    """Render each same-view segment of steps in one LeoCAD invocation."""
    collected: dict[int, bytes] = {}
    for seg_index, (first, last, longitude) in enumerate(_view_segments(longitudes)):
        seg_dir = model.parent / f"seg{seg_index:03d}"
        seg_dir.mkdir()
        cmd = [
            str(renderer.executable),
            str(model),
            "-i",
            str(seg_dir / "step.png"),
            "-w",
            str(config.width),
            "-h",
            str(config.height),
            "--camera-angles",
            f"{config.latitude:g}",
            f"{longitude:g}",
            "-f",
            str(first),
            "-t",
            str(last),
        ]
        if config.highlight:
            cmd.append("--highlight")
        if ldraw_dir is not None:
            cmd += ["-l", str(ldraw_dir)]
        if progress is not None:
            progress(f"rendering steps {first}-{last}")
        stderr = _invoke(run, _wrap_xvfb(cmd), config.timeout_s, warnings)
        if stderr is None:
            continue
        found = _collect_pngs(seg_dir, first=first, last=last)
        for step_no in range(first, last + 1):
            if (data := found.get(step_no)) is not None:
                collected[step_no] = data
            else:
                warnings.append(_no_image_warning(step_no, stderr))
    return collected


def _render_ldview(  # noqa: PLR0913 - one bag of render state, locally owned
    *,
    renderer: Renderer,
    model: Path,
    longitudes: tuple[float, ...],
    config: RenderConfig,
    ldraw_dir: Path | None,
    run: Runner,
    progress: Callable[[str], None] | None,
    warnings: list[str],
) -> dict[int, bytes]:
    """Render one snapshot per step; LDView has no batched step export."""
    collected: dict[int, bytes] = {}
    for step_no, longitude in enumerate(longitudes, start=1):
        out = model.parent / f"ldview{step_no:04d}.png"
        cmd = [
            str(renderer.executable),
            str(model),
            f"-SaveSnapshot={out}",
            f"-SaveWidth={config.width}",
            f"-SaveHeight={config.height}",
            f"-DefaultLatLong={config.latitude:g},{longitude:g}",
            f"-Step={step_no}",
            "-SaveAlpha=0",
            # Per-step auto-crop would change the framing between steps.
            "-AutoCrop=0",
        ]
        if ldraw_dir is not None:
            cmd.append(f"-LDrawDir={ldraw_dir}")
        if progress is not None:
            progress(f"rendering step {step_no}")
        stderr = _invoke(run, cmd, config.timeout_s, warnings)
        if stderr is None:
            continue
        if out.is_file() and out.stat().st_size > 0:
            collected[step_no] = out.read_bytes()
        else:
            warnings.append(_no_image_warning(step_no, stderr))
    return collected


def _invoke(
    run: Runner,
    cmd: list[str],
    timeout_s: float,
    warnings: list[str],
) -> str | None:
    """Run one renderer command; ``None`` means it never got to draw."""
    try:
        return run(cmd, timeout_s)
    except subprocess.TimeoutExpired:
        warnings.append(f"renderer timed out after {timeout_s:g}s: {cmd[0]}")
    except OSError as error:
        warnings.append(f"renderer failed to start: {error}")
    return None


def _collect_pngs(directory: Path, *, first: int, last: int) -> dict[int, bytes]:
    """Map rendered PNGs to step numbers.

    LeoCAD appends a zero-padded *absolute* step number before the extension
    when rendering a range (``step01.png``) but writes the requested name
    verbatim for a single step — match trailing digits, with an unnumbered
    fallback for single-step segments.
    """
    found: dict[int, bytes] = {}
    for png in sorted(directory.glob("*.png")):
        if png.stat().st_size == 0:
            continue
        if (match := re.search(r"(\d+)\.png$", png.name)) is not None:
            if first <= (step_no := int(match.group(1))) <= last:
                found[step_no] = png.read_bytes()
        elif first == last:
            found.setdefault(first, png.read_bytes())
    return found


def _no_image_warning(step_no: int, stderr: str) -> str:
    detail = f": {trimmed}" if (trimmed := stderr.strip()) else ""
    return f"step {step_no}: renderer produced no image{detail}"


def _wrap_xvfb(cmd: list[str]) -> list[str]:
    """LeoCAD renders through OpenGL; on headless Linux wrap it in Xvfb."""
    if platform.system() == "Linux" and shutil.which("xvfb-run"):
        return ["xvfb-run", "-a", "-s", "-screen 0 1600x1200x24", *cmd]
    return cmd


def _run_subprocess(cmd: list[str], timeout_s: float) -> str:
    """Default :data:`Runner`: run the renderer, capture stderr for warnings."""
    proc = subprocess.run(  # noqa: S603 - fixed renderer executable, no shell
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout_s,
        check=False,
    )
    return proc.stderr or ""
