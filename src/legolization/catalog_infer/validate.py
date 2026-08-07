"""The five activation gates for a catalog extension.

A drafted or hand-written extension becomes eligible for automatic
overlay activation only after every gate passes:

- **import** — ``Catalog.load(require_explicit_nonrect=True)``, the
  builtin merge (``with_extensions``), decode-ambiguity validation, and
  strict estimate-sidecar parsing;
- **round-trip** — each part placed in a minimal :class:`Layout`,
  written through ``ldraw_out.write_model``, imported back through
  ``ldraw_in``, and decoded to the same key and world cells;
- **collision** — the exact collision boxes cover every occupied cell
  with no degenerate or stray volumes;
- **connector** — every vertical connector mates with a reference brick
  above/below through the physical connector machinery;
- **topology** — a two-part assembly forms one component of the
  connection graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Literal

from legolization.catalog import (
    CATALOG_VALIDATION_SCHEMA,
    DOWN,
    UP,
    Catalog,
    Part,
    default_catalog,
)
from legolization.layout import CollisionError, Layout
from legolization.physical import LduBox, box_for_cell

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from legolization.catalog import Cell

GATE_NAMES: tuple[str, ...] = (
    "import",
    "round-trip",
    "collision",
    "connector",
    "topology",
)

type GateStatus = Literal["passed", "failed", "skipped"]

_REFERENCE_PART = "plate_1x1"
_ROUND_TRIP_COLOUR = 4


@dataclass(frozen=True, slots=True, kw_only=True)
class GateResult:
    """One gate's outcome with a human-readable detail line."""

    gate: str
    status: GateStatus
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON payload for this gate."""
        return {"gate": self.gate, "status": self.status, "detail": self.detail}


def all_gates_passed(gates: Iterable[GateResult]) -> bool:
    """Whether every gate ran and passed (skips block activation)."""
    materialized = tuple(gates)
    return bool(materialized) and all(gate.status == "passed" for gate in materialized)


def validate_extension(
    extension_path: Path,
    sidecars: Sequence[Path] = (),
) -> tuple[GateResult, ...]:
    """Run the five activation gates over one extension document."""
    import_result, merged, extension = _import_gate(extension_path, sidecars)
    if merged is None or extension is None:
        skipped = tuple(
            GateResult(gate=name, status="skipped", detail="import gate failed")
            for name in GATE_NAMES[1:]
        )
        return (import_result, *skipped)
    parts = tuple(extension.parts.values())
    return (
        import_result,
        _part_gate("round-trip", parts, lambda part: _round_trip(merged, part)),
        _part_gate("collision", parts, _collision_check),
        _part_gate("connector", parts, lambda part: _connector_check(merged, part)),
        _part_gate("topology", parts, lambda part: _topology_check(merged, part)),
    )


def _import_gate(
    extension_path: Path,
    sidecars: Sequence[Path],
) -> tuple[GateResult, Catalog | None, Catalog | None]:
    from legolization.catalog import load_estimate_sidecar  # noqa: PLC0415
    from legolization.errors import ConfigurationError  # noqa: PLC0415

    try:
        extension = Catalog.load(extension_path, require_explicit_nonrect=True)
        merged = default_catalog().with_extensions([extension_path])
        for sidecar in sidecars:
            load_estimate_sidecar(sidecar)
    except (ConfigurationError, OSError, TypeError, ValueError) as error:
        failed = GateResult(gate="import", status="failed", detail=str(error))
        return failed, None, None
    keys = ", ".join(sorted(extension.parts))
    passed = GateResult(
        gate="import",
        status="passed",
        detail=f"extension merges cleanly with unambiguous decode ({keys})",
    )
    return passed, merged, extension


def _part_gate(
    gate: str,
    parts: tuple[Part, ...],
    check: Callable[[Part], str | None],
) -> GateResult:
    """Run one per-part check, failing the gate on the first problem."""
    for part in parts:
        if (problem := check(part)) is not None:
            return GateResult(
                gate=gate,
                status="failed",
                detail=f"{part.key}: {problem}",
            )
    keys = ", ".join(part.key for part in parts)
    return GateResult(gate=gate, status="passed", detail=f"verified {keys}")


# --- round-trip -----------------------------------------------------------


def _round_trip(merged: Catalog, part: Part) -> str | None:
    from legolization.ldraw_in import (  # noqa: PLC0415
        LdrawImportError,
        layout_from_ldraw,
    )
    from legolization.ldraw_out import write_model  # noqa: PLC0415

    layout = Layout(catalog=merged)
    try:
        placed = layout.add(part.key, 0, 0, 0, _gate_yaw(part), _ROUND_TRIP_COLOUR)
    except CollisionError as error:
        return f"cannot place in an empty layout: {error}"
    expected = frozenset(layout.cells_of(placed))
    with TemporaryDirectory(prefix="legolization-roundtrip-") as scratch:
        model_path = Path(scratch) / "round-trip.ldr"
        write_model(layout, model_path)
        try:
            imported = layout_from_ldraw(model_path, catalog=merged)
        except (LdrawImportError, ValueError) as error:
            return f"model did not import back: {error}"
    bricks = list(imported)
    if len(bricks) != 1:
        return f"expected one imported brick, found {len(bricks)}"
    decoded = bricks[0]
    if decoded.part_key != part.key:
        return f"decoded back as {decoded.part_key!r}, not {part.key!r}"
    if (actual := frozenset(imported.cells_of(decoded))) != expected:
        return (
            f"decoded cells differ: expected {sorted(expected)}, got {sorted(actual)}"
        )
    return None


# --- collision ------------------------------------------------------------


def _collision_check(part: Part) -> str | None:
    boxes = part.collision_boxes_ldu
    if not boxes:
        return "no collision boxes"
    occupied = tuple(box_for_cell(cell) for cell in sorted(part.occupied_cells))
    for box in boxes:
        if not any(box.intersects(cell_box) for cell_box in occupied):
            return (
                f"collision box {box.minimum}..{box.maximum} sits outside "
                "every occupied cell"
            )
    for cell in sorted(part.occupied_cells):
        if not _box_covered(box_for_cell(cell), boxes):
            return f"occupied cell {cell} is not covered by the collision boxes"
    return None


def _box_covered(target: LduBox, boxes: tuple[LduBox, ...]) -> bool:
    """Exact axis-aligned coverage via elementary sub-box sweeping."""
    # Exhaustive sweep-coverage arithmetic stays in one place.
    # lizard forgives(cyclomatic_complexity)
    clipped = [box for box in boxes if box.intersects(target)]
    if not clipped:
        return False
    breakpoints = tuple(
        sorted(
            {target.minimum[axis], target.maximum[axis]}
            | {
                bound
                for box in clipped
                for bound in (box.minimum[axis], box.maximum[axis])
                if target.minimum[axis] < bound < target.maximum[axis]
            }
        )
        for axis in range(3)
    )
    centers = tuple(
        [(low + high) / 2 for low, high in pairwise(edges)] for edges in breakpoints
    )
    return all(
        any(
            all(
                box.minimum[axis] <= point[axis] < box.maximum[axis]
                for axis in range(3)
            )
            for box in clipped
        )
        for point in (
            (x, y, z) for x in centers[0] for y in centers[1] for z in centers[2]
        )
    )


# --- connector ------------------------------------------------------------


def _gate_yaw(part: Part) -> int:
    """Return the gate placement yaw (0 whenever the part allows it)."""
    return 0 if 0 in part.orientations else part.orientations[0]


def _world_connector_cells(
    part: Part,
    *,
    layer: int,
    top: bool,
) -> tuple[Cell, ...]:
    """World cells of the vertical connectors at the gate placement."""
    wanted = UP if top else DOWN
    return tuple(
        connector.cell
        for connector in part.connectors_at(0, 0, layer, _gate_yaw(part), top=top)
        if connector.direction == wanted
    )


def _connector_check(merged: Catalog, part: Part) -> str | None:
    has_top = bool(_world_connector_cells(part, layer=0, top=True))
    has_bottom = bool(_world_connector_cells(part, layer=0, top=False))
    if not has_top and not has_bottom:
        return "part has no vertical connectors; nothing can mate with it"
    if has_top and (problem := _mate_above(merged, part)):
        return problem
    if has_bottom and (problem := _mate_below(merged, part)):
        return problem
    return None


def _mate_above(merged: Catalog, part: Part) -> str | None:
    """Seat a reference plate on every stud and count the mates."""
    from legolization.graph import GROUND_ID, ConnectionGraph  # noqa: PLC0415

    layout = Layout(catalog=merged)
    placed = layout.add(part.key, 0, 0, 0, _gate_yaw(part), _ROUND_TRIP_COLOUR)
    seated = 0
    for dx, dy, dz in _world_connector_cells(part, layer=0, top=True):
        try:
            layout.add(_REFERENCE_PART, dx, dy, dz + 1, 0, _ROUND_TRIP_COLOUR)
        except CollisionError:
            return f"reference brick cannot seat on the stud at {(dx, dy, dz)}"
        seated += 1
    graph = ConnectionGraph.from_layout(layout)
    mated = sum(
        contact.below_id == placed.brick_id
        for contact in graph.knob_contacts
        if contact.below_id != GROUND_ID
    )
    if mated < seated:
        return f"only {mated} of {seated} top studs mate with a reference brick above"
    return None


def _mate_below(merged: Catalog, part: Part) -> str | None:
    """Support every anti-stud on a reference plate and count the mates."""
    from legolization.graph import GROUND_ID, ConnectionGraph  # noqa: PLC0415

    layout = Layout(catalog=merged)
    placed = layout.add(part.key, 0, 0, 1, _gate_yaw(part), _ROUND_TRIP_COLOUR)
    supported = 0
    for dx, dy, dz in _world_connector_cells(part, layer=1, top=False):
        try:
            layout.add(_REFERENCE_PART, dx, dy, dz - 1, 0, _ROUND_TRIP_COLOUR)
        except CollisionError:
            return f"reference brick cannot support the anti-stud at {(dx, dy, dz)}"
        supported += 1
    graph = ConnectionGraph.from_layout(layout)
    mated = sum(
        contact.above_id == placed.brick_id
        for contact in graph.knob_contacts
        if contact.below_id != GROUND_ID
    )
    if mated < supported:
        return (
            f"only {mated} of {supported} anti-studs mate with a reference brick below"
        )
    return None


# --- topology -------------------------------------------------------------


def _topology_check(merged: Catalog, part: Part) -> str | None:
    from legolization.graph import ConnectionGraph  # noqa: PLC0415

    layout = Layout(catalog=merged)
    try:
        if top_cells := _world_connector_cells(part, layer=0, top=True):
            layout.add(part.key, 0, 0, 0, _gate_yaw(part), _ROUND_TRIP_COLOUR)
            dx, dy, dz = top_cells[0]
            layout.add(_REFERENCE_PART, dx, dy, dz + 1, 0, _ROUND_TRIP_COLOUR)
        elif bottom_cells := _world_connector_cells(part, layer=1, top=False):
            layout.add(part.key, 0, 0, 1, _gate_yaw(part), _ROUND_TRIP_COLOUR)
            dx, dy, dz = bottom_cells[0]
            layout.add(_REFERENCE_PART, dx, dy, dz - 1, 0, _ROUND_TRIP_COLOUR)
        else:
            return "part has no vertical connectors; no two-part assembly exists"
    except CollisionError as error:
        return f"two-part assembly cannot be built: {error}"
    graph = ConnectionGraph.from_layout(layout)
    if (components := graph.component_count()) != 1:
        return f"two-part assembly splits into {components} components"
    return None


def validation_payload(
    gates: Iterable[GateResult],
    *,
    extension: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return the ``validation.json`` document for one gate run."""
    return {
        "schema": CATALOG_VALIDATION_SCHEMA,
        "extension": extension,
        "generated_at": generated_at,
        "gates": [gate.to_dict() for gate in gates],
    }
