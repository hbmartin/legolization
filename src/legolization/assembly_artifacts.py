"""Diagnostic graph, MPD, HTML, callout, and comparison artifacts."""

from __future__ import annotations

import html
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - used to materialize candidate paths
from typing import TYPE_CHECKING

from ldraw import ConnectionStatus
from ldraw.pieces import Piece

from legolization.assembly_paths import reachable_occurrences
from legolization.instructions.render import detect_ldraw_dir, detect_renderer

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ldraw.geometry import Vector

    from legolization.assembly import AssemblyAnalysisReport
    from legolization.assembly_connections import ConnectionAnalysis
    from legolization.assembly_model import AssemblyModel
    from legolization.assembly_physics import SupportResolution

_COMPONENT_COLOURS = (4, 1, 2, 14, 5, 13, 3, 6, 9, 10, 11, 12, 15, 7)


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    """Successfully written diagnostic paths and non-fatal warnings."""

    paths: dict[str, str]
    warnings: tuple[str, ...] = ()


def write_graph_json(
    connectivity: ConnectionAnalysis,
    path: Path,
) -> None:
    """Write the complete connector graph as deterministic JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(connectivity.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_component_mpd(
    model: AssemblyModel,
    connectivity: ConnectionAnalysis,
    path: Path,
) -> None:
    """Write a flattened MPD with confirmed components recolored."""
    component_of = {
        occurrence_id: index
        for index, component in enumerate(connectivity.confirmed_components)
        for occurrence_id in component
    }
    _write_flattened_mpd(
        model,
        path,
        occurrence_ids={item.occurrence_id for item in model.occurrences},
        colours={
            occurrence_id: _COMPONENT_COLOURS[index % len(_COMPONENT_COLOURS)]
            for occurrence_id, index in component_of.items()
        },
        title="confirmed connectivity components",
    )


def write_floating_mpd(
    model: AssemblyModel,
    connectivity: ConnectionAnalysis,
    support: SupportResolution,
    path: Path,
) -> None:
    """Write occurrences lacking a confirmed path to resolved support."""
    confirmed_edges = [
        edge.occurrence_ids
        for edge in connectivity.connections
        if edge.status is ConnectionStatus.CONFIRMED
    ]
    if support.occurrence_ids:
        reference = reachable_occurrences(confirmed_edges, support.occurrence_ids)
    else:
        reference_component: tuple[int, ...] = max(
            connectivity.confirmed_components,
            key=lambda component: (len(component), -component[0]),
            default=(),
        )
        reference = frozenset(reference_component)
    floating = {
        item.occurrence_id
        for item in model.occurrences
        if item.occurrence_id not in reference
    }
    _write_flattened_mpd(
        model,
        path,
        occurrence_ids=floating,
        colours=dict.fromkeys(floating, 4),
        title="floating or disconnected confirmed components",
    )


def write_html_report(report: AssemblyAnalysisReport, path: Path) -> None:
    """Write a self-contained human-readable assembly report."""
    payload = report.to_dict()
    input_path = str(report.input.get("path", ""))
    source_rows = []
    for item in report.occurrences:
        source = item.get("source", {})
        model = source.get("source_model")
        line = source.get("source_line")
        location = f"{model}:{line}" if model and line else model or "unknown"
        source_link = (
            f'<a href="{html.escape(input_path)}#L{line}">'
            f"{html.escape(str(location))}</a>"
            if input_path and line
            else html.escape(str(location))
        )
        source_rows.append(
            "<tr>"
            f"<td>{item['occurrence_id']}</td>"
            f"<td>{html.escape(str(item['part_id']))}</td>"
            f"<td>{source_link}</td>"
            f"<td>{html.escape(str(source.get('source_page')))}</td>"
            "</tr>"
        )
    serialized = html.escape(json.dumps(payload, indent=2, sort_keys=True))
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Assembly analysis: {html.escape(str(report.input.get("path", "model")))}</title>
<style>
body{{font:15px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:.35rem}}
pre{{background:#f6f8fa;padding:1rem;overflow:auto}}.bad{{color:#a00}}.ok{{color:#073}}
</style></head><body>
<h1>Geometry-first assembly analysis</h1>
<p>Status: <strong>{html.escape(report.status)}</strong>; verdict:
<strong>{html.escape(report.verdict)}</strong>; topology:
<strong>{html.escape(report.topology_verdict)}</strong>; physics:
<strong>{html.escape(report.physics_verdict)}</strong>.</p>
<h2>Occurrence provenance</h2>
<table><thead><tr><th>ID</th><th>Part</th><th>Source</th><th>Page</th></tr></thead>
<tbody>{"".join(source_rows)}</tbody></table>
<h2>Complete report</h2><pre>{serialized}</pre>
</body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def write_callout_svg(
    model: AssemblyModel,
    connectivity: ConnectionAnalysis,
    path: Path,
) -> None:
    """Write an isometric SVG circling unmatched connector endpoints."""
    unmatched = set(connectivity.unmatched_endpoints)
    endpoint_points = [
        endpoint.point_ldu
        for endpoint in connectivity.endpoints
        if endpoint.key in unmatched
    ]
    occurrence_points = [item.bounds_ldu.center for item in model.occurrences]
    projected = [_project(item) for item in occurrence_points]
    callouts = [_project(item) for item in endpoint_points[:100]]
    all_points = [*projected, *callouts] or [(0.0, 0.0)]
    min_x = min(item[0] for item in all_points)
    max_x = max(item[0] for item in all_points)
    min_y = min(item[1] for item in all_points)
    max_y = max(item[1] for item in all_points)
    width, height = 1_200.0, 800.0

    def screen(point: tuple[float, float]) -> tuple[float, float]:
        x = 40 + (point[0] - min_x) / max(max_x - min_x, 1) * (width - 80)
        y = 40 + (point[1] - min_y) / max(max_y - min_y, 1) * (height - 80)
        return x, y

    model_marks = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#667"/>'
        for x, y in (screen(item) for item in projected)
    )
    callout_marks = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" fill="none" '
        'stroke="#e22" stroke-width="3"/>'
        for x, y in (screen(item) for item in callouts)
    )
    document = f"""<svg xmlns="http://www.w3.org/2000/svg"
