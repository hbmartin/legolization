"""Explicit build, validation, and cache command orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from legolization.catalog import DEFAULT_CATALOG_PATH
from legolization.compare import restart_race
from legolization.configuration import (
    ProjectConfig,
    load_project_config,
    merge_overrides,
)
from legolization.errors import (
    ConfigurationError,
    ExactPlacementLimitError,
    LegolizationError,
    ManifestError,
    PlacementInfeasibleError,
)
from legolization.manifest import (
    BuildManifestMetadata,
    manifest_for_build,
    read_manifest,
    validate_manifest,
    write_manifest,
)
from legolization.pipeline import load_grid, run_file
from legolization.template_cache import TemplateCache

if TYPE_CHECKING:
    from legolization.grid import VoxelGrid
    from legolization.pipeline import PipelineResult

_BUILD_STRATEGIES = (
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


def main(argv: list[str]) -> int:
    """Dispatch one redesigned explicit subcommand."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "build":
                return _build(args)
            case "validate":
                return _validate(args)
            case "cache":
                return _cache(args)
            case _:
                parser.error("a command is required")
    except ExactPlacementLimitError as error:
        _error(error)
        return 4
    except PlacementInfeasibleError as error:
        _error(error)
        return 2
    except (
        ConfigurationError,
        ManifestError,
        LegolizationError,
        OSError,
        ValueError,
    ) as error:
        _error(error)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legolization")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build an LDraw model from 3D input")
    build.add_argument("input", type=Path)
    build.add_argument("-o", "--output", type=Path, required=True)
    build.add_argument("--config", type=Path)
    build.add_argument("--strategy", choices=_BUILD_STRATEGIES)
    build.add_argument("--seed", type=int)
    scale = build.add_mutually_exclusive_group()
    scale.add_argument("--target-studs", type=_positive_int)
    scale.add_argument(
        "--auto-scale", type=_positive_int, nargs=2, metavar=("MIN", "MAX")
    )
    build.add_argument("--grid-phases", type=int, choices=(1, 2, 4, 8))
    build.add_argument("--restarts", type=_positive_int)
    build.add_argument("--jobs", type=_positive_int)
    build.add_argument("--objective", choices=("bricks", "mass"))
    build.add_argument(
        "--exact-limit-policy",
        choices=("fail", "fallback", "continue"),
    )
    manifest = build.add_mutually_exclusive_group()
    manifest.add_argument("--manifest", type=Path)
    manifest.add_argument("--no-manifest", action="store_true")

    validate = commands.add_parser("validate", help="validate an assembly manifest")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--against", type=Path)

    cache = commands.add_parser("cache", help="inspect or clear template cache")
    cache.add_argument("--config", type=Path)
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_commands.add_parser("inspect")
    clear = cache_commands.add_parser("clear")
    clear_target = clear.add_mutually_exclusive_group(required=True)
    clear_target.add_argument("--key")
    clear_target.add_argument("--all", action="store_true")
    return parser


def _build(args: argparse.Namespace) -> int:
    _require_file(args.input, label="input")
    _require_output_path(args.output)
    config = _configuration(args)
    grid = load_grid(args.input, config.to_pipeline(strategy="bond"))
    strategy, automatic = _select_strategy(config, grid)
    pipeline = config.to_pipeline(strategy=strategy, automatic=automatic)
    if config.placement.restarts > 1:
        if strategy == "global-exact":
            msg = "deterministic restarts apply only to heuristic placement"
            raise ConfigurationError(msg)
        seeds = tuple(
            range(
                config.placement.seed,
                config.placement.seed + config.placement.restarts,
            )
        )
        winner, _ = restart_race(
            grid,
            pipeline,
            seeds=seeds,
            jobs=config.placement.jobs,
            timeout_s=config.placement.time_budget_s,
        )
        config = replace(
            config,
            placement=replace(config.placement, seed=winner),
        )
        pipeline = replace(pipeline, seed=winner)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = run_file(
        args.input,
        args.output,
        pipeline,
        grid=grid,
    )
    manifest_path = _build_manifest_path(args, config=config)
    if manifest_path is not None:
        _write_build_manifest(
            result,
            config=config,
            input_path=args.input,
            output_path=args.output,
            manifest_path=manifest_path,
        )
    _build_summary(result, output=args.output, manifest=manifest_path)
    return 0 if result.buildable else 2


