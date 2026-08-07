"""Strict nested configuration for the command-line application."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from legolization.errors import ConfigurationError
from legolization.instructions.sequencer import InstructionsConfig
from legolization.mesh import MeshOptions
from legolization.pipeline import PipelineConfig
from legolization.placement.base import ObjectiveWeights
from legolization.placement.repair import RepairConfig
from legolization.stability.solver import SolverConfig

type PlacementMode = Literal[
    "auto",
    "global-exact",
    "greedy",
    "luo",
    "bond",
    "fast",
    "smga",
    "beauty",
    "kollsker",
]
type ExactLimitPolicy = Literal["fail", "fallback", "continue"]
type PhysicsProfile = Literal["corrected", "stablelego-parity"]
type SupportMode = Literal["baseplate", "table"]
type CostObjective = Literal["bricks", "mass"]

_PLACEMENT_MODES = frozenset(
    (
        "auto",
        "global-exact",
        "greedy",
        "luo",
        "bond",
        "fast",
        "smga",
        "beauty",
        "kollsker",
    )
)


@dataclass(frozen=True, slots=True, kw_only=True)
class InputConfig:
    """Voxel and mesh input options."""

    plates_per_voxel: int = 3
    aspect_correct: bool = False
    dither: bool = False
    mesh: MeshOptions = field(default_factory=lambda: MeshOptions(grid_phases=8))
    _target_studs_explicit: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.plates_per_voxel <= 0:
            msg = "input.plates_per_voxel must be positive"
            raise ConfigurationError(msg)

    @property
    def target_studs_explicit(self) -> bool:
        """Whether the user supplied mesh target_studs rather than its default."""
        return self._target_studs_explicit


@dataclass(frozen=True, slots=True, kw_only=True)
class GeometryConfig:
    """Target-shell construction options."""

    hollow: bool = True
    hollow_rounds: int = 5
    hollow_restore_radius: int = 2
    shell_studs: int = 1
    shell_plates: int = 3
    ignore_interior: bool = True

    def __post_init__(self) -> None:
        values = (
            self.hollow_rounds,
            self.hollow_restore_radius,
            self.shell_studs,
            self.shell_plates,
        )
        if any(value < 0 for value in values):
            msg = "geometry integer options must be non-negative"
            raise ConfigurationError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExactPlacementConfig:
    """Guardrails and policy for whole-model exact placement."""

    max_cells: int = 256
    max_candidates: int = 100_000
    max_stability_cuts: int = 256
    time_limit_s: float = 60.0
    limit_policy: ExactLimitPolicy = "fail"
    fallback_strategy: Literal["bond", "fast", "greedy"] = "bond"

    def __post_init__(self) -> None:
        if (
            self.max_cells <= 0
            or self.max_candidates <= 0
            or self.max_stability_cuts <= 0
        ):
            msg = "exact placement caps must be positive"
            raise ConfigurationError(msg)
        if not math.isfinite(self.time_limit_s) or self.time_limit_s <= 0:
            msg = "placement.exact.time_limit_s must be positive"
            raise ConfigurationError(msg)
        if self.limit_policy not in {"fail", "fallback", "continue"}:
            msg = f"unsupported exact limit policy {self.limit_policy!r}"
            raise ConfigurationError(msg)
        if self.fallback_strategy not in {"bond", "fast", "greedy"}:
            msg = f"unsupported exact fallback {self.fallback_strategy!r}"
            raise ConfigurationError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlacementConfig:
    """Placement selection, objective, and deterministic restart options."""

    strategy: PlacementMode = "auto"
    seed: int = 0
    restarts: int = 1
    jobs: int = 1
    refine: bool = True
    time_budget_s: float | None = None
    objective: CostObjective = "bricks"
    colour_mode: Literal["hard", "soft"] = "hard"
    colour_weight: float = 1.0
    ga_generations: int = 200
    beauty_preset: Literal["balanced", "stability", "aesthetics", "efficiency"] = (
        "balanced"
    )
    exact: ExactPlacementConfig = field(default_factory=ExactPlacementConfig)
    weights: ObjectiveWeights = field(default_factory=ObjectiveWeights)

    def __post_init__(self) -> None:
        if self.strategy not in _PLACEMENT_MODES:
            msg = f"unsupported placement strategy {self.strategy!r}"
            raise ConfigurationError(msg)
        if self.restarts <= 0 or self.jobs <= 0 or self.ga_generations <= 0:
            msg = "placement counts must be positive"
            raise ConfigurationError(msg)
        if self.time_budget_s is not None and (
            not math.isfinite(self.time_budget_s) or self.time_budget_s <= 0
        ):
            msg = "placement.time_budget_s must be positive"
            raise ConfigurationError(msg)
        if not math.isfinite(self.colour_weight) or self.colour_weight < 0:
            msg = "placement.colour_weight must be non-negative"
            raise ConfigurationError(msg)
        if self.objective not in {"bricks", "mass"}:
            msg = f"unsupported placement objective {self.objective!r}"
            raise ConfigurationError(msg)
        if self.colour_mode not in {"hard", "soft"}:
            msg = f"unsupported colour mode {self.colour_mode!r}"
            raise ConfigurationError(msg)
        if self.exact.limit_policy == "continue" and self.time_budget_s is None:
            msg = "exact continue policy requires placement.time_budget_s"
            raise ConfigurationError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class FinishingConfig:
    """Shape-preserving surface and side-detail options."""

    slopes: bool = True
    tiles: bool = True
    snot: bool = False
    plate_cap: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class StabilityConfig:
    """Physics profile, support boundary, and repair options."""

    profile: PhysicsProfile = "corrected"
    support: SupportMode = "baseplate"
    repair: bool = True
    solver: SolverConfig = field(default_factory=SolverConfig)
    repair_options: RepairConfig = field(default_factory=RepairConfig)

    def __post_init__(self) -> None:
        if self.profile not in {"corrected", "stablelego-parity"}:
            msg = f"unsupported physics profile {self.profile!r}"
            raise ConfigurationError(msg)
        if self.support not in {"baseplate", "table"}:
            msg = f"unsupported support mode {self.support!r}"
            raise ConfigurationError(msg)
        if self.solver.mode != "lp":
            msg = "stability.solver.mode is production LP only"
            raise ConfigurationError(msg)

    def effective_solver(self) -> SolverConfig:
        """Return the explicitly selected physics profile."""
        match self.profile:
            case "corrected":
                return replace(
                    self.solver,
                    mode="lp",
                    torque_z=True,
                    paper_knob_rule=True,
                    rotate_contact_pattern=True,
                    ground_pull=self.support == "baseplate",
                )
            case "stablelego-parity":
                return replace(
                    self.solver,
                    mode="lp",
                    torque_z=False,
                    paper_knob_rule=False,
                    rotate_contact_pattern=False,
                    ground_pull=True,
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class InstructionSection:
    """Instruction planning options."""

    options: InstructionsConfig = field(
        default_factory=lambda: InstructionsConfig(
            stability_policy="strict",
            insertion_check=True,
        )
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputConfig:
    """Default artifact policy."""

    manifest: bool = True
    emit_support: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CacheConfig:
    """Persistent architectural-template cache options."""

    enabled: bool = True
    path: Path | None = None

    def __post_init__(self) -> None:
        if self.path is not None and self.path.exists() and not self.path.is_dir():
            msg = f"cache.path must be a directory: {self.path}"
            raise ConfigurationError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogConfig:
    """Parts-catalog overlays and estimate sidecars."""

    extensions: tuple[Path, ...] = ()
    estimate_sidecars: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectConfig:
    """Complete strict configuration used by the redesigned CLI."""

    input: InputConfig = field(default_factory=InputConfig)
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    placement: PlacementConfig = field(default_factory=PlacementConfig)
    finishing: FinishingConfig = field(default_factory=FinishingConfig)
    stability: StabilityConfig = field(default_factory=StabilityConfig)
    instructions: InstructionSection = field(default_factory=InstructionSection)
    output: OutputConfig = field(default_factory=OutputConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)

    def __post_init__(self) -> None:
        if self.output.emit_support and self.stability.support != "baseplate":
            msg = "output.emit_support requires stability.support='baseplate'"
            raise ConfigurationError(msg)

    def to_pipeline(
        self,
        *,
        strategy: str,
        automatic: bool = False,
    ) -> PipelineConfig:
        """Compile the nested contract into the existing pipeline engine."""
        return PipelineConfig(
            strategy=strategy,
            hollow=self.geometry.hollow,
            hollow_rounds=self.geometry.hollow_rounds,
            hollow_restore_radius=self.geometry.hollow_restore_radius,
            slopes=self.finishing.slopes,
            tiles=self.finishing.tiles,
            refine=self.placement.refine,
            seed=self.placement.seed,
            plates_per_voxel=self.input.plates_per_voxel,
            aspect_correct=self.input.aspect_correct,
            shell_studs=self.geometry.shell_studs,
            shell_plates=self.geometry.shell_plates,
            ignore_interior=self.geometry.ignore_interior,
            colour_mode=self.placement.colour_mode,
            colour_weight=self.placement.colour_weight,
            dither=self.input.dither,
            time_budget_s=self.placement.time_budget_s,
            ga_generations=self.placement.ga_generations,
            beauty_preset=self.placement.beauty_preset,
            repair=self.stability.repair,
            repair_config=self.stability.repair_options,
            instructions=self.instructions.options,
            mesh=self.input.mesh,
            weights=self.placement.weights,
            solver=self.stability.effective_solver(),
            snot=self.finishing.snot,
            plate_cap=self.finishing.plate_cap,
            emit_support=self.output.emit_support,
            exact_max_cells=self.placement.exact.max_cells,
            exact_max_candidates=self.placement.exact.max_candidates,
            exact_max_stability_cuts=self.placement.exact.max_stability_cuts,
            exact_time_limit_s=self.placement.exact.time_limit_s,
            exact_limit_policy=self.placement.exact.limit_policy,
            exact_fallback_strategy=self.placement.exact.fallback_strategy,
            exact_auto_preflight_fallback=automatic,
            cost_objective=self.placement.objective,
            template_cache_enabled=self.cache.enabled,
            template_cache_path=self.cache.path,
            template_configuration_hash=mapping_hash(self.to_dict()),
            template_physics_profile=self.stability.profile,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible deterministic configuration mapping."""
        payload = asdict(self)
        input_payload = cast("dict[str, Any]", payload["input"])
        input_payload.pop("_target_studs_explicit")
        mesh_payload = cast("dict[str, Any]", input_payload["mesh"])
        if (
            self.input.mesh.auto_scale is not None
            and not self.input.target_studs_explicit
        ):
            mesh_payload.pop("target_studs")
        return cast("dict[str, Any]", _json_safe(payload))


