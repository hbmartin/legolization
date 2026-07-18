"""Connection graph: stud-cavity contacts, side contacts, connectivity.

Edges are directed "supports" relations (``below → above``) discovered by
matching top connectors (studs) against bottom connectors (anti-studs) one
layer up. Bricks whose bottom connectors sit at layer 0 connect to a virtual
ground node. Side contacts (shared vertical faces) don't join components but
feed horizontal press forces in the stability model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from legolization import telemetry

if TYPE_CHECKING:
    from legolization.layout import Layout

GROUND_ID = -1
"""Pseudo brick id for the ground plane in knob contacts."""


@dataclass(frozen=True, slots=True)
class KnobContact:
    """One mated stud: ``below_id`` (or ground) into ``above_id``.

    ``x``/``y`` are the knob's grid column; ``interface_layer`` is the plate
    layer of the mating plane (the bottom layer of the upper brick).
    """

    below_id: int
    above_id: int
    x: int
    y: int
    interface_layer: int


@dataclass(frozen=True, slots=True)
class SideContact:
    """Bricks ``a_id``/``b_id`` sharing vertical faces along ``axis``.

    ``direction`` is +1 if b lies at greater coordinate than a along the
    axis. ``centroid`` is the mean shared-face center as
    ``(x, y, layer)`` floats in grid units (layer in plates).
    ``z_lo``/``z_hi`` are the lowest and highest plate layers of the shared
    faces — the stability model presses at both vertical extremes so side
    forces can carry torque about the horizontal axes.
    """

    a_id: int
    b_id: int
    axis: int
    direction: int
    face_count: int
    centroid: tuple[float, float, float]
    z_lo: int
    z_hi: int


@dataclass(frozen=True, slots=True)
class ConnectionGraph:
    """All contacts of a layout plus component/grounding queries."""

    brick_ids: tuple[int, ...]
    knob_contacts: tuple[KnobContact, ...]
    side_contacts: tuple[SideContact, ...]
    grounded_ids: frozenset[int]

    @classmethod
    def from_layout(cls, layout: Layout) -> ConnectionGraph:
        """Extract all knob and side contacts from a layout."""
        with telemetry.span("graph.from_layout", n=len(layout)):
            return cls._from_layout_body(layout)

    @classmethod
    def _from_layout_body(cls, layout: Layout) -> ConnectionGraph:
        """Run the body of :meth:`from_layout` without its telemetry span."""
        sockets: dict[tuple[int, int, int], int] = {}
        for brick in layout:
            for conn in layout.connectors_of(brick, top=False):
                sockets[conn.cell] = brick.brick_id

        knob_contacts: list[KnobContact] = []
        grounded: set[int] = set()
        for brick in layout:
            for conn in layout.connectors_of(brick, top=True):
                cx, cy, cz = conn.cell
                mate = (cx, cy, cz + 1)
                if (above := sockets.get(mate)) is not None:
                    knob_contacts.append(
                        KnobContact(
                            below_id=brick.brick_id,
                            above_id=above,
                            x=cx,
                            y=cy,
                            interface_layer=cz + 1,
                        )
                    )
            for conn in layout.connectors_of(brick, top=False):
                if conn.cell[2] == 0:
                    grounded.add(brick.brick_id)
                    knob_contacts.append(
                        KnobContact(
                            below_id=GROUND_ID,
                            above_id=brick.brick_id,
                            x=conn.cell[0],
                            y=conn.cell[1],
                            interface_layer=0,
                        )
                    )

        side_contacts = _side_contacts(layout)
        return cls(
            brick_ids=tuple(sorted(layout.bricks)),
            knob_contacts=tuple(knob_contacts),
            side_contacts=tuple(side_contacts),
            grounded_ids=frozenset(grounded),
        )

    def support_edges(self) -> list[tuple[int, int]]:
        """Distinct ``(below, above)`` pairs, ground included as -1."""
        return sorted({(k.below_id, k.above_id) for k in self.knob_contacts})

    def component_count(self) -> int:
        """Count brick-graph components (stud connections between bricks only).

        Ground contacts do NOT join components: two grounded but
        stud-disconnected towers are two components (Luo's
        single-connectedness), even though neither is floating. An empty
        layout has zero components.
        """
        if not self.brick_ids:
            return 0
        n_components, _ = self._components(include_ground=False)
        return n_components

    def brick_components(self) -> dict[int, int]:
        """Map each brick id to its brick-graph component label."""
        if not self.brick_ids:
            return {}
        _, labels = self._components(include_ground=False)
        return {bid: int(labels[i]) for i, bid in enumerate(self.brick_ids)}

    def floating_ids(self) -> frozenset[int]:
        """Bricks not reachable from the ground through stud connections.

        This is the ground-merged reachability question — deliberately
        different from :meth:`component_count`'s brick-graph semantics.
        """
        _, labels = self._components(include_ground=True)
        index = {brick_id: i for i, brick_id in enumerate(self.brick_ids)}
        ground_label = labels[len(self.brick_ids)]
        return frozenset(
            brick_id
            for brick_id in self.brick_ids
            if labels[index[brick_id]] != ground_label
        )

    def is_stable_topology(self) -> bool:
        """Return True when every brick is ground-reachable via studs."""
        return not self.floating_ids()

    def _components(self, *, include_ground: bool) -> tuple[int, np.ndarray]:
        index = {brick_id: i for i, brick_id in enumerate(self.brick_ids)}
        n = len(self.brick_ids) + (1 if include_ground else 0)
        rows: list[int] = []
        cols: list[int] = []
        for contact in self.knob_contacts:
            if contact.below_id == GROUND_ID:
                if not include_ground:
                    continue
                below = len(self.brick_ids)
            else:
                below = index[contact.below_id]
            rows.append(below)
            cols.append(index[contact.above_id])
        matrix = coo_matrix(
            (np.ones(len(rows)), (rows, cols)),
            shape=(n, n),
        )
        return connected_components(matrix, directed=False)


def _side_contacts(layout: Layout) -> list[SideContact]:
    """Aggregate shared vertical faces per brick pair per axis direction."""
    faces: dict[tuple[int, int, int, int], list[tuple[float, float, float]]] = {}
    for brick in layout:
        for x, y, z in layout.cells_of(brick):
            for axis, (dx, dy) in enumerate(((1, 0), (0, 1))):
                neighbour = layout.brick_at((x + dx, y + dy, z))
                if neighbour is None or neighbour.brick_id == brick.brick_id:
                    continue
                center = (x + dx / 2, y + dy / 2, z + 0.5)
                key = (brick.brick_id, neighbour.brick_id, axis, 1)
                faces.setdefault(key, []).append(center)
    contacts: list[SideContact] = []
    for (a_id, b_id, axis, direction), centers in sorted(faces.items()):
        arr = np.asarray(centers)
        cx, cy, cz = arr.mean(axis=0)
        layers = arr[:, 2] - 0.5  # face centers sit at cell z + 0.5
        contacts.append(
            SideContact(
                a_id=a_id,
                b_id=b_id,
                axis=axis,
                direction=direction,
                face_count=len(centers),
                centroid=(float(cx), float(cy), float(cz)),
                z_lo=int(layers.min()),
                z_hi=int(layers.max()),
            )
        )
    return contacts
