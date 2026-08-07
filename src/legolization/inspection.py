"""Native source-model inspection behind ``legolization input inspect``.

Inspection reports recommended generation settings and machine-readable
conditions (ambiguous up-axis, absent colour data, ...) without ever
prompting: the conversational skill asks the questions, the CLI only
reports. Only the five native formats are accepted — the CLI is not a
general converter, and ``.ldr``/``.mpd`` assemblies route through
``bundle`` or ``analyze``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

import numpy as np
import trimesh

from legolization.errors import ConfigurationError
from legolization.grid import VoxelGrid
from legolization.mesh import (
    MESH_SUFFIXES,
    MeshOptions,
    VoxelizationRecommendation,
    has_colour_data,
    recommend_voxelization,
)

if TYPE_CHECKING:
    from pathlib import Path

VOXEL_SUFFIXES = frozenset({".vox", ".npy"})
NATIVE_SUFFIXES = VOXEL_SUFFIXES | MESH_SUFFIXES
_LDRAW_SUFFIXES = frozenset({".ldr", ".mpd"})

_UP_AXES: tuple[Literal["x", "y", "z"], ...] = ("x", "y", "z")
_UP_CONFIDENCE_MARGIN = 0.15
_UP_FORMAT_PRIOR = 0.05
_BASE_NORMAL_ALIGNMENT = 0.9
_BASE_BAND_FRACTION = 0.05
_COM_HEIGHT_WEIGHT = 0.5
_MAX_PRACTICAL_PLATE_LAYERS = 400
_COLOUR_SUMMARY_TOP = 5

type UpAxis = Literal["x", "y", "z"]
type UpConfidence = Literal["high", "ambiguous"]


@dataclass(frozen=True, slots=True, kw_only=True)
class UpAxisReport:
    """The orientation classifier's recommendation and evidence."""

    recommended: UpAxis
    confidence: UpConfidence
    scores: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this report."""
        return {
            "recommended": self.recommended,
            "confidence": self.confidence,
            "scores": {axis: round(score, 4) for axis, score in self.scores.items()},
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class VoxelReport:
    """Inspection facts for a ``.vox``/``.npy`` input."""

    grid_shape: tuple[int, int, int]
    filled_count: int
    colour_codes: dict[int, float]
    recommended_plates_per_voxel: int

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this report."""
        return {
            "grid_shape": list(self.grid_shape),
            "filled_count": self.filled_count,
            "colour_codes": {
                str(code): round(share, 4)
                for code, share in sorted(self.colour_codes.items())
            },
            "recommended_plates_per_voxel": self.recommended_plates_per_voxel,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MeshReport:
    """Inspection facts for an ``.obj``/``.stl``/``.ply`` input."""

    triangle_count: int
    watertight: bool
    component_count: int
    extents: tuple[float, float, float]
    up: UpAxisReport
    recommendation: VoxelizationRecommendation
    has_colour_data: bool
    colour_summary: dict[int, float]

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this report."""
        return {
            "triangle_count": self.triangle_count,
            "watertight": self.watertight,
            "component_count": self.component_count,
            "extents": [round(value, 6) for value in self.extents],
            "up": self.up.to_dict(),
            "recommendation": self.recommendation.to_dict(),
            "has_colour_data": self.has_colour_data,
            "colour_summary": {
                str(code): round(share, 4)
                for code, share in sorted(self.colour_summary.items())
            },
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class InputInspection:
    """The complete machine-readable inspection of one native input."""

    filename: str
    format: str
    kind: Literal["voxel", "mesh"]
    sha256: str
    warnings: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    voxel: VoxelReport | None = None
    mesh: MeshReport | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this inspection."""
        payload: dict[str, object] = {
            "schema": "legolization.input-inspection/v1",
            "filename": self.filename,
            "format": self.format,
            "kind": self.kind,
            "sha256": self.sha256,
            "warnings": list(self.warnings),
            "conditions": list(self.conditions),
        }
        if self.voxel is not None:
            payload["voxel"] = self.voxel.to_dict()
        if self.mesh is not None:
            payload["mesh"] = self.mesh.to_dict()
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedOutput:
    """Paths written by the normalization option."""

    directory: Path
    npy_path: Path
    sidecar_path: Path


@dataclass(slots=True, kw_only=True)
class _MeshInspectionState:
    """Working products shared between inspection and normalization."""

    mesh: trimesh.Trimesh
    report: MeshReport
    warnings: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)


