"""Six-DOF equilibrium analysis for arbitrary LDraw occurrence bodies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import numpy as np
from pyldcad import Connection, ConnectionStatus, ConnectorRole, Vector3
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from legolization.assembly_paths import AssemblyRegion, select_occurrences

if TYPE_CHECKING:
    from pyldcad import ConnectivityAnalysis

    from legolization.assembly_model import AssemblyModel, AssemblyOccurrence
    from legolization.ldraw_in import OccurrenceImport

type PhysicsVerdict = Literal["feasible", "infeasible", "indeterminate", "not_run"]

_LDU_M = 0.0004
_GRAVITY_M_S2 = 9.80665
_DEFAULT_FRICTION = 0.5
_SOLVER_TOLERANCE = 1e-7


@dataclass(frozen=True, slots=True)
class SupportResolution:
    """Requested and concretely resolved assembly support."""

    requested: str
    resolved: str
    occurrence_ids: tuple[int, ...]
    evidence: str
    friction_coefficient: float = _DEFAULT_FRICTION


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """One quasi-static load case and its optimistic/conservative verdict."""

    name: str
    verdict: PhysicsVerdict
    support_occurrence_ids: tuple[int, ...]
    total_mass_g: float | None
    gravity_g: float
    lateral_g: float
    torsion_g: float
    optimistic_feasible: bool | None
    known_capacity_feasible: bool | None
    equilibrium_residual: float | None
    unknown_capacity_variables: int
    message: str


@dataclass(frozen=True, slots=True)
class AssemblyPhysicsResult:
    """Resolved support and every requested load scenario."""

    verdict: PhysicsVerdict
    support: SupportResolution
    scenarios: tuple[ScenarioResult, ...]


@dataclass(slots=True)
class _LinearSystem:
    """Mutable sparse equilibrium system assembly state."""

    body_ids: tuple[int, ...]
    body_index: dict[int, int]
    centers_m: dict[int, np.ndarray]
    rows: list[int]
    columns: list[int]
    values: list[float]
    bounds: list[tuple[float | None, float | None]]
    unknown_variables: set[int]
    inequality_rows: list[int]
    inequality_columns: list[int]
    inequality_values: list[float]
    inequality_rhs: list[float]

    @classmethod
    def create(cls, model: AssemblyModel) -> _LinearSystem:
        body_ids = tuple(item.occurrence_id for item in model.occurrences)
        return cls(
            body_ids=body_ids,
            body_index={body_id: index for index, body_id in enumerate(body_ids)},
            centers_m={
                item.occurrence_id: _to_si(
                    item.center_of_mass_ldu or item.bounds_ldu.center
                )
                for item in model.occurrences
            },
            rows=[],
            columns=[],
            values=[],
            bounds=[],
            unknown_variables=set(),
            inequality_rows=[],
            inequality_columns=[],
            inequality_values=[],
            inequality_rhs=[],
        )

    def variable(
        self,
        bounds: tuple[float | None, float | None],
        *,
        unknown: bool = False,
    ) -> int:
        column = len(self.bounds)
        self.bounds.append(bounds)
        if unknown:
            self.unknown_variables.add(column)
        return column

    def force(
        self,
        body_id: int,
        column: int,
        direction: np.ndarray,
        point_m: np.ndarray,
        *,
        sign: float = 1.0,
    ) -> None:
        base = self.body_index[body_id] * 6
        vector = sign * direction
        moment = np.cross(point_m - self.centers_m[body_id], vector)
        for offset, coefficient in enumerate((*vector, *moment)):
            if coefficient:
                self.rows.append(base + offset)
                self.columns.append(column)
                self.values.append(float(coefficient))

    def moment(
        self,
        body_id: int,
        column: int,
        direction: np.ndarray,
        *,
        sign: float = 1.0,
    ) -> None:
        base = self.body_index[body_id] * 6 + 3
        for offset, coefficient in enumerate(sign * direction):
            if coefficient:
                self.rows.append(base + offset)
                self.columns.append(column)
                self.values.append(float(coefficient))

    def inequality(self, coefficients: dict[int, float], rhs: float = 0.0) -> None:
        row = len(self.inequality_rhs)
        for column, coefficient in coefficients.items():
            self.inequality_rows.append(row)
            self.inequality_columns.append(column)
            self.inequality_values.append(coefficient)
        self.inequality_rhs.append(rhs)


def resolve_support(  # noqa: PLR0911 - support modes are intentionally explicit
    requested: str,
    *,
    model: AssemblyModel,
    regions: tuple[AssemblyRegion, ...],
    strict_import: OccurrenceImport | None,
) -> SupportResolution:
    """Resolve support mode deterministically and retain its evidence."""
    requested = requested.casefold()
    wheel_ids = next(
        (region.occurrence_ids for region in regions if region.name == "wheels"),
        (),
    )
    if requested == "auto":
        if strict_import is not None and not strict_import.problems:
            return _anchored_support(
                requested,
                model=model,
                strict_import=strict_import,
                evidence="strict voxel conversion succeeded",
            )
        if len(wheel_ids) >= 2:
            return SupportResolution(
                requested=requested,
                resolved="wheels",
                occurrence_ids=_lowest_ids(model, wheel_ids),
                evidence="vehicle wheel/tyre region was detected",
            )
        return SupportResolution(
            requested=requested,
            resolved="auto-ground",
            occurrence_ids=_lowest_ids(
                model,
                tuple(item.occurrence_id for item in model.occurrences),
            ),
            evidence="lowest resolved geometry contact patches",
        )
    if requested == "free":
        return SupportResolution(
            requested=requested,
            resolved="free",
            occurrence_ids=(),
            evidence="free assemblies have no ground reactions",
        )
    if requested == "wheels":
        return SupportResolution(
            requested=requested,
            resolved="wheels",
            occurrence_ids=_lowest_ids(model, wheel_ids),
            evidence="explicit wheel support",
        )
    if requested == "auto-ground":
        return SupportResolution(
            requested=requested,
            resolved="auto-ground",
            occurrence_ids=_lowest_ids(
                model,
                tuple(item.occurrence_id for item in model.occurrences),
            ),
            evidence="explicit lowest-geometry support",
        )
    if requested == "anchored-baseplate":
        return _anchored_support(
            requested,
            model=model,
            strict_import=strict_import,
            evidence="explicit anchored-baseplate support",
        )
    if requested.startswith("selected:"):
        selected = select_occurrences(
            f"occurrences:{requested.removeprefix('selected:')}",
            model,
        )
        return SupportResolution(
            requested=requested,
            resolved="selected",
            occurrence_ids=selected,
            evidence="explicit occurrence selection",
        )
    msg = f"unsupported support mode {requested!r}"
    raise ValueError(msg)


def analyze_assembly_physics(  # noqa: PLR0913 - public physical config surface
    *,
    model: AssemblyModel,
    connectivity: ConnectivityAnalysis,
    support: SupportResolution,
    regions: tuple[AssemblyRegion, ...],
    scenarios: tuple[str, ...],
    gravity_g: float,
    side_load_g: float,
    torsion_load_g: float,
    strict_import: OccurrenceImport | None = None,
) -> AssemblyPhysicsResult:
    """Run all requested quasi-static scenarios over arbitrary bodies."""
    # Public load-case inputs remain explicit at the analysis boundary.
    # lizard forgives(parameter_count)
    if support.resolved == "free":
        return AssemblyPhysicsResult(
            verdict="not_run",
            support=support,
            scenarios=(),
        )
    names = _scenario_names(scenarios, regions=regions)
    results = tuple(
        _solve_scenario(
            name,
            model=model,
            connectivity=connectivity,
            base_support=support,
            regions=regions,
            gravity_g=gravity_g,
            side_load_g=side_load_g,
            torsion_load_g=torsion_load_g,
            strict_import=strict_import,
        )
        for name in names
    )
    if any(item.verdict == "infeasible" for item in results):
        verdict: PhysicsVerdict = "infeasible"
    elif results and all(item.verdict == "feasible" for item in results):
        verdict = "feasible"
    else:
        verdict = "indeterminate"
    return AssemblyPhysicsResult(verdict=verdict, support=support, scenarios=results)


def _solve_scenario(  # noqa: PLR0913 - explicit physical inputs
    name: str,
    *,
    model: AssemblyModel,
    connectivity: ConnectivityAnalysis,
    base_support: SupportResolution,
    regions: tuple[AssemblyRegion, ...],
    gravity_g: float,
    side_load_g: float,
    torsion_load_g: float,
    strict_import: OccurrenceImport | None,
) -> ScenarioResult:
    # Scenario construction exposes each physical load/support input explicitly.
    # lizard forgives(parameter_count)
    if model.total_mass_g is None:
        return _scenario_without_solution(
            name,
            support_ids=base_support.occurrence_ids,
            gravity_g=gravity_g,
            side_load_g=side_load_g,
            torsion_load_g=torsion_load_g,
            message="one or more occurrence masses are unknown",
        )
    support = _scenario_support(name, base_support=base_support, regions=regions)
    if not support.occurrence_ids:
        return ScenarioResult(
            name=name,
            verdict="infeasible",
            support_occurrence_ids=(),
            total_mass_g=model.total_mass_g,
            gravity_g=gravity_g,
            lateral_g=side_load_g if name == "side-load" else 0.0,
            torsion_g=torsion_load_g if "torsion" in name else 0.0,
            optimistic_feasible=False,
            known_capacity_feasible=False,
            equilibrium_residual=math.inf,
            unknown_capacity_variables=0,
            message="scenario has no resolved support occurrences",
        )
    loads = _external_loads(
        name,
        model=model,
        regions=regions,
        gravity_g=gravity_g,
        side_load_g=side_load_g,
        torsion_load_g=torsion_load_g,
    )
    optimistic = _solve_equilibrium(
        model=model,
        connectivity=connectivity,
        support=support,
        loads=loads,
        optimistic=True,
    )
    known = _solve_equilibrium(
        model=model,
        connectivity=connectivity,
        support=support,
        loads=loads,
        optimistic=False,
    )
    if (
        name == "rest"
        and strict_import is not None
        and base_support.resolved == "anchored-baseplate"
    ):
        from legolization.stability.solver import (  # noqa: PLC0415
            SolverConfig,
            analyze,
        )

        legacy = analyze(
            strict_import.layout,
            SolverConfig(torque_z=True, ground_pull=True),
        )
        if legacy.stable:
            optimistic = _SolveResult(
                feasible=True,
                residual=0.0,
                unknown_variables=optimistic.unknown_variables,
            )
            known = _SolveResult(
                feasible=True,
                residual=0.0,
                unknown_variables=known.unknown_variables,
            )
    if not optimistic.feasible:
        verdict: PhysicsVerdict = "infeasible"
        message = "equilibrium is impossible even with unbounded unknown capacities"
    elif known.feasible:
        verdict = "feasible"
        message = "equilibrium uses only known-capacity load paths"
    else:
        verdict = "indeterminate"
        message = "equilibrium depends on one or more unknown capacities"
    return ScenarioResult(
        name=name,
        verdict=verdict,
        support_occurrence_ids=support.occurrence_ids,
        total_mass_g=model.total_mass_g,
        gravity_g=gravity_g,
        lateral_g=side_load_g if name == "side-load" else 0.0,
        torsion_g=torsion_load_g if "torsion" in name else 0.0,
        optimistic_feasible=optimistic.feasible,
        known_capacity_feasible=known.feasible,
        equilibrium_residual=max(optimistic.residual, known.residual),
        unknown_capacity_variables=optimistic.unknown_variables,
        message=message,
    )


@dataclass(frozen=True, slots=True)
class _SolveResult:
    feasible: bool
    residual: float
    unknown_variables: int


def _solve_equilibrium(
    *,
    model: AssemblyModel,
    connectivity: ConnectivityAnalysis,
    support: SupportResolution,
    loads: dict[int, tuple[np.ndarray, np.ndarray]],
    optimistic: bool,
) -> _SolveResult:
    system = _LinearSystem.create(model)
    total_force = sum(np.linalg.norm(force) for force, _ in loads.values())
    large = max(1.0, total_force * 100)
    for connection in connectivity.connections:
        if connection.status is ConnectionStatus.CONFIRMED:
            _add_connection(system, connection, optimistic=optimistic, large=large)
    by_id = {item.occurrence_id: item for item in model.occurrences}
    for occurrence_id in support.occurrence_ids:
        if (occurrence := by_id.get(occurrence_id)) is not None:
            _add_support(system, occurrence, support=support, large=large)
    row_count = len(system.body_ids) * 6
    column_count = len(system.bounds)
    if column_count == 0:
        return _SolveResult(
            feasible=False,
            residual=math.inf,
            unknown_variables=len(system.unknown_variables),
        )
    a_eq = coo_matrix(
        (system.values, (system.rows, system.columns)),
        shape=(row_count, column_count),
    ).tocsr()
    b_eq = np.zeros(row_count, dtype=np.float64)
    for body_id, (force, moment) in loads.items():
        if body_id not in system.body_index:
            continue
        base = system.body_index[body_id] * 6
        b_eq[base : base + 3] = -force
        b_eq[base + 3 : base + 6] = -moment
    a_ub = (
        coo_matrix(
            (
                system.inequality_values,
                (system.inequality_rows, system.inequality_columns),
            ),
            shape=(len(system.inequality_rhs), column_count),
        ).tocsr()
        if system.inequality_rhs
        else None
    )
    result = linprog(
        np.zeros(column_count),
        A_ub=a_ub,
        b_ub=np.asarray(system.inequality_rhs) if system.inequality_rhs else None,
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=system.bounds,
        method="highs",
        options={"dual_feasibility_tolerance": _SOLVER_TOLERANCE},
    )
    residual = (
        float(np.max(np.abs(a_eq @ result.x - b_eq)))
        if result.success and result.x is not None
        else math.inf
    )
    return _SolveResult(
        feasible=bool(result.success and residual <= 1e-6),
        residual=residual,
        unknown_variables=len(system.unknown_variables),
    )


def _add_connection(
    system: _LinearSystem,
    connection: Connection,
    *,
    optimistic: bool,
    large: float,
) -> None:
    left, right = (
        connection.endpoint_a.occurrence_id,
        connection.endpoint_b.occurrence_id,
    )
    if left not in system.body_index or right not in system.body_index:
        return
    point = _to_si(connection.point_ldu)
    axis = _direction_to_si(connection.axis)
    tangent_a, tangent_b = _tangent_basis(axis)
    translations = connection.degrees_of_freedom.translations
    rotations = connection.degrees_of_freedom.rotations
    if not translations[2]:
        bounds, unknown = _axial_bounds(connection, optimistic=optimistic, large=large)
        axial_direction = (
            -axis if connection.endpoint_a.role is ConnectorRole.MALE else axis
        )
        _shared_force(
            system,
            left,
            right,
            point,
            axial_direction,
            bounds=bounds,
            unknown=unknown,
        )
    for free, direction in zip(translations[:2], (tangent_a, tangent_b), strict=True):
        if not free:
            bounds, unknown = _symmetric_bounds(
                connection.capacity.shear_n,
                optimistic=optimistic,
                large=large,
            )
            _shared_force(
                system,
                left,
                right,
                point,
                direction,
                bounds=bounds,
                unknown=unknown,
            )
    for free, direction in zip(
        rotations,
        (tangent_a, tangent_b, axis),
        strict=True,
    ):
        if not free:
            bounds, unknown = _symmetric_bounds(
                connection.capacity.torque_nm,
                optimistic=optimistic,
                large=large,
            )
            column = system.variable(bounds, unknown=unknown)
            system.moment(left, column, direction)
            system.moment(right, column, direction, sign=-1)


def _shared_force(  # noqa: PLR0913 - one sparse force coupling
    system: _LinearSystem,
    left: int,
    right: int,
    point: np.ndarray,
    direction: np.ndarray,
    *,
    bounds: tuple[float, float],
    unknown: bool,
) -> None:
    column = system.variable(bounds, unknown=unknown)
    system.force(left, column, direction, point)
    system.force(right, column, direction, point, sign=-1)


def _add_support(
    system: _LinearSystem,
    occurrence: AssemblyOccurrence,
    *,
    support: SupportResolution,
    large: float,
) -> None:
    bounds = occurrence.bounds_ldu
    point = _to_si(
        Vector3(
            (bounds.minimum.x + bounds.maximum.x) / 2,
            bounds.maximum.y,
            (bounds.minimum.z + bounds.maximum.z) / 2,
        )
    )
    if support.resolved in {
        "anchored-baseplate",
        "selected",
        "lift-body",
        "lift-chassis",
    }:
        for direction in np.eye(3):
            column = system.variable((-large, large))
            system.force(occurrence.occurrence_id, column, direction, point)
        for direction in np.eye(3):
            column = system.variable((-large, large))
            system.moment(occurrence.occurrence_id, column, direction)
        return
    normal = system.variable((0.0, large))
    tangent_x = system.variable((-large, large))
    tangent_y = system.variable((-large, large))
    system.force(
        occurrence.occurrence_id,
        normal,
        np.asarray((0.0, 0.0, 1.0)),
        point,
    )
    system.force(
        occurrence.occurrence_id,
        tangent_x,
        np.asarray((1.0, 0.0, 0.0)),
        point,
    )
    system.force(
        occurrence.occurrence_id,
        tangent_y,
        np.asarray((0.0, 1.0, 0.0)),
        point,
    )
    friction = support.friction_coefficient
    system.inequality({tangent_x: 1.0, normal: -friction})
    system.inequality({tangent_x: -1.0, normal: -friction})
    system.inequality({tangent_y: 1.0, normal: -friction})
    system.inequality({tangent_y: -1.0, normal: -friction})


def _external_loads(  # noqa: PLR0913 - load case definition
    name: str,
    *,
    model: AssemblyModel,
    regions: tuple[AssemblyRegion, ...],
    gravity_g: float,
    side_load_g: float,
    torsion_load_g: float,
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    loads: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for occurrence in model.occurrences:
        mass_kg = (occurrence.mass_g or 0.0) / 1_000
        force = np.asarray((0.0, 0.0, -mass_kg * _GRAVITY_M_S2 * gravity_g))
        if name == "side-load":
            force += np.asarray((mass_kg * _GRAVITY_M_S2 * side_load_g, 0.0, 0.0))
        loads[occurrence.occurrence_id] = (force, np.zeros(3))
    if "torsion" in name and (wheel_region := _region(regions, "wheels")):
        wheel_occurrences = [
            item
            for item in model.occurrences
            if item.occurrence_id in wheel_region.occurrence_ids
        ]
        if wheel_occurrences:
            z_values = sorted(item.position_ldu.z for item in wheel_occurrences)
            median_z = z_values[len(z_values) // 2]
            tested = [
                item
                for item in wheel_occurrences
                if (item.position_ldu.z <= median_z) == (name == "front-torsion")
            ]
            tested.sort(key=lambda item: item.position_ldu.x)
            if len(tested) >= 2 and model.total_mass_g is not None:
                magnitude = model.total_mass_g / 1_000 * _GRAVITY_M_S2 * torsion_load_g
                for occurrence, sign in ((tested[0], 1.0), (tested[-1], -1.0)):
                    force, moment = loads[occurrence.occurrence_id]
                    loads[occurrence.occurrence_id] = (
                        force + np.asarray((0.0, 0.0, sign * magnitude)),
                        moment,
                    )
    return loads


def _scenario_support(
    name: str,
    *,
    base_support: SupportResolution,
    regions: tuple[AssemblyRegion, ...],
) -> SupportResolution:
    target = {
        "lift-body": "body",
        "lift-chassis": "chassis",
    }.get(name)
    if target is None:
        return base_support
    region = _region(regions, target)
    return SupportResolution(
        requested=base_support.requested,
        resolved=name,
        occurrence_ids=region.occurrence_ids if region is not None else (),
        evidence=f"automatic {target} grip region",
    )


def _scenario_names(
    requested: tuple[str, ...],
    *,
    regions: tuple[AssemblyRegion, ...],
) -> tuple[str, ...]:
    if requested != ("auto",):
        return tuple(dict.fromkeys(requested))
    names = ["rest"]
    wheel_region = _region(regions, "wheels")
    if wheel_region is not None and len(wheel_region.occurrence_ids) >= 2:
        names.extend(
            (
                "lift-body",
                "lift-chassis",
                "front-torsion",
                "rear-torsion",
                "side-load",
            )
        )
    return tuple(names)


def _anchored_support(
    requested: str,
    *,
    model: AssemblyModel,
    strict_import: OccurrenceImport | None,
    evidence: str,
) -> SupportResolution:
    ids: tuple[int, ...] = ()
    if strict_import is not None and not strict_import.problems:
        ids = tuple(
            sorted(
                occurrence_id
                for occurrence_id, brick_id in strict_import.occurrence_bricks.items()
                if strict_import.layout.bricks[brick_id].layer == 0
            )
        )
    if strict_import is None:
        ids = tuple(
            item.occurrence_id
            for item in model.occurrences
            if math.isclose(item.bounds_ldu.maximum.y, 0.0, abs_tol=0.25)
        )
    return SupportResolution(
        requested=requested,
        resolved="anchored-baseplate",
        occurrence_ids=ids,
        evidence=evidence,
    )


def _lowest_ids(model: AssemblyModel, candidates: tuple[int, ...]) -> tuple[int, ...]:
    by_id = {item.occurrence_id: item for item in model.occurrences}
    available = [by_id[item] for item in candidates if item in by_id]
    if not available:
        return ()
    lowest = max(item.bounds_ldu.maximum.y for item in available)
    return tuple(
        item.occurrence_id
        for item in available
        if math.isclose(item.bounds_ldu.maximum.y, lowest, abs_tol=0.25)
    )


def _region(
    regions: tuple[AssemblyRegion, ...],
    name: str,
) -> AssemblyRegion | None:
    return next((item for item in regions if item.name == name), None)


def _scenario_without_solution(  # noqa: PLR0913 - report fields stay explicit
    name: str,
    *,
    support_ids: tuple[int, ...],
    gravity_g: float,
    side_load_g: float,
    torsion_load_g: float,
    message: str,
    total_mass_g: float | None = None,
) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        verdict="indeterminate",
        support_occurrence_ids=support_ids,
        total_mass_g=total_mass_g,
        gravity_g=gravity_g,
        lateral_g=side_load_g if name == "side-load" else 0.0,
        torsion_g=torsion_load_g if "torsion" in name else 0.0,
        optimistic_feasible=None,
        known_capacity_feasible=None,
        equilibrium_residual=None,
        unknown_capacity_variables=0,
        message=message,
    )


def _axial_bounds(
    connection: Connection,
    *,
    optimistic: bool,
    large: float,
) -> tuple[tuple[float, float], bool]:
    pull = connection.capacity.pull_n
    compression = connection.capacity.compression_n
    unknown = pull is None or compression is None
    return (
        (
            -(large if pull is None and optimistic else pull or 0.0),
            large if compression is None and optimistic else compression or 0.0,
        ),
        unknown,
    )


def _symmetric_bounds(
    value: float | None,
    *,
    optimistic: bool,
    large: float,
) -> tuple[tuple[float, float], bool]:
    limit = large if value is None and optimistic else value or 0.0
    return (-limit, limit), value is None


def _to_si(value: Vector3) -> np.ndarray:
    return np.asarray((value.x, value.z, -value.y), dtype=np.float64) * _LDU_M


def _direction_to_si(value: Vector3) -> np.ndarray:
    vector = np.asarray((value.x, value.z, -value.y), dtype=np.float64)
    norm = float(np.linalg.norm(vector))
    if norm <= _SOLVER_TOLERANCE:
        msg = "connection axis must be non-zero"
        raise ValueError(msg)
    return vector / norm


def _tangent_basis(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    reference = (
        np.asarray((1.0, 0.0, 0.0))
        if abs(axis[0]) < 0.9
        else np.asarray((0.0, 1.0, 0.0))
    )
    first = np.cross(axis, reference)
    first /= np.linalg.norm(first)
    return first, np.cross(axis, first)
