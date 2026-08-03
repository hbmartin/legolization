"""Canonical placement-template instantiation for repeated voxel components."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from legolization.catalog import rotate_offset
from legolization.grid import IGNORE
from legolization.layout import CollisionError
from legolization.template_cache import TemplateCache, TemplateCacheKey

if TYPE_CHECKING:
    from legolization.catalog import Cell
    from legolization.layout import Layout
    from legolization.repetition import ComponentInstance, RepeatedComponent

_ALGORITHM_VERSION = "placement-template-v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateContext:
    """Inputs that make a solved component template reusable and safe."""

    catalog_hash: str
    configuration_hash: str
    physics_profile: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateApplication:
    """Deterministic cache and instantiation provenance for one component."""

    component_signature: str
    key: str
    status: str
    instance_count: int


def instantiate_repeated_templates(
    layout: Layout,
    repeated: tuple[RepeatedComponent, ...],
    *,
    cache: TemplateCache | None,
    context: TemplateContext,
) -> tuple[TemplateApplication, ...]:
    """Make every repeated component use one canonical solved placement."""
    applications: list[TemplateApplication] = []
    for component in repeated:
        key = TemplateCacheKey(
            component_signature=component.signature,
            catalog_hash=context.catalog_hash,
            configuration_hash=context.configuration_hash,
            physics_profile=context.physics_profile,
            algorithm_version=_ALGORITHM_VERSION,
        )
        payload = cache.read(key) if cache is not None else None
        status = "hit" if payload is not None else "derived"
        if payload is not None and not _valid_payload(payload):
            if cache is None:  # payload can only come from a configured cache
                raise AssertionError
            cache.quarantine(key)
            payload = None
            status = "recovered"
        elif payload is not None and not _apply(layout, component, payload):
            # The payload is structurally valid; a collision or local coverage
            # mismatch says nothing about its reuse in another model.
            payload = None
            status = "local_miss"
        if payload is None:
            payload = _derive(layout, component)
            if payload is None or not _apply(layout, component, payload):
                applications.append(
                    TemplateApplication(
                        component_signature=component.signature,
                        key=key.digest,
                        status="skipped",
                        instance_count=len(component.instances),
                    )
                )
                continue
            if cache is not None:
                cache.write(key, payload)
                status = "write" if status == "derived" else status
        applications.append(
            TemplateApplication(
                component_signature=component.signature,
                key=key.digest,
                status=status,
                instance_count=len(component.instances),
            )
        )
    return tuple(applications)


def _derive(
    layout: Layout,
    component: RepeatedComponent,
) -> dict[str, Any] | None:
    source = component.instances[0]
    bricks = sorted(
        (
            brick
            for brick in layout
            if set(layout.filled_cells_of(brick)) <= source.cells
        ),
        key=lambda item: item.brick_id,
    )
    if not bricks:
        return None
    canonical_labels = {
        (x, y, z): label for x, y, z, label in component.canonical_cells
    }
    placements: list[dict[str, Any]] = []
    for brick in bricks:
        normalized_cells = {
            _to_canonical(cell, source) for cell in layout.filled_cells_of(brick)
        }
        labels = {canonical_labels[cell] for cell in normalized_cells}
        if len(labels) != 1:
            return None
        anchor = _to_canonical((brick.x, brick.y, brick.layer), source)
        placements.append(
            {
                "anchor": list(anchor),
                "colour_label": labels.pop(),
                "offset_ldu": list(
                    rotate_offset(brick.offset_ldu, source.canonical_yaw)
                ),
                "part_key": brick.part_key,
                "yaw": (brick.yaw + source.canonical_yaw) % 360,
            }
        )
    return {
        "placements": sorted(
            placements,
            key=lambda item: (
                item["part_key"],
                item["anchor"],
                item["yaw"],
                item["colour_label"],
                item["offset_ldu"],
            ),
        )
    }


def _apply(
    layout: Layout,
    component: RepeatedComponent,
    payload: dict[str, Any],
) -> bool:
    placements = payload.get("placements")
    if not isinstance(placements, list) or not placements:
        return False
    target_cells = _component_cells(component)
    remove_ids = _component_brick_ids(layout, target_cells)
    if _filled_by(layout, remove_ids) != target_cells:
        return False
    trial = layout.copy()
    trial.remove_many(remove_ids)
    if not _instantiate_all(trial, component, placements):
        return False
    if _target_filled(trial, target_cells) != target_cells:
        return False
    layout.replace_with(trial)
    return True


def _valid_payload(payload: dict[str, Any]) -> bool:
    """Return whether a cache payload has the complete placement shape."""
    placements = payload.get("placements")
    if not isinstance(placements, list) or not placements:
        return False
    try:
        for raw in placements:
            if not isinstance(raw, dict):
                return False
            _cell(raw.get("anchor"), label="anchor")
            _cell(raw.get("offset_ldu"), label="offset_ldu")
            int(raw["colour_label"])
            int(raw["yaw"])
            if not isinstance(raw["part_key"], str):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _component_cells(component: RepeatedComponent) -> frozenset[Cell]:
    return frozenset(cell for item in component.instances for cell in item.cells)


def _component_brick_ids(layout: Layout, cells: frozenset[Cell]) -> list[int]:
    return [
        brick.brick_id
        for brick in layout
        if set(layout.filled_cells_of(brick)) <= cells
    ]


def _filled_by(layout: Layout, brick_ids: list[int]) -> frozenset[Cell]:
    return frozenset(
        cell
        for brick_id in brick_ids
        for cell in layout.filled_cells_of(layout.bricks[brick_id])
    )


def _instantiate_all(
    layout: Layout,
    component: RepeatedComponent,
    placements: list[Any],
) -> bool:
    try:
        for instance in component.instances:
            _instantiate(layout, instance, placements)
    except (CollisionError, KeyError, TypeError, ValueError):
        return False
    return True


def _target_filled(layout: Layout, target: frozenset[Cell]) -> frozenset[Cell]:
    return frozenset(
        cell
        for brick in layout
        for cell in layout.filled_cells_of(brick)
        if cell in target
    )


def _instantiate(
    layout: Layout,
    instance: ComponentInstance,
    placements: list[Any],
) -> None:
    colours = dict(instance.colour_mapping)
    for raw in placements:
        if not isinstance(raw, dict):
            msg = "template placement must be an object"
            raise TypeError(msg)
        anchor = _cell(raw.get("anchor"), label="anchor")
        offset = _cell(raw.get("offset_ldu"), label="offset_ldu")
        label = int(raw["colour_label"])
        canonical_yaw = int(raw["yaw"])
        world_anchor = _from_canonical(anchor, instance)
        world_offset = rotate_offset(offset, (-instance.canonical_yaw) % 360)
        colour = int(colours[label])
        layout.add(
            part_key=str(raw["part_key"]),
            x=world_anchor[0],
            y=world_anchor[1],
            layer=world_anchor[2],
            yaw=(canonical_yaw - instance.canonical_yaw) % 360,
            colour_code=7 if colour == IGNORE else colour,
            offset_ldu=world_offset,
        )


def _to_canonical(cell: Cell, instance: ComponentInstance) -> Cell:
    rotated = rotate_offset(cell, instance.canonical_yaw)
    return cast(
        "Cell",
        tuple(rotated[axis] - instance.origin[axis] for axis in range(3)),
    )


def _from_canonical(cell: Cell, instance: ComponentInstance) -> Cell:
    rotated_world = cast(
        "Cell",
        tuple(cell[axis] + instance.origin[axis] for axis in range(3)),
    )
    return rotate_offset(rotated_world, (-instance.canonical_yaw) % 360)


def _cell(value: object, *, label: str) -> Cell:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        )
    ):
        msg = f"template {label} must contain three integers"
        raise TypeError(msg)
    return cast("Cell", tuple(value))


__all__ = [
    "TemplateApplication",
    "TemplateContext",
    "instantiate_repeated_templates",
]
