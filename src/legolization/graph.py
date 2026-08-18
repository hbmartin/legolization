"""Connection graph: stud-cavity contacts, side contacts, connectivity.

Edges are directed "supports" relations (``below → above``) discovered by
matching top connectors (studs) against bottom connectors (anti-studs) one
layer up. Bricks whose bottom connectors sit at layer 0 connect to a virtual
ground node. Side contacts (shared vertical faces) don't join components but
feed horizontal press forces in the stability model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from legolization import telemetry

if TYPE_CHECKING:
    from legolization.layout import Layout
    from legolization.physical import LduConnector

GROUND_ID = -1
"""Pseudo brick id for the ground plane in knob contacts."""


@dataclass(frozen=True, slots=True)
class KnobContact:
    """One mated stud: ``below_id`` (or ground) into ``above_id``.

    ``x``/``y`` are the knob's grid column; ``interface_layer`` is the plate
    layer of the mating plane (the bottom layer of the upper brick).
    ``normal`` is the stud axis — ``(0, 0, 1)`` for ordinary vertical
    mates; lateral (SNOT) mates carry their sideways direction, and then
    ``x``/``y``/``interface_layer`` are the stud cell itself rather than
    a horizontal mating plane.
    """

    below_id: int
    above_id: int
    x: float
    y: float
    interface_layer: float
    normal: tuple[int, int, int] = (0, 0, 1)
    point_ldu: tuple[int, int, int] | None = None


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
    t_lo: float = 0.0
    t_hi: float = 0.0
    """Transverse edge bounds of the shared faces along the horizontal
    axis perpendicular to ``axis`` (grid units) — the
    yaw-torque model presses at the four (transverse, vertical) corners
    so side forces can carry every modelled torque axis."""


@dataclass(frozen=True, slots=True)
class TopologyMetrics:
    """Brick-component and ground-reachability metrics from one labeling."""

    component_count: int
    floating_ids: frozenset[int]
    component_labels: tuple[int, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Reject summaries whose labels cannot belong to their count.

        The check stays O(1) on purpose: SciPy's ``connected_components``
        always returns dense ``0..count-1`` labels, so re-deriving the
        distinct-label count would be a third per-brick pass over an array the
        caller has just built.
        """
        if self.component_count < 0:
            msg = "component_count must be non-negative"
            raise ValueError(msg)
        if bool(self.component_labels) != bool(self.component_count):
            msg = (
                f"component_count={self.component_count} does not match "
                f"{len(self.component_labels)} component label(s)"
            )
            raise ValueError(msg)

    @property
    def floating_count(self) -> int:
        """Number of bricks without a stud path to ground."""
        return len(self.floating_ids)

    def is_buildable(self, *, component_target: int = 1) -> bool:
        """Whether every brick is grounded within the component target."""
        return not self.floating_ids and self.component_count <= component_target


