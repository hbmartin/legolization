#!/usr/bin/env python3
"""Render an LDraw model to PNGs from several camera angles.

Auto-detects a renderer on PATH in the order ``ldview`` -> ``leocad`` and an
LDraw parts library on disk, then produces ``<prefix>.front.png``,
``<prefix>.iso.png``, and ``<prefix>.top.png`` so a model can be inspected
visually.

Usage::

    python render.py MODEL.ldr [--prefix NAME] [--size WxH] [--views front,iso,top]
        [--ldraw-dir DIR]

A model file (``.ldr``/``.mpd``) only references parts by id; the renderer needs
the LDraw parts library to resolve them. The library is located from, in order:
``--ldraw-dir``, ``$LDRAWDIR``, then well-known install locations. Without it
LeoCAD falls back to a tiny built-in set and slopes/tiles render blank.

Success is decided by whether a non-empty PNG was written, not by the renderer's
exit code: an unconfigured LDView exits 0 without writing a file, while LeoCAD
under ``xvfb-run`` can exit non-zero after writing a perfectly good one.

Existing requested-view images move to ``previous/<UTC timestamp>/`` before
replacement. Prints ``ARCHIVED: <path>`` and ``RENDERED: <path>`` lines plus a
final summary. Exits 0 if at least one image was written, 1 if none.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# (latitude, longitude) in degrees for each named view.
VIEWS: dict[str, tuple[int, int]] = {
    "front": (0, 0),
    "iso": (30, 45),
    "top": (89, 0),
}

RENDER_TIMEOUT_S = 240
RENDERER_PRIORITY = ("ldview", "leocad")

# macOS app bundles are not on PATH; probe their executables directly.
APP_BUNDLE_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "ldview": ("/Applications/LDView.app/Contents/MacOS/LDView",),
    "leocad": ("/Applications/LeoCAD.app/Contents/MacOS/LeoCAD",),
}

# Directories that commonly hold an LDraw parts library. Globs are expanded and
# each candidate is validated by the presence of a ``parts`` subdirectory.
LDRAW_DIR_CANDIDATES: tuple[str, ...] = (
    "/usr/share/ldraw",
    "/usr/local/share/ldraw",
    "~/.ldraw",
    "~/ldraw",
    "~/.cache/pyldraw3/*/ldraw",
    "~/Library/Caches/pyldraw3/*/ldraw",
    "~/Library/Application Support/LDraw",
)


@dataclass(frozen=True)
class RenderRequest:
    """Validated inputs and output paths for one render invocation."""

    model: Path
    outputs: dict[str, Path]
    renderers: list[tuple[str, str]]  # (name, executable)
    size: tuple[int, int]
    views: list[str]
    ldraw_dir: Path | None


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        # Renderer arguments are passed directly; shell execution is disabled.
        proc = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=RENDER_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {RENDER_TIMEOUT_S}s: {' '.join(cmd)}"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, ""


def _detect() -> list[tuple[str, str]]:
    """Find available renderers as (name, executable) pairs, PATH first."""
    found: list[tuple[str, str]] = []
    for renderer in RENDERER_PRIORITY:
        if executable := shutil.which(renderer):
            found.append((renderer, executable))
            continue
        for bundle in APP_BUNDLE_EXECUTABLES.get(renderer, ()):
            if Path(bundle).is_file():
                found.append((renderer, bundle))
                break
    return found


def _is_ldraw_dir(path: Path) -> bool:
    """Report whether ``path`` holds an LDraw library (has a ``parts`` dir)."""
    return (path / "parts").is_dir()


def _detect_ldraw_dir(explicit: Path | None) -> Path | None:
    """Locate the LDraw parts library, or ``None`` if no library is found."""
    if explicit is not None:
        return explicit if _is_ldraw_dir(explicit) else None

    if (env := os.environ.get("LDRAWDIR")) and _is_ldraw_dir(candidate := Path(env)):
        return candidate

    for pattern in LDRAW_DIR_CANDIDATES:
        expanded = Path(pattern).expanduser()
        matches = (
            sorted(Path().glob(str(expanded).lstrip("/")))
            if "*" in pattern
            else [expanded]
        )
        for match in matches:
            if _is_ldraw_dir(match):
                return match
    return None


def _render_ldview(  # noqa: PLR0913 - one scalar per render knob
    executable: str,
    model: Path,
    out: Path,
    angle: tuple[int, int],
    size: tuple[int, int],
    ldraw_dir: Path | None,
) -> tuple[bool, str]:
    lat, lon = angle
    width, height = size
    cmd = [
        executable,
        str(model),
        f"-SaveSnapshot={out}",
        f"-SaveWidth={width}",
        f"-SaveHeight={height}",
        f"-DefaultLatLong={lat},{lon}",
        "-AutoCrop=1",
        "-SaveAlpha=0",
    ]
    if ldraw_dir is not None:
        cmd.append(f"-LDrawDir={ldraw_dir}")
    return _run(cmd)


def _render_leocad(  # noqa: PLR0913 - one scalar per render knob
    executable: str,
    model: Path,
    out: Path,
    angle: tuple[int, int],
    size: tuple[int, int],
    ldraw_dir: Path | None,
) -> tuple[bool, str]:
    lat, lon = angle
    width, height = size
    cmd = [
        executable,
        str(model),
        "--image",
        str(out),
        "--width",
        str(width),
        "--height",
        str(height),
        "--camera-angles",
        str(lat),
        str(lon),
    ]
    if ldraw_dir is not None:
        cmd += ["--libpath", str(ldraw_dir)]
    # LeoCAD renders through OpenGL; on headless Linux wrap it in Xvfb.
    if platform.system() == "Linux" and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1600x1200x24", *cmd]
    return _run(cmd)


RENDERERS = {
    "ldview": _render_ldview,
    "leocad": _render_leocad,
}


def _parse_views(value: str) -> list[str]:
    views: list[str] = []
    for view in (item.strip() for item in value.split(",") if item.strip()):
        if view not in VIEWS:
            print(f"skipping unknown view {view!r}", file=sys.stderr)
            continue
        views.append(view)
    return views


def _archive_stamp() -> str:
    """Return a sortable UTC timestamp for a render-history directory."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _create_archive_dir(out_dir: Path) -> Path:
    """Create a unique directory for one prior render set."""
    archive_root = out_dir / "previous"
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = _archive_stamp()
    suffix = 0
    while True:
        name = stamp if suffix == 0 else f"{stamp}-{suffix:02d}"
        candidate = archive_root / name
        try:
            candidate.mkdir()
        except FileExistsError:
            suffix += 1
            continue
        return candidate