def _configuration(args: argparse.Namespace) -> ProjectConfig:
    config_path = getattr(args, "config", None)
    if config_path is not None:
        _require_file(config_path, label="configuration")
    config = load_project_config(config_path)
    overrides = {
        key: value
        for key, value in {
            "placement.strategy": getattr(args, "strategy", None),
            "placement.seed": getattr(args, "seed", None),
            "placement.restarts": getattr(args, "restarts", None),
            "placement.jobs": getattr(args, "jobs", None),
            "placement.objective": getattr(args, "objective", None),
            "placement.exact.limit_policy": getattr(
                args,
                "exact_limit_policy",
                None,
            ),
            "input.mesh.target_studs": getattr(args, "target_studs", None),
            "input.mesh.auto_scale": getattr(args, "auto_scale", None),
            "input.mesh.grid_phases": getattr(args, "grid_phases", None),
        }.items()
        if value is not None
    }
    if getattr(args, "target_studs", None) is not None:
        overrides["input.mesh.auto_scale"] = None
    return merge_overrides(config, overrides) if overrides else config


def _select_strategy(config: ProjectConfig, grid: VoxelGrid) -> tuple[str, bool]:
    requested = config.placement.strategy
    if requested != "auto":
        return (requested, False)
    if grid.filled_count <= config.placement.exact.max_cells:
        return ("global-exact", True)
    return (config.placement.exact.fallback_strategy, False)


def _build_manifest_path(
    args: argparse.Namespace,
    *,
    config: ProjectConfig,
) -> Path | None:
    if args.no_manifest or (args.manifest is None and not config.output.manifest):
        return None
    return args.manifest or Path(f"{args.output}.manifest.json")


def _write_build_manifest(
    result: PipelineResult,
    *,
    config: ProjectConfig,
    input_path: Path,
    output_path: Path,
    manifest_path: Path,
) -> None:
    configuration = _configuration_payload(config)
    catalog_hash = _sha256(DEFAULT_CATALOG_PATH)
    exactness: dict[str, Any] = {
        "catalog_sha256": catalog_hash,
        "cache_provenance": list(result.cache_provenance),
        "candidate_count": result.exact_candidate_count,
        "selected_seed": config.placement.seed,
        "status": result.exact_status or "heuristic",
        "strategy": result.placement_strategy,
    }
    if result.grid is not None and result.grid.mesh_features is not None:
        exactness["mesh_features"] = asdict(result.grid.mesh_features)
    artifact_hash = _sha256(output_path)
    manifest = manifest_for_build(
        result,
        BuildManifestMetadata(
            source_path=input_path,
            configuration=configuration,
            selected_strategy=result.placement_strategy,
            support_mode=config.stability.support,
            exactness=exactness,
            artifacts={"model_sha256": artifact_hash},
        ),
    )
    write_manifest(manifest, manifest_path)


def _configuration_payload(config: ProjectConfig) -> dict[str, Any]:
    values = config.to_dict()
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return {
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "values": values,
    }


def _validate(args: argparse.Namespace) -> int:
    _require_file(args.manifest, label="manifest")
    if args.against is not None:
        _require_file(args.against, label="comparison model")
    manifest = read_manifest(args.manifest)
    if errors := validate_manifest(manifest, against=args.against):
        for message in errors:
            _error(message)
        return 1
    print(f"valid {manifest.schema} version {manifest.version}")
    return 3 if manifest.status == "partial" else 0


def _cache(args: argparse.Namespace) -> int:
    config = _configuration(args)
    cache = (
        TemplateCache(root=config.cache.path)
        if config.cache.path is not None
        else TemplateCache.default()
    )
    match args.cache_command:
        case "inspect":
            payload = [
                {
                    "key": entry.key,
                    "payload_sha256": entry.payload_sha256,
                    "size_bytes": entry.size_bytes,
                }
                for entry in cache.inspect()
            ]
            print(json.dumps(payload, indent=2, sort_keys=True))
        case "clear":
            removed = cache.clear(key=args.key, all_entries=args.all)
            print(f"removed {removed} cache entr{'y' if removed == 1 else 'ies'}")
    return 0


def _require_file(path: Path, *, label: str) -> None:
    if not path.is_file():
        msg = f"{label} must be an existing file: {path}"
        raise ConfigurationError(msg)


def _require_output_path(path: Path) -> None:
    if path.exists() and not path.is_file():
        msg = f"output must be a file path: {path}"
        raise ConfigurationError(msg)
    if path.suffix.lower() not in {".ldr", ".mpd"}:
        msg = "output must end in .ldr or .mpd"
        raise ConfigurationError(msg)


def _build_summary(
    result: PipelineResult,
    *,
    output: Path,
    manifest: Path | None,
) -> None:
    print(
        f"{result.placement_strategy}: {result.brick_count} parts, "
        f"stable={result.stability.stable}, buildable={result.buildable}"
    )
    print(f"wrote {output}")
    if manifest is not None:
        print(f"wrote {manifest}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        msg = "value must be positive"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def _error(error: object) -> None:
    print(f"error: {error}", file=sys.stderr)