@dataclass(frozen=True, slots=True)
class ConnectionGraph:
    """All contacts of a layout plus component/grounding queries."""

    brick_ids: tuple[int, ...]
    knob_contacts: tuple[KnobContact, ...]
    side_contacts: tuple[SideContact, ...]
    grounded_ids: frozenset[int]
    _labeling: tuple[int, np.ndarray] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )
    _topology: TopologyMetrics | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    @classmethod
    def from_layout(cls, layout: Layout) -> ConnectionGraph:
        """Extract all knob and side contacts from a layout."""
        with telemetry.span("graph.from_layout", n=len(layout)):
            return cls._from_layout_body(layout)

    @classmethod
    def _from_layout_body(cls, layout: Layout) -> ConnectionGraph:
        """Run the body of :meth:`from_layout` without its telemetry span."""
        # Exact LDU points keep half-stud and half-plate features distinct;
        # coarse target cells are never used as physical connector evidence.
        sockets: dict[tuple[tuple[int, int, int], tuple[int, int, int]], int] = {}
        bottom_connectors: dict[int, tuple[LduConnector, ...]] = {}
        for brick in layout:
            connectors = layout.physical_connectors_of(brick, top=False)
            bottom_connectors[brick.brick_id] = connectors
            for connector in connectors:
                sockets[(connector.point, connector.direction)] = brick.brick_id

        knob_contacts: list[KnobContact] = []
        grounded: set[int] = set()
        for brick in layout:
            for connector in layout.physical_connectors_of(brick, top=True):
                dx, dy, dz = connector.direction
                if (above := sockets.get((connector.point, (-dx, -dy, -dz)))) is None:
                    continue
                point = connector.point
                knob_contacts.append(
                    KnobContact(
                        below_id=brick.brick_id,
                        above_id=above,
                        x=point[0] / 20,
                        y=point[1] / 20,
                        interface_layer=point[2] / 8,
                        normal=connector.direction,
                        point_ldu=point,
                    )
                )
            for connector in bottom_connectors[brick.brick_id]:
                # Only downward anti-studs can seat on the ground plane.
                if connector.point[2] == 0 and connector.direction == (0, 0, -1):
                    grounded.add(brick.brick_id)
                    knob_contacts.append(
                        KnobContact(
                            below_id=GROUND_ID,
                            above_id=brick.brick_id,
                            x=connector.point[0] / 20,
                            y=connector.point[1] / 20,
                            interface_layer=0,
                            point_ldu=connector.point,
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
        count, _ = self._ensure_labeling()
        return count

    def brick_components(self) -> dict[int, int]:
        """Map each brick id to its brick-graph component label."""
        _, labels = self._ensure_labeling()
        return dict(zip(self.brick_ids, labels.tolist(), strict=True))

    def floating_ids(self) -> frozenset[int]:
        """Bricks not reachable from the ground through stud connections.

        This is the ground-merged reachability question — deliberately
        different from :meth:`component_count`'s brick-graph semantics.
        """
        return self.topology_metrics().floating_ids

    def topology_metrics(self) -> TopologyMetrics:
        """Compute component and grounding metrics with one sparse labeling."""
        if self._topology is not None:
            return self._topology
        component_count, raw_labels = self._ensure_labeling()
        labels = tuple(raw_labels.tolist())
        grounded_labels = {
            labels[index]
            for index, brick_id in enumerate(self.brick_ids)
            if brick_id in self.grounded_ids
        }
        floating = frozenset(
            brick_id
            for index, brick_id in enumerate(self.brick_ids)
            if labels[index] not in grounded_labels
        )
        topology = TopologyMetrics(
            component_count=component_count,
            floating_ids=floating,
            component_labels=labels,
        )
        object.__setattr__(self, "_topology", topology)
        return topology

    def is_stable_topology(self) -> bool:
        """Return True when every brick is ground-reachable via studs."""
        return not self.floating_ids()

    def _ensure_labeling(self) -> tuple[int, np.ndarray]:
        """Label the brick graph at most once and cache SciPy's raw result.

        SciPy allocates and fills the label array whether or not
        ``return_labels`` is requested (measured <2% apart on a 200k-node
        graph), so a count-only traversal saves nothing at the SciPy level —
        while a count-only *cache* made every later label request rebuild the
        COO matrix and traverse it a second time. One labeling therefore backs
        the count, :meth:`brick_components`, and :meth:`topology_metrics`, in
        whatever order they are asked for. Labels stay in their SciPy array:
        only :meth:`topology_metrics` pays the per-brick tuple build.
        """
        if (labeling := self._labeling) is None:
            if self.brick_ids:
                count, labels = connected_components(
                    self._component_matrix(),
                    directed=False,
                )
            else:
                count, labels = 0, np.empty(0, dtype=np.intp)
            labeling = (int(count), labels)
            object.__setattr__(self, "_labeling", labeling)
        return labeling

    def _component_matrix(self) -> coo_matrix:
        index = {brick_id: i for i, brick_id in enumerate(self.brick_ids)}
        n = len(self.brick_ids)
        rows: list[int] = []
        cols: list[int] = []
        for contact in self.knob_contacts:
            if contact.below_id == GROUND_ID:
                continue
            below = index[contact.below_id]
            rows.append(below)
            cols.append(index[contact.above_id])
        return coo_matrix(
            (np.ones(len(rows)), (rows, cols)),
            shape=(n, n),
        )


def _side_contacts(layout: Layout) -> list[SideContact]:
    """Aggregate shared vertical faces per brick pair per axis direction.

    Sideways cladding parts (``mount_normal`` set) are excluded on both
    sides: their occupied cells are a conservative collision prism, not
    physical volume, and counting those faces gave a mounted tile a
    phantom press contact on top of its real lateral stud mate (PR #17
    review). Their only physical connection is the stud.
    """
    cladding = {
        brick.brick_id
        for brick in layout
        if layout.part_of(brick).mount_normal is not None
    }
    faces: dict[tuple[int, int, int, int], list[tuple[float, float, float]]] = {}
    for brick in layout:
        if brick.brick_id in cladding:
            continue
        for x, y, z in layout.cells_of(brick):
            for axis, (dx, dy) in enumerate(((1, 0), (0, 1))):
                neighbour = layout.brick_at((x + dx, y + dy, z))
                if (
                    neighbour is None
                    or neighbour.brick_id == brick.brick_id
                    or neighbour.brick_id in cladding
                ):
                    continue
                center = (x + dx / 2, y + dy / 2, z + 0.5)
                key = (brick.brick_id, neighbour.brick_id, axis, 1)
                faces.setdefault(key, []).append(center)
    contacts: list[SideContact] = []
    for (a_id, b_id, axis, direction), centers in sorted(faces.items()):
        arr = np.asarray(centers)
        cx, cy, cz = arr.mean(axis=0)
        layers = arr[:, 2] - 0.5  # face centers sit at cell z + 0.5
        transverse = arr[:, 1 - axis]
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
                t_lo=float(transverse.min() - 0.5),
                t_hi=float(transverse.max() + 0.5),
            )
        )
    return contacts