def load_project_config(path: Path | None) -> ProjectConfig:
    """Load a strict TOML configuration, or return defaults when absent."""
    if path is None:
        return ProjectConfig()
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"cannot load configuration {path}: {error}"
        raise ConfigurationError(msg) from error
    if not isinstance(payload, dict):
        msg = "configuration root must be a TOML table"
        raise ConfigurationError(msg)
    return project_config_from_mapping(payload, base_dir=path.parent.resolve())


def mapping_hash(payload: dict[str, Any]) -> str:
    """Hash a JSON mapping with the manifest/cache canonical encoding."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def project_config_from_mapping(
    payload: dict[str, Any],
    *,
    base_dir: Path | None = None,
) -> ProjectConfig:
    """Construct a strict config from an already parsed mapping."""
    _reject_non_finite(payload, "configuration")
    _reject_unknown(payload, ProjectConfig, "configuration")
    input_data = _table(payload, "input")
    geometry_data = _table(payload, "geometry")
    placement_data = _table(payload, "placement")
    finishing_data = _table(payload, "finishing")
    stability_data = _table(payload, "stability")
    instructions_data = _table(payload, "instructions")
    output_data = _table(payload, "output")
    cache_data = _table(payload, "cache")
    catalog_data = _table(payload, "catalog")
    for key in ("extensions", "estimate_sidecars"):
        if key in catalog_data:
            catalog_data[key] = _path_tuple(
                catalog_data[key],
                f"catalog.{key}",
                base_dir,
            )

    mesh_data = _pop_table(input_data, "mesh")
    target_studs_explicit = "target_studs" in mesh_data
    if mesh_data.get("auto_scale") is not None and target_studs_explicit:
        msg = "input.mesh.auto_scale is incompatible with explicit target_studs"
        raise ConfigurationError(msg)
    mesh = _construct(MeshOptions, mesh_data, "input.mesh")
    exact = _construct(
        ExactPlacementConfig,
        _pop_table(placement_data, "exact"),
        "placement.exact",
    )
    weights = _construct(
        ObjectiveWeights,
        _pop_table(placement_data, "weights"),
        "placement.weights",
    )
    solver = _construct(
        SolverConfig,
        _pop_table(stability_data, "solver"),
        "stability.solver",
    )
    repair = _construct(
        RepairConfig,
        _pop_table(stability_data, "repair_options"),
        "stability.repair_options",
    )
    instruction_data = _pop_table(instructions_data, "options")
    _reject_unknown(instruction_data, InstructionsConfig, "instructions.options")
    try:
        instruction_options = replace(InstructionSection().options, **instruction_data)
    except (TypeError, ValueError) as error:
        msg = f"invalid instructions.options: {error}"
        raise ConfigurationError(msg) from error
    if "path" in cache_data and cache_data["path"] is not None:
        cache_path = Path(str(cache_data["path"])).expanduser()
        if not cache_path.is_absolute():
            cache_path = (base_dir or Path.cwd()) / cache_path
        cache_data["path"] = cache_path.resolve()

    input_data["mesh"] = mesh
    input_config = _construct(InputConfig, input_data, "input")
    input_config = replace(
        input_config,
        _target_studs_explicit=target_studs_explicit,
    )
    placement_data.update(exact=exact, weights=weights)
    stability_data.update(solver=solver, repair_options=repair)
    instructions_data["options"] = instruction_options
    return ProjectConfig(
        input=input_config,
        geometry=_construct(GeometryConfig, geometry_data, "geometry"),
        placement=_construct(PlacementConfig, placement_data, "placement"),
        finishing=_construct(FinishingConfig, finishing_data, "finishing"),
        stability=_construct(StabilityConfig, stability_data, "stability"),
        instructions=_construct(
            InstructionSection,
            instructions_data,
            "instructions",
        ),
        output=_construct(OutputConfig, output_data, "output"),
        cache=_construct(CacheConfig, cache_data, "cache"),
        catalog=_construct(CatalogConfig, catalog_data, "catalog"),
    )


def merge_overrides(
    config: ProjectConfig,
    overrides: dict[str, Any],
) -> ProjectConfig:
    """Apply dotted CLI overrides through the same strict parser."""
    payload = config.to_dict()
    if (
        overrides.get("input.mesh.auto_scale") is not None
        and "input.mesh.target_studs" not in overrides
        and not config.input.target_studs_explicit
    ):
        cast("dict[str, Any]", payload["input"]["mesh"]).pop("target_studs", None)
    for dotted, value in overrides.items():
        target = payload
        segments = dotted.split(".")
        for segment in segments[:-1]:
            child = target.get(segment)
            if not isinstance(child, dict):
                msg = f"unknown configuration key {dotted!r}"
                raise ConfigurationError(msg)
            target = child
        if segments[-1] not in target:
            msg = f"unknown configuration key {dotted!r}"
            raise ConfigurationError(msg)
        target[segments[-1]] = value
    return project_config_from_mapping(payload)


def _path_tuple(
    value: object,
    label: str,
    base_dir: Path | None,
) -> tuple[Path, ...]:
    """Parse a list of paths, resolving relative entries against the TOML."""
    if not isinstance(value, list | tuple):
        msg = f"{label} must be a list of paths"
        raise ConfigurationError(msg)
    resolved: list[Path] = []
    for entry in cast("list[object]", value):
        path = Path(str(entry)).expanduser()
        if not path.is_absolute():
            path = (base_dir or Path.cwd()) / path
        resolved.append(path.resolve())
    return tuple(resolved)


def _construct[T](cls: type[T], payload: dict[str, Any], label: str) -> T:
    _reject_unknown(payload, cls, label)
    try:
        return cls(**payload)
    except (TypeError, ValueError) as error:
        msg = f"invalid {label}: {error}"
        raise ConfigurationError(msg) from error


def _table(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        msg = f"{key} must be a TOML table"
        raise ConfigurationError(msg)
    return dict(cast("dict[str, Any]", value))


def _pop_table(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.pop(key, {})
    if not isinstance(value, dict):
        msg = f"{key} must be a TOML table"
        raise ConfigurationError(msg)
    return dict(cast("dict[str, Any]", value))


def _reject_unknown[T](payload: dict[str, Any], cls: type[T], label: str) -> None:
    dataclass_fields = cast(
        "dict[str, object]",
        getattr(cls, "__dataclass_fields__", {}),
    )
    allowed = {
        name
        for name, dataclass_field in dataclass_fields.items()
        if getattr(dataclass_field, "init", False) and not name.startswith("_")
    }
    if unknown := sorted(set(payload) - allowed):
        msg = f"unknown {label} keys: {', '.join(unknown)}"
        raise ConfigurationError(msg)


def _json_safe(value: object) -> object:
    match value:
        case Path():
            return str(value)
        case dict():
            return {str(key): _json_safe(item) for key, item in value.items()}
        case list() | tuple():
            return [_json_safe(item) for item in value]
        case _:
            return value


def _reject_non_finite(value: object, label: str) -> None:
    match value:
        case float() if not math.isfinite(value):
            msg = f"{label} contains a non-finite value"
            raise ConfigurationError(msg)
        case dict():
            for key, item in value.items():
                _reject_non_finite(item, f"{label}.{key}")
        case list() | tuple():
            for index, item in enumerate(value):
                _reject_non_finite(item, f"{label}[{index}]")
