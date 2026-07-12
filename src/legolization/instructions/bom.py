"""Bill of materials: total and per-step part counts as JSON or text.

Hand-rolled rather than pyldraw3's ``ldraw.bom`` — that module expects
``Colour`` objects and a loaded parts catalog, while everything needed here
already lives on the layout: part keys, LDraw ids, colour codes, masses.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from legolization.color import default_palette

if TYPE_CHECKING:
    from legolization.instructions.sequencer import InstructionPlan
    from legolization.layout import Layout


@dataclass(frozen=True, slots=True)
class BomEntry:
    """One part/colour line of the bill of materials."""

    part_key: str
    ldraw_part: str
    colour_code: int
    colour_name: str
    quantity: int
    mass_g: float


@dataclass(frozen=True, slots=True)
class BillOfMaterials:
    """Total parts list plus optional per-step callouts."""

    total: tuple[BomEntry, ...]
    per_step: tuple[tuple[BomEntry, ...], ...] = ()

    @property
    def brick_count(self) -> int:
        """Total number of parts."""
        return sum(entry.quantity for entry in self.total)

    @property
    def mass_g(self) -> float:
        """Total mass in grams."""
        return sum(entry.mass_g for entry in self.total)

    def to_json(self, *, model_name: str = "") -> str:
        """Serialize as the documented JSON schema."""
        payload = {
            "model": model_name,
            "brick_count": self.brick_count,
            "mass_g": round(self.mass_g, 3),
            "total": [_entry_json(entry) for entry in self.total],
            "steps": [
                {
                    "step": index + 1,
                    "brick_count": sum(entry.quantity for entry in entries),
                    "parts": [_entry_json(entry) for entry in entries],
                }
                for index, entries in enumerate(self.per_step)
            ],
        }
        return json.dumps(payload, indent=2)

    def to_text(self) -> str:
        """Render a human-readable parts list."""
        lines = [f"{'qty':>4}  {'part':<12} {'ldraw':<8} colour"]
        lines.extend(
            f"{entry.quantity:>4}  {entry.part_key:<12} "
            f"{entry.ldraw_part:<8} {entry.colour_name}"
            for entry in self.total
        )
        for index, entries in enumerate(self.per_step):
            callout = ", ".join(
                f"{entry.quantity} x {entry.ldraw_part} {entry.colour_name}"
                for entry in entries
            )
            lines.append(f"Step {index + 1}: {callout}")
        return "\n".join(lines)


def bill_of_materials(
    layout: Layout,
    *,
    plan: InstructionPlan | None = None,
) -> BillOfMaterials:
    """Group the layout's parts by (part, colour); add per-step callouts."""
    total = _entries(layout, layout.bricks)
    per_step: tuple[tuple[BomEntry, ...], ...] = ()
    if plan is not None:
        per_step = tuple(
            _entries(layout, dict.fromkeys(step.brick_ids)) for step in plan.steps
        )
    return BillOfMaterials(total=total, per_step=per_step)


def _entries(layout: Layout, brick_ids: dict) -> tuple[BomEntry, ...]:
    palette = default_palette()
    counts: Counter[tuple[str, int]] = Counter()
    for brick_id in brick_ids:
        brick = layout.bricks[brick_id]
        counts[(brick.part_key, brick.colour_code)] += 1
    entries = []
    for (part_key, colour_code), quantity in sorted(counts.items()):
        part = layout.catalog[part_key]
        try:
            colour_name = palette.name_of(colour_code)
        except ValueError:
            colour_name = f"code_{colour_code}"
        entries.append(
            BomEntry(
                part_key=part_key,
                ldraw_part=part.ldraw_part,
                colour_code=colour_code,
                colour_name=colour_name,
                quantity=quantity,
                mass_g=round(quantity * part.mass_g, 3),
            )
        )
    return tuple(entries)


def _entry_json(entry: BomEntry) -> dict:
    return {
        "part_key": entry.part_key,
        "ldraw_part": entry.ldraw_part,
        "colour_code": entry.colour_code,
        "colour_name": entry.colour_name,
        "quantity": entry.quantity,
        "mass_g": entry.mass_g,
    }