def _archive_existing(outputs: list[Path]) -> list[Path]:
    """Move existing requested outputs into one timestamped history directory."""
    existing = [output for output in outputs if output.is_file()]
    if not existing:
        return []

    archive_dir = _create_archive_dir(existing[0].parent)
    archived: list[Path] = []
    for output in existing:
        destination = archive_dir / output.name
        output.replace(destination)
        archived.append(destination)
    return archived


def _remove_partial_output(output: Path) -> str | None:
    """Remove a failed renderer's output and return any cleanup error."""
    try:
        output.unlink(missing_ok=True)
    except OSError as exc:
        return str(exc)
    return None


def _wrote_image(output: Path) -> bool:
    """Whether the renderer left a non-empty image behind."""
    try:
        return output.is_file() and output.stat().st_size > 0
    except OSError:
        return False


def _prepare_request(args: argparse.Namespace) -> RenderRequest | None:
    """Validate CLI arguments without changing existing render files."""
    model: Path = args.model
    if not model.is_file():
        print(f"error: model not found: {model}", file=sys.stderr)
        return None

    try:
        width_s, height_s = args.size.lower().split("x", 1)
        size = (int(width_s), int(height_s))
    except ValueError:
        print(
            f"error: bad --size {args.size!r}; expected WxH like 1024x768",
            file=sys.stderr,
        )
        return None

    views = _parse_views(args.views)
    if not views:
        print("error: no valid views specified", file=sys.stderr)
        return None

    renderers = _detect()
    if not renderers:
        print(
            "renderer: NONE — no ldview/leocad on PATH or in /Applications; "
            "cannot render. Run preflight.sh or install LeoCAD.",
            file=sys.stderr,
        )
        return None

    ldraw_dir = _detect_ldraw_dir(args.ldraw_dir)
    if ldraw_dir is None:
        print(
            "ldraw library: NONE — no parts library found; non-basic parts "
            "(slopes, tiles) will render blank. Run preflight.sh or pass "
            "--ldraw-dir.",
            file=sys.stderr,
        )
    else:
        print(f"ldraw library: {ldraw_dir}", file=sys.stderr)

    prefix = args.prefix or model.stem
    out_dir = model.resolve().parent
    outputs = {view: out_dir / f"{prefix}.{view}.png" for view in views}
    return RenderRequest(
        model=model,
        outputs=outputs,
        renderers=renderers,
        size=size,
        views=views,
        ldraw_dir=ldraw_dir,
    )