def inspect_input(
    path: Path,
    *,
    up: UpAxis | None = None,
    target_studs: int | None = None,
    auto_scale: tuple[int, int] | None = None,
) -> InputInspection:
    """Inspect one native source model without prompting or converting."""
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415

    suffix = _validated_suffix(path)
    sha256 = input_sha256(path)
    if suffix in VOXEL_SUFFIXES:
        return _inspect_voxel(path, suffix=suffix, sha256=sha256)
    state = _inspect_mesh(
        path,
        up=up,
        target_studs=target_studs,
        auto_scale=auto_scale,
    )
    return InputInspection(
        filename=path.name,
        format=suffix.lstrip("."),
        kind="mesh",
        sha256=sha256,
        warnings=tuple(state.warnings),
        conditions=tuple(state.conditions),
        mesh=state.report,
    )


def classify_up_axis(
    mesh: trimesh.Trimesh,
    *,
    prior: UpAxis | None = None,
) -> UpAxisReport:
    """Score candidate up-axes by base support and centre-of-mass height.

    The classifier is deliberately biased toward ``ambiguous``: a wrong
    auto-accepted axis is expensive, an extra question is cheap.
    """
    scores: dict[str, float] = {}
    for axis in _UP_AXES:
        scores[axis] = _axis_score(mesh, axis)
        if prior == axis:
            scores[axis] += _UP_FORMAT_PRIOR
    ranked = sorted(_UP_AXES, key=lambda axis: scores[axis], reverse=True)
    margin = scores[ranked[0]] - scores[ranked[1]]
    return UpAxisReport(
        recommended=ranked[0],
        confidence="high" if margin >= _UP_CONFIDENCE_MARGIN else "ambiguous",
        scores=scores,
    )


def recommend_plates_per_voxel(shape: tuple[int, int, int]) -> int:
    """Suggest a vertical resolution keeping total plate height practical."""
    for plates in (3, 2):
        if shape[2] * plates <= _MAX_PRACTICAL_PLATE_LAYERS:
            return plates
    return 1


def write_normalized(
    path: Path,
    *,
    out_dir: Path | None = None,
    up: UpAxis | None = None,
    target_studs: int | None = None,
    auto_scale: tuple[int, int] | None = None,
) -> tuple[InputInspection, NormalizedOutput]:
    """Write the ``-prepared`` sibling bundle with a normalized grid.

    The ``.npy`` holds int16 colour codes round-trippable through
    :meth:`VoxelGrid.from_npy`; the JSON sidecar records scale,
    orientation, hashes, and warnings.
    """
    from legolization.bundle.paths import (  # noqa: PLC0415
        default_bundle_dir,
        numbered_sibling,
    )
    from legolization.eval_artifacts import atomic_json, input_sha256  # noqa: PLC0415

    inspection = inspect_input(
        path,
        up=up,
        target_studs=target_studs,
        auto_scale=auto_scale,
    )
    grid = _normalized_grid(path, inspection)
    directory = (
        numbered_sibling(default_bundle_dir(path, "prepared"))
        if out_dir is None
        else out_dir
    )
    directory.mkdir(parents=True, exist_ok=True)
    npy_path = directory / "normalized.npy"
    np.save(npy_path, grid.codes)
    sidecar_path = directory / "normalized.json"
    atomic_json(
        sidecar_path, _sidecar_payload(inspection, npy_hash=input_sha256(npy_path))
    )
    _write_prepared_record(path, inspection, directory=directory)
    return inspection, NormalizedOutput(
        directory=directory,
        npy_path=npy_path,
        sidecar_path=sidecar_path,
    )


def _validated_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _LDRAW_SUFFIXES:
        msg = (
            f"{path.name} is an LDraw assembly, not a native source model; "
            "use 'legolization bundle' (preserve or --retile) or "
            "'legolization analyze' instead"
        )
        raise ConfigurationError(msg)
    if suffix not in NATIVE_SUFFIXES:
        msg = (
            f"unsupported input {path.name}: expected one of "
            f"{', '.join(sorted(NATIVE_SUFFIXES))}; the CLI is not a "
            "general 2D/3D converter — export a mesh or voxel model first"
        )
        raise ConfigurationError(msg)
    if not path.is_file():
        msg = f"input must be an existing file: {path}"
        raise ConfigurationError(msg)
    return suffix


