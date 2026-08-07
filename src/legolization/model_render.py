"""Multi-view model rendering behind ``legolization model render``.

Views render the whole written model from fixed camera angles. Existing
outputs are never overwritten — collisions get numeric file suffixes —
and the report records the renderer and parts-library identity behind
every image.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from legolization.errors import ConfigurationError
from legolization.instructions.render import (
    Renderer,
    Runner,
    detect_ldraw_dir,
    detect_renderer,
    render_single_image,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

VIEWS: dict[str, tuple[float, float]] = {
    "front": (0.0, 0.0),
    "iso": (30.0, 45.0),
    "top": (89.0, 0.0),
}
DEFAULT_VIEWS: tuple[str, ...] = ("front", "iso", "top")
RENDER_INFO_SCHEMA = "legolization.render-info/v1"

_VERSION_PROBE_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewOutcome:
    """One requested view and what happened to it."""

    view: str
    status: Literal["ok", "failed"]
    path: Path | None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this outcome."""
        return {
            "view": self.view,
            "status": self.status,
            "path": str(self.path) if self.path is not None else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelRenderReport:
    """All view outcomes plus renderer and library identity."""

    model: Path
    outcomes: tuple[ViewOutcome, ...]
    renderer_kind: str | None
    renderer_path: str | None
    renderer_version: str | None
    ldraw_dir: Path | None
    library: dict[str, Any] | None

    @property
    def succeeded(self) -> tuple[ViewOutcome, ...]:
        """The views that produced an image."""
        return tuple(item for item in self.outcomes if item.status == "ok")

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this report."""
        return {
            "schema": RENDER_INFO_SCHEMA,
            "model": self.model.name,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "renderer": {
                "kind": self.renderer_kind,
                "path": self.renderer_path,
                "version": self.renderer_version,
            },
            "ldraw_dir": str(self.ldraw_dir) if self.ldraw_dir else None,
            "library": self.library,
        }


def resolve_model(input_path: Path) -> Path:
    """Resolve a model file or a bundle directory to its primary model."""
    if input_path.is_dir():
        from legolization.bundle.record import read_record  # noqa: PLC0415

        record = read_record(input_path)
        if record is None:
            msg = f"{input_path} is not a bundle directory (no readable bundle.json)"
            raise ConfigurationError(msg)
        for entry in record.artifacts:
            if entry.kind == "model" and entry.path.endswith(".mpd"):
                return input_path / entry.path
        for entry in record.artifacts:
            if entry.kind == "model":
                return input_path / entry.path
        msg = f"bundle at {input_path} records no model artifact"
        raise ConfigurationError(msg)
    if input_path.suffix.lower() not in {".ldr", ".mpd"}:
        msg = f"model render expects a .ldr/.mpd model or bundle: {input_path}"
        raise ConfigurationError(msg)
    if not input_path.is_file():
        msg = f"model must be an existing file: {input_path}"
        raise ConfigurationError(msg)
    return input_path


def render_model_views(  # noqa: PLR0913 - one bag of render state
    input_path: Path,
    *,
    views: Sequence[str] = DEFAULT_VIEWS,
    out_dir: Path | None = None,
    width: int = 800,
    height: int = 600,
    renderer: Renderer | None = None,
    ldraw_dir: Path | None = None,
    runner: Runner | None = None,
) -> ModelRenderReport:
    """Render the requested views, never overwriting existing images."""
    from legolization.bundle.paths import numbered_file  # noqa: PLC0415

    model = resolve_model(input_path)
    for view in views:
        if view not in VIEWS:
            msg = f"unknown view {view!r}; expected {', '.join(sorted(VIEWS))}"
            raise ConfigurationError(msg)
    effective_renderer = renderer if renderer is not None else detect_renderer()
    effective_ldraw = ldraw_dir if ldraw_dir is not None else detect_ldraw_dir()
    target_dir = out_dir if out_dir is not None else model.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    outcomes: list[ViewOutcome] = []
    if effective_renderer is None:
        outcomes = [
            ViewOutcome(
                view=view,
                status="failed",
                path=None,
                detail=(
                    "no renderer found; install LDView (macOS), LeoCAD "
                    "(Windows via winget), or LeoCAD+Xvfb (Ubuntu)"
                ),
            )
            for view in views
        ]
    else:
        for view in views:
            latitude, longitude = VIEWS[view]
            output = numbered_file(target_dir / f"{model.stem}.{view}.png")
            warning = render_single_image(
                model,
                output,
                renderer=effective_renderer,
                latitude=latitude,
                longitude=longitude,
                width=width,
                height=height,
                ldraw_dir=effective_ldraw,
                run=runner,
            )
            outcomes.append(
                ViewOutcome(
                    view=view,
                    status="ok" if warning is None else "failed",
                    path=output if warning is None else None,
                    detail=warning,
                )
            )
    return ModelRenderReport(
        model=model,
        outcomes=tuple(outcomes),
        renderer_kind=(
            effective_renderer.kind if effective_renderer is not None else None
        ),
        renderer_path=(
            str(effective_renderer.executable)
            if effective_renderer is not None
            else None
        ),
        renderer_version=(
            _renderer_version(effective_renderer, runner)
            if effective_renderer is not None
            else None
        ),
        ldraw_dir=effective_ldraw,
        library=_library_metadata(effective_ldraw),
    )


def write_render_info(report: ModelRenderReport, *, out_dir: Path) -> Path:
    """Write the render-info sidecar next to the outputs."""
    from legolization.bundle.paths import numbered_file  # noqa: PLC0415
    from legolization.eval_artifacts import atomic_json  # noqa: PLC0415

    path = numbered_file(out_dir / f"{report.model.stem}.render-info.json")
    atomic_json(path, report.to_dict())
    return path


def _renderer_version(renderer: Renderer, runner: Runner | None) -> str | None:
    import subprocess  # noqa: PLC0415

    if runner is not None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed renderer executable
            [str(renderer.executable), "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    return output.splitlines()[0] if output else None


def _library_metadata(ldraw_dir: Path | None) -> dict[str, Any] | None:
    from legolization.parts_library import (  # noqa: PLC0415
        library_dir,
        load_metadata,
        managed_root,
    )

    if ldraw_dir is None:
        return None
    root = managed_root()
    if library_dir(root) == ldraw_dir and (metadata := load_metadata(root)):
        return metadata.to_dict()
    return {"path": str(ldraw_dir)}