width="{width:g}" height="{height:g}">
<rect width="100%" height="100%" fill="white"/>
<text x="20" y="25" font-family="sans-serif" font-size="16">
Unmatched connector callouts</text>
{model_marks}{callout_marks}</svg>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def render_comparison(
    before: Path,
    after: Path,
    directory: Path,
) -> ArtifactWriteResult:
    """Render before/after snapshots when LeoCAD or LDView is available."""
    renderer = detect_renderer()
    if renderer is None:
        return ArtifactWriteResult(
            paths={},
            warnings=("no LDraw renderer found; comparison renders were skipped",),
        )
    directory.mkdir(parents=True, exist_ok=True)
    ldraw_dir = detect_ldraw_dir()
    paths: dict[str, str] = {}
    warnings: list[str] = []
    for label, model_path in (("before", before), ("after", after)):
        output = directory / f"{label}.png"
        if renderer.kind == "leocad":
            command = [
                str(renderer.executable),
                str(model_path),
                "-i",
                str(output),
                "-w",
                "1200",
                "-h",
                "900",
                "--camera-angles",
                "30",
                "45",
            ]
            if ldraw_dir is not None:
                command.extend(("-l", str(ldraw_dir)))
        else:
            command = [
                str(renderer.executable),
                str(model_path),
                f"-SaveSnapshot={output}",
                "-SaveWidth=1200",
                "-SaveHeight=900",
                "-DefaultLatLong=30,45",
                "-SaveAlpha=0",
            ]
            if ldraw_dir is not None:
                command.append(f"-LDrawDir={ldraw_dir}")
        completed: subprocess.CompletedProcess[bytes] | None = None
        try:
            completed = subprocess.run(  # noqa: S603 - executable was explicitly resolved
                command,
                check=False,
                capture_output=True,
                timeout=240,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            warnings.append(f"{label} comparison render failed: {error}")
        if output.is_file() and output.stat().st_size:
            paths[f"comparison_{label}"] = str(output)
        else:
            detail = ""
            if completed is not None:
                stderr = completed.stderr.decode(errors="replace").strip().splitlines()
                tail = stderr[-1] if stderr else "no stderr"
                detail = f" (exit {completed.returncode}: {tail})"
            warnings.append(f"{label} comparison renderer produced no image{detail}")
    return ArtifactWriteResult(paths=paths, warnings=tuple(warnings))


def _write_flattened_mpd(
    model: AssemblyModel,
    path: Path,
    *,
    occurrence_ids: set[int],
    colours: dict[int, int],
    title: str,
) -> None:
    lines = [
        f"0 FILE {path.name}",
        f"0 {title}",
        "0 Name: diagnostic flattened assembly",
        "0 Author: legolization",
        "0 !LDRAW_ORG Unofficial_Model",
    ]
    inspection = model.analysis.inspection
    if inspection is None:
        msg = "assembly model has no inspection geometry"
        raise ValueError(msg)
    geometry_by_id = {item.index + 1: item for item in inspection.occurrences}
    for occurrence_id in sorted(occurrence_ids):
        geometry = geometry_by_id.get(occurrence_id)
        if geometry is None:
            continue
        source = geometry.attribution
        model_name = source.model_path[-1] if source.model_path else "unknown"
        source_line = source.source_line_path[-1] if source.source_line_path else None
        page = source.page_path[-1] if source.page_path else None
        lines.append(
            f"0 // SOURCE occurrence={occurrence_id} model={model_name} "
            f"line={source_line} page={page}"
        )
        occurrence = geometry.occurrence
        lines.append(
            Piece.place(
                occurrence.reference,
                colour=colours.get(occurrence_id, occurrence.colour),
                position=occurrence.position,
                matrix=occurrence.matrix,
            ).to_ldraw()
        )
    lines.append("0 NOFILE")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _project(point: Vector) -> tuple[float, float]:
    x = float(point.x)
    y = float(point.y)
    z = float(point.z)
    return (x - z, (x + z) * 0.35 + y)


def artifact_paths_payload(paths: Iterable[Path]) -> dict[str, str]:
    """Return existing artifact paths keyed by their stem suffix."""
    return {path.stem: str(path) for path in paths if path.exists()}