def _inspect_voxel(path: Path, *, suffix: str, sha256: str) -> InputInspection:
    grid = (
        VoxelGrid.from_vox(path, plates_per_voxel=1)
        if suffix == ".vox"
        else VoxelGrid.from_npy(path, plates_per_voxel=1)
    )
    shape = cast("tuple[int, int, int]", tuple(int(v) for v in grid.codes.shape))
    filled = grid.filled_mask
    filled_count = int(filled.sum())
    conditions: list[str] = []
    if filled_count == 0:
        conditions.append("empty-grid")
    codes, counts = np.unique(grid.codes[filled], return_counts=True)
    shares = {
        int(code): float(count) / filled_count if filled_count else 0.0
        for code, count in zip(codes, counts, strict=True)
    }
    return InputInspection(
        filename=path.name,
        format=suffix.lstrip("."),
        kind="voxel",
        sha256=sha256,
        conditions=tuple(conditions),
        voxel=VoxelReport(
            grid_shape=shape,
            filled_count=filled_count,
            colour_codes=shares,
            recommended_plates_per_voxel=recommend_plates_per_voxel(shape),
        ),
    )


def _inspect_mesh(
    path: Path,
    *,
    up: UpAxis | None,
    target_studs: int | None,
    auto_scale: tuple[int, int] | None,
) -> _MeshInspectionState:
    mesh = cast("trimesh.Trimesh", trimesh.load(path, force="mesh"))
    warnings: list[str] = []
    conditions: list[str] = []
    prior: UpAxis = "y" if path.suffix.lower() == ".obj" else "z"
    up_report = classify_up_axis(mesh, prior=prior)
    if up is not None:
        effective_up: UpAxis = up
    elif up_report.confidence == "high":
        effective_up = up_report.recommended
    else:
        effective_up = prior
        conditions.append("ambiguous-up-axis")
    if not mesh.is_watertight:
        conditions.append("not-watertight")
    component_count = len(mesh.split(only_watertight=False)) or 1
    if component_count > 1:
        conditions.append("multiple-components")
        warnings.append(f"{component_count} disconnected components are all preserved")
    coloured = has_colour_data(mesh)
    if not coloured:
        conditions.append("no-colour-data")
    options = _mesh_options(
        up=effective_up,
        target_studs=target_studs,
        auto_scale=auto_scale,
        sampled=coloured,
    )
    recommendation = recommend_voxelization(mesh, options)
    report = MeshReport(
        triangle_count=len(mesh.faces),
        watertight=bool(mesh.is_watertight),
        component_count=component_count,
        extents=cast(
            "tuple[float, float, float]",
            tuple(float(value) for value in mesh.extents),
        ),
        up=up_report,
        recommendation=recommendation,
        has_colour_data=coloured,
        colour_summary=_colour_summary(mesh) if coloured else {},
    )
    return _MeshInspectionState(
        mesh=mesh,
        report=report,
        warnings=warnings,
        conditions=conditions,
    )


def _mesh_options(
    *,
    up: UpAxis,
    target_studs: int | None,
    auto_scale: tuple[int, int] | None,
    sampled: bool,
) -> MeshOptions:
    colour_mode: Literal["uniform", "sampled"] = "sampled" if sampled else "uniform"
    if target_studs is not None:
        return MeshOptions(
            up=up,
            target_studs=target_studs,
            grid_phases=8,
            colour_mode=colour_mode,
        )
    return MeshOptions(
        up=up,
        auto_scale=auto_scale or (16, 64),
        grid_phases=8,
        colour_mode=colour_mode,
    )


def _axis_score(mesh: trimesh.Trimesh, axis: UpAxis) -> float:
    from legolization.mesh import _orient_z_up  # noqa: PLC0415

    oriented = _orient_z_up(mesh, axis)
    normals = np.asarray(oriented.face_normals)
    areas = np.asarray(oriented.area_faces)
    centroids = np.asarray(oriented.triangles_center)
    total_area = float(areas.sum()) or 1.0
    min_z = float(oriented.bounds[0][2])
    extent_z = float(oriented.extents[2]) or 1.0
    band = min_z + _BASE_BAND_FRACTION * extent_z
    base_faces = (normals[:, 2] < -_BASE_NORMAL_ALIGNMENT) & (centroids[:, 2] <= band)
    base_support = float(areas[base_faces].sum()) / total_area
    com_height = (float(oriented.center_mass[2]) - min_z) / extent_z
    return base_support - _COM_HEIGHT_WEIGHT * com_height


