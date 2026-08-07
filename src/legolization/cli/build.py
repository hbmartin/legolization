"""The explicit single-strategy ``build`` command."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from legolization.cli.common import (
    add_catalog_options,
    add_config_options,
    add_json_option,
    require_file,
    require_output_path,
    resolve_config,
)
from legolization.cli.envelope import ArtifactRecord, ResultEnvelope
from legolization.cli.exit_codes import COMPLETE, UNBUILDABLE

if TYPE_CHECKING:
    from legolization.configuration import ProjectConfig
    from legolization.grid import VoxelGrid
    from legolization.pipeline import PipelineResult

BUILD_STRATEGIES = (
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


def configure(parser: argparse.ArgumentParser) -> None:
    """Register the ``build`` arguments and handler."""
    parser.add_argument(
        "input",
        type=Path,
        help="input .vox/.npy/.obj/.stl/.ply model or -prepared bundle directory",
    )
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--strategy", choices=BUILD_STRATEGIES)
    parser.add_argument("--seed", type=int)
    scale = parser.add_mutually_exclusive_group()
    scale.add_argument("--target-studs", type=_positive_int)
    scale.add_argument(
        "--auto-scale",
        type=_positive_int,
        nargs=2,
        metavar=("MIN", "MAX"),
    )
    parser.add_argument("--grid-phases", type=int, choices=(1, 2, 4, 8))
    parser.add_argument("--restarts", type=_positive_int)
    parser.add_argument("--jobs", type=_positive_int)
    parser.add_argument("--objective", choices=("bricks", "mass"))
    parser.add_argument(
        "--exact-limit-policy",
        choices=("fail", "fallback", "continue"),
    )
    manifest = parser.add_mutually_exclusive_group()
    manifest.add_argument("--manifest", type=Path)
    manifest.add_argument("--no-manifest", action="store_true")
    add_config_options(parser)
    add_catalog_options(parser)
    add_json_option(parser)
    parser.set_defaults(handler=_run, command_name="build")


def build_configuration(args: argparse.Namespace) -> ProjectConfig:
    """Resolve the effective configuration for a build-shaped namespace."""
    dedicated: dict[str, object] = {
        "placement.strategy": getattr(args, "strategy", None),
        "placement.seed": getattr(args, "seed", None),
        "placement.restarts": getattr(args, "restarts", None),
        "placement.jobs": getattr(args, "jobs", None),
        "placement.objective": getattr(args, "objective", None),
        "placement.exact.limit_policy": getattr(args, "exact_limit_policy", None),
        "input.mesh.target_studs": getattr(args, "target_studs", None),
        "input.mesh.auto_scale": getattr(args, "auto_scale", None),
        "input.mesh.grid_phases": getattr(args, "grid_phases", None),
    }
    config = resolve_config(args, dedicated=dedicated)
    if getattr(args, "target_studs", None) is not None:
        from legolization.configuration import merge_overrides  # noqa: PLC0415

        config = merge_overrides(config, {"input.mesh.auto_scale": None})
    return config


def select_strategy(config: ProjectConfig, grid: VoxelGrid) -> tuple[str, bool]:
    """Resolve ``auto`` to exact placement when the preflight allows it."""
    requested = config.placement.strategy
    if requested != "auto":
        return (requested, False)
    if grid.filled_count <= config.placement.exact.max_cells:
        return ("global-exact", True)
    return (config.placement.exact.fallback_strategy, False)


def _run(args: argparse.Namespace) -> ResultEnvelope:
    from dataclasses import replace  # noqa: PLC0415

    from legolization.catalog import resolve_catalog  # noqa: PLC0415
    from legolization.compare import restart_race  # noqa: PLC0415
    from legolization.inspection import resolve_prepared_input  # noqa: PLC0415
    from legolization.pipeline import load_grid, run_file  # noqa: PLC0415

    if resolve_prepared_input(args.input) is None:
        require_file(args.input, label="input")
    require_output_path(args.output)
    config = build_configuration(args)
    catalog = (
        resolve_catalog(
            config.catalog.extensions,
            config.catalog.estimate_sidecars,
        ).catalog
        if config.catalog.extensions or config.catalog.estimate_sidecars
        else None
    )
    grid = load_grid(args.input, config.to_pipeline(strategy="bond"))
    strategy, automatic = select_strategy(config, grid)
    pipeline = config.to_pipeline(strategy=strategy, automatic=automatic)
    if config.placement.restarts > 1:
        if strategy == "global-exact":
            from legolization.errors import ConfigurationError  # noqa: PLC0415

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
        config = replace(config, placement=replace(config.placement, seed=winner))
        pipeline = replace(pipeline, seed=winner)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = run_file(args.input, args.output, pipeline, grid=grid, catalog=catalog)
    manifest_path = _manifest_path(args, config=config)
    if manifest_path is not None:
        _write_build_manifest(
            result,
            config=config,
            input_path=args.input,
            output_path=args.output,
            manifest_path=manifest_path,
        )
    if not args.json:
        _print_summary(result, output=args.output, manifest=manifest_path)
    return _envelope(result, output=args.output, manifest=manifest_path)


def _envelope(
    result: PipelineResult,
    *,
    output: Path,
    manifest: Path | None,
) -> ResultEnvelope:
    artifacts = [ArtifactRecord(path=str(output), kind="model")]
    if manifest is not None:
        artifacts.append(ArtifactRecord(path=str(manifest), kind="manifest"))
    warnings = tuple(result.plan.warnings) if result.plan is not None else ()
    return ResultEnvelope(
        command="build",
        exit_code=COMPLETE if result.buildable else UNBUILDABLE,
        artifacts=tuple(artifacts),
        warnings=warnings,
        data={
            "strategy": result.placement_strategy,
            "brick_count": result.brick_count,
            "mass_g": result.mass_g,
            "step_count": result.step_count,
            "stable": result.stability.stable,
            "buildable": result.buildable,
        },
    )


def _manifest_path(
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
    from dataclasses import asdict  # noqa: PLC0415

    from legolization.catalog import DEFAULT_CATALOG_PATH  # noqa: PLC0415
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415
    from legolization.manifest import (  # noqa: PLC0415
        BuildManifestMetadata,
        manifest_for_build,
        write_manifest,
    )

    configuration = _configuration_payload(config)
    exactness: dict[str, Any] = {
        "catalog_sha256": input_sha256(DEFAULT_CATALOG_PATH),
        "cache_provenance": list(result.cache_provenance),
        "candidate_count": result.exact_candidate_count,
        "selected_seed": config.placement.seed,
        "status": result.exact_status or "heuristic",
        "strategy": result.placement_strategy,
    }
    if result.grid is not None and result.grid.mesh_features is not None:
        exactness["mesh_features"] = asdict(result.grid.mesh_features)
    manifest = manifest_for_build(
        result,
        BuildManifestMetadata(
            source_path=input_path,
            configuration=configuration,
            selected_strategy=result.placement_strategy,
            support_mode=config.stability.support,
            exactness=exactness,
            artifacts={"model_sha256": input_sha256(output_path)},
            status=(
                "partial"
                if result.instruction_certification is not None
                and not result.instruction_certification.valid
                else "complete"
            ),
        ),
    )
    write_manifest(manifest, manifest_path)


def _configuration_payload(config: ProjectConfig) -> dict[str, Any]:
    from legolization.configuration import mapping_hash  # noqa: PLC0415

    values = config.to_dict()
    return {"sha256": mapping_hash(values), "values": values}


def _print_summary(
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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        msg = "value must be positive"
        raise argparse.ArgumentTypeError(msg)
    return parsed
