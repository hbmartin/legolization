"""Placed bricks and the layout occupancy index.

A :class:`Layout` is the working representation between placement and
export: an id-keyed set of :class:`PlacedBrick` records plus a cell → brick
occupancy dict giving O(1) collision and adjacency queries (avoiding the
O(n²) scans of the sample repos).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from legolization.catalog import Catalog, Cell, Connector, Part


@dataclass(frozen=True, slots=True)
class PlacedBrick:
    """One part instance at a grid position with yaw and colour."""

    brick_id: int
    part_key: str
    x: int
    y: int
    layer: int
    yaw: int
    colour_code: int


class CollisionError(ValueError):
    """Raised when a placement overlaps an existing brick."""


@dataclass(slots=True)
class Layout:
    """Mutable brick collection with an occupancy index for O(1) queries."""

    catalog: Catalog
    bricks: dict[int, PlacedBrick] = field(default_factory=dict)
    occupancy: dict[Cell, int] = field(default_factory=dict)
    _next_id: int = 0

    def __len__(self) -> int:
        return len(self.bricks)

    def __iter__(self) -> Iterator[PlacedBrick]:
        return iter(self.bricks.values())

    def part_of(self, brick: PlacedBrick) -> Part:
        """Return the catalog part of a placed brick."""
        return self.catalog[brick.part_key]

    def cells_of(self, brick: PlacedBrick) -> list[Cell]:
        """World cells occupied by a placed brick."""
        return self.part_of(brick).cells_at(brick.x, brick.y, brick.layer, brick.yaw)

    def filled_cells_of(self, brick: PlacedBrick) -> list[Cell]:
        """World cells a placed brick contributes to the target shape."""
        return self.part_of(brick).filled_at(brick.x, brick.y, brick.layer, brick.yaw)

    def connectors_of(self, brick: PlacedBrick, *, top: bool) -> list[Connector]:
        """World-space studs (top) or anti-studs (bottom) of a placed brick."""
        return self.part_of(brick).connectors_at(
            brick.x, brick.y, brick.layer, brick.yaw, top=top
        )

    def can_place(
        self,
        part: Part,
        x: int,
        y: int,
        layer: int,
        yaw: int,
    ) -> bool:
        """Whether the placement is collision-free and above ground."""
        cells = part.cells_at(x, y, layer, yaw)
        return all(cell[2] >= 0 and cell not in self.occupancy for cell in cells)

    def add(  # noqa: PLR0913 - a placement is naturally six scalars
        self,
        part_key: str,
        x: int,
        y: int,
        layer: int,
        yaw: int,
        colour_code: int,
    ) -> PlacedBrick:
        """Place a brick, raising :class:`CollisionError` on overlap."""
        part = self.catalog[part_key]
        cells = part.cells_at(x, y, layer, yaw)
        for cell in cells:
            if cell[2] < 0:
                msg = f"{part_key} at {(x, y, layer)} extends below ground"
                raise CollisionError(msg)
            if (other := self.occupancy.get(cell)) is not None:
                msg = f"{part_key} at {(x, y, layer)} collides with brick {other}"
                raise CollisionError(msg)
        brick = PlacedBrick(
            brick_id=self._next_id,
            part_key=part_key,
            x=x,
            y=y,
            layer=layer,
            yaw=yaw,
            colour_code=colour_code,
        )
        self.bricks[brick.brick_id] = brick
        for cell in cells:
            self.occupancy[cell] = brick.brick_id
        self._next_id += 1
        return brick

    def remove(self, brick_id: int) -> PlacedBrick:
        """Remove a brick and free its cells."""
        brick = self.bricks.pop(brick_id)
        for cell in self.cells_of(brick):
            del self.occupancy[cell]
        return brick

    def remove_many(self, brick_ids: Iterable[int]) -> list[PlacedBrick]:
        """Remove several bricks, returning the removed records."""
        return [self.remove(brick_id) for brick_id in brick_ids]

    def brick_at(self, cell: Cell) -> PlacedBrick | None:
        """Return the brick occupying a cell, if any."""
        if (brick_id := self.occupancy.get(cell)) is not None:
            return self.bricks[brick_id]
        return None

    def copy(self) -> Layout:
        """Return a shallow structural copy (records are frozen)."""
        return Layout(
            catalog=self.catalog,
            bricks=dict(self.bricks),
            occupancy=dict(self.occupancy),
            _next_id=self._next_id,
        )

    def subset(self, brick_ids: Iterable[int]) -> Layout:
        """Return a layout holding only the given bricks (prefix analysis)."""
        wanted = set(brick_ids)
        return Layout(
            catalog=self.catalog,
            bricks={bid: b for bid, b in self.bricks.items() if bid in wanted},
            occupancy={
                cell: bid for cell, bid in self.occupancy.items() if bid in wanted
            },
            _next_id=self._next_id,
        )

    def replace_with(self, other: Layout) -> None:
        """Adopt another layout's contents (accept a refinement candidate)."""
        self.bricks = other.bricks
        self.occupancy = other.occupancy
        self._next_id = other._next_id

    def translated(self, *, dz: int) -> Layout:
        """Return an id-preserving copy shifted down by ``dz`` plate layers.

        Used to analyze a subassembly as its own grounded-on-table
        structure. Constructs the dicts directly — ``add`` would reassign
        brick ids, and subassembly bookkeeping depends on them.
        """
        bricks = {
            bid: replace(brick, layer=brick.layer - dz)
            for bid, brick in self.bricks.items()
        }
        occupancy = {(x, y, z - dz): bid for (x, y, z), bid in self.occupancy.items()}
        if any(cell[2] < 0 for cell in occupancy):
            msg = f"translation by {dz} would sink bricks below ground"
            raise ValueError(msg)
        return Layout(
            catalog=self.catalog,
            bricks=bricks,
            occupancy=occupancy,
            _next_id=self._next_id,
        )

    def total_mass_g(self) -> float:
        """Total mass of all placed bricks in grams."""
        return sum(self.part_of(b).mass_g for b in self)

    def layers(self) -> list[int]:
        """Sorted distinct base layers present in the layout."""
        return sorted({b.layer for b in self})