def _colour_summary(mesh: trimesh.Trimesh) -> dict[int, float]:
    from legolization.color import default_palette  # noqa: PLC0415
    from legolization.mesh import _vertex_colours  # noqa: PLC0415

    colours = _vertex_colours(mesh)
    if colours is None:
        return {}
    palette = default_palette()
    codes = palette.quantize(np.asarray(colours[:, :3], dtype=np.float64))
    counts = Counter(int(code) for code in codes)
    total = sum(counts.values()) or 1
    top = counts.most_common(_COLOUR_SUMMARY_TOP)
    return {code: count / total for code, count in top}


def _normalized_grid(path: Path, inspection: InputInspection) -> VoxelGrid:
    from legolization.mesh import mesh_to_grid  # noqa: PLC0415

    if inspection.voxel is not None:
        plates = inspection.voxel.recommended_plates_per_voxel
        if inspection.format == "vox":
            return VoxelGrid.from_vox(path, plates_per_voxel=plates)
        return VoxelGrid.from_npy(path, plates_per_voxel=plates)
    report = inspection.mesh
    if report is None:  # pragma: no cover - inspection always sets one side
        msg = "inspection carries neither voxel nor mesh report"
        raise ValueError(msg)
    options = MeshOptions(
        up=report.up.recommended if report.up.confidence == "high" else "z",
        target_studs=report.recommendation.target_studs,
        grid_phases=8,
        colour_mode="sampled" if report.has_colour_data else "uniform",
    )
    return mesh_to_grid(path, options=options)


def _sidecar_payload(
    inspection: InputInspection,
    *,
    npy_hash: str,
) -> dict[str, object]:
    from legolization.version import package_version  # noqa: PLC0415

    scale: dict[str, object]
    orientation: dict[str, object]
    colours: dict[str, object]
    if inspection.voxel is not None:
        scale = {
            "plates_per_voxel": inspection.voxel.recommended_plates_per_voxel,
        }
        orientation = {"up": "z", "confidence": "high"}
        colours = {
            "mode": "codes",
            "summary": {
                str(code): round(share, 4)
                for code, share in sorted(inspection.voxel.colour_codes.items())
            },
        }
    else:
        report = inspection.mesh
        assert report is not None  # noqa: S101 - inspection invariant
        scale = report.recommendation.to_dict()
        orientation = {
            "up": report.up.recommended,
            "confidence": report.up.confidence,
        }
        colours = {
            "mode": "sampled" if report.has_colour_data else "uniform",
            "summary": {
                str(code): round(share, 4)
                for code, share in sorted(report.colour_summary.items())
            },
        }
    return {
        "schema": "legolization.input-normalized/v1",
        "version": package_version(),
        "source": {"filename": inspection.filename, "sha256": inspection.sha256},
        "npy_sha256": npy_hash,
        "scale": scale,
        "orientation": orientation,
        "colours": colours,
        "warnings": list(inspection.warnings),
        "conditions": list(inspection.conditions),
    }


def _write_prepared_record(
    path: Path,
    inspection: InputInspection,
    *,
    directory: Path,
) -> None:
    from legolization.bundle.identity import BundleIdentity  # noqa: PLC0415
    from legolization.bundle.record import (  # noqa: PLC0415
        BundleRecord,
        source_payload,
        versions_payload,
        write_record,
    )
    from legolization.catalog import catalog_hash  # noqa: PLC0415
    from legolization.configuration import mapping_hash  # noqa: PLC0415
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415
    from legolization.version import package_version  # noqa: PLC0415

    values = {"operation": "input-inspect", "inspection": inspection.to_dict()}
    record = BundleRecord(
        identity=BundleIdentity(
            input_sha256=inspection.sha256,
            config_sha256=mapping_hash(values),
            legolization_version=package_version(),
            catalog_sha256=catalog_hash(),
        ),
        source=source_payload(path, bundle_dir=directory, sha256=inspection.sha256),
        configuration={"sha256": mapping_hash(values), "values": values},
        versions=versions_payload(),
        quality="direct",
        status="complete",
        exit_code=0,
    )
    for name in ("normalized.npy", "normalized.json"):
        record.record_artifact(
            path=name,
            stage="normalize",
            kind="normalized" if name.endswith(".npy") else "sidecar",
            sha256=input_sha256(directory / name),
        )
    record.stage("normalize").status = "complete"
    write_record(record, directory)
