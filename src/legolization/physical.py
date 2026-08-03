"""Exact integer-LDU geometry alongside coarse target-cell coverage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from legolization.ldraw_units import PLATE_LDU, STUD_LDU

if TYPE_CHECKING:
    from legolization.catalog import Cell, Connector

type LduPoint = tuple[int, int, int]

_STUD = int(STUD_LDU)
_PLATE = int(PLATE_LDU)
_HALF_STUD = _STUD // 2
_HALF_PLATE = _PLATE // 2


@dataclass(frozen=True, slots=True)
class LduBox:
    """Axis-aligned half-open collision box in model-space LDU."""

    minimum: LduPoint
    maximum: LduPoint

    def __post_init__(self) -> None:
        if any(
            low >= high for low, high in zip(self.minimum, self.maximum, strict=True)
        ):
            msg = f"invalid LDU box {self.minimum}..{self.maximum}"
            raise ValueError(msg)

    def translated(self, offset: LduPoint) -> Self:
        """Translate the box by an exact integer offset."""
        return type(self)(
            minimum=_add(self.minimum, offset),
            maximum=_add(self.maximum, offset),
        )

    def rotated_yaw(self, yaw: int) -> Self:
        """Rotate the horizontal rectangle about the local LDraw origin."""
        corners = (
            (self.minimum[0], self.minimum[1]),
            (self.minimum[0], self.maximum[1]),
            (self.maximum[0], self.minimum[1]),
            (self.maximum[0], self.maximum[1]),
        )
        rotated = tuple(_rotate_xy(point, yaw) for point in corners)
        xs, ys = zip(*rotated, strict=True)
        return type(self)(
            minimum=(min(xs), min(ys), self.minimum[2]),
            maximum=(max(xs), max(ys), self.maximum[2]),
        )

    def intersects(self, other: LduBox) -> bool:
        """Return whether two half-open volumes overlap with positive volume."""
        return all(
            left_low < right_high and right_low < left_high
            for left_low, left_high, right_low, right_high in zip(
                self.minimum,
                self.maximum,
                other.minimum,
                other.maximum,
                strict=True,
            )
        )


@dataclass(frozen=True, slots=True)
class LduConnector:
    """Connector center and unit grid direction in exact physical space."""

    point: LduPoint
    direction: Cell

    def translated(self, offset: LduPoint) -> Self:
        """Translate the connector while preserving its direction."""
        return type(self)(point=_add(self.point, offset), direction=self.direction)

    def rotated_yaw(self, yaw: int) -> Self:
        """Rotate the connector point and direction around the vertical axis."""
        x, y = _rotate_xy((self.point[0], self.point[1]), yaw)
        dx, dy = _rotate_xy((self.direction[0], self.direction[1]), yaw)
        return type(self)(
            point=(x, y, self.point[2]),
            direction=(dx, dy, self.direction[2]),
        )


def box_for_cell(cell: Cell) -> LduBox:
    """Return the exact box represented by one coarse stud/plate cell."""
    x, y, z = cell
    return LduBox(
        minimum=(
            _STUD * x - _HALF_STUD,
            _STUD * y - _HALF_STUD,
            _PLATE * z,
        ),
        maximum=(
            _STUD * x + _HALF_STUD,
            _STUD * y + _HALF_STUD,
            _PLATE * (z + 1),
        ),
    )


def physical_connector(connector: Connector) -> LduConnector:
    """Convert a coarse connector cell/direction to its mating-plane center."""
    x, y, z = connector.cell
    dx, dy, dz = connector.direction
    point = (
        _STUD * x + _HALF_STUD * dx,
        _STUD * y + _HALF_STUD * dy,
        _PLATE * z + (_PLATE if dz > 0 else 0) + (_HALF_PLATE if dz == 0 else 0),
    )
    return LduConnector(point=point, direction=connector.direction)


def placement_offset(
    x: int,
    y: int,
    layer: int,
    extra: LduPoint = (0, 0, 0),
) -> LduPoint:
    """Translate a coarse placement anchor to exact model-space LDU."""
    return (_STUD * x + extra[0], _STUD * y + extra[1], _PLATE * layer + extra[2])


def boxes_intersect(first: tuple[LduBox, ...], second: tuple[LduBox, ...]) -> bool:
    """Return whether any exact collision volumes overlap."""
    return any(left.intersects(right) for left in first for right in second)


def _rotate_xy(point: tuple[int, int], yaw: int) -> tuple[int, int]:
    x, y = point
    match yaw % 360:
        case 0:
            return (x, y)
        case 90:
            return (-y, x)
        case 180:
            return (-x, -y)
        case 270:
            return (y, -x)
        case _:
            msg = f"yaw must be a multiple of 90, got {yaw}"
            raise ValueError(msg)


def _add(left: LduPoint, right: LduPoint) -> LduPoint:
    return (
        left[0] + right[0],
        left[1] + right[1],
        left[2] + right[2],
    )