def _render_with_backend(
    request: RenderRequest, renderer: tuple[str, str]
) -> list[Path]:
    """Render all requested views with one backend."""
    name, executable = renderer
    print(f"renderer: {name} ({executable})", file=sys.stderr)
    render_fn = RENDERERS[name]
    produced: list[Path] = []
    for view in request.views:
        out = request.outputs[view]
        if cleanup_error := _remove_partial_output(out):
            message = f"could not remove stale output: {cleanup_error}"
            print(f"failed {view} view: {message}", file=sys.stderr)
            continue

        _, err = render_fn(
            executable, request.model, out, VIEWS[view], request.size, request.ldraw_dir
        )
        # The image on disk is the real signal: a renderer can exit non-zero
        # yet leave a valid PNG, or exit zero without writing one.
        if _wrote_image(out):
            produced.append(out)
            print(f"RENDERED: {out}")
            continue

        cleanup_error = _remove_partial_output(out)
        if not err:
            err = "renderer exited without producing an image"
        if cleanup_error:
            err = f"{err}; could not remove partial output: {cleanup_error}"
        print(f"failed {view} view: {err}", file=sys.stderr)
    return produced


def main(argv: list[str] | None = None) -> int:
    """Render requested model views and return whether any image was produced."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="path to the .ldr/.mpd model")
    parser.add_argument(
        "--prefix",
        default=None,
        help="output name stem (default: model stem)",
    )
    parser.add_argument(
        "--size",
        default="1024x768",
        help="image size WxH (default 1024x768)",
    )
    parser.add_argument(
        "--views",
        default="front,iso,top",
        help="comma-separated subset of: " + ",".join(VIEWS),
    )
    parser.add_argument(
        "--ldraw-dir",
        type=Path,
        default=None,
        help="path to the LDraw parts library (default: auto-detect)",
    )
    args = parser.parse_args(argv)
    request = _prepare_request(args)
    if request is None:
        return 1

    try:
        archived = _archive_existing(list(request.outputs.values()))
    except OSError as exc:
        print(f"error: could not archive previous renders: {exc}", file=sys.stderr)
        return 1
    for path in archived:
        print(f"ARCHIVED: {path}")

    for renderer in request.renderers:
        produced = _render_with_backend(request, renderer)
        if produced:
            print(
                f"Rendered {len(produced)} view(s) with {renderer[0]}.",
                file=sys.stderr,
            )
            return 0
        print(
            f"{renderer[0]} produced no images; trying next renderer.",
            file=sys.stderr,
        )

    print("No images produced.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
