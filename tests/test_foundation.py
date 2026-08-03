"""Foundation contracts for exact geometry, configuration, manifests, and cache."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from jsonschema import Draft202012Validator

from legolization.catalog import default_catalog
from legolization.commands import main as commands_main
from legolization.configuration import load_project_config, project_config_from_mapping
from legolization.errors import ConfigurationError
from legolization.grid import EMPTY, VoxelGrid
from legolization.instructions.blocking import directional_blockers
from legolization.layout import Layout
from legolization.manifest import MANIFEST_SCHEMA, manifest_json_schema, read_manifest
from legolization.physical import LduBox
from legolization.placement.slopes import apply_plate_caps
from legolization.placement.snot import (
    _carrier_for,
    _carrier_yaw,
    _cladding_for,
    _cladding_yaw,
    _ConnectorPose,
    _tile_connector_offset,
)
from legolization.placement.templates import (
    TemplateContext,
    instantiate_repeated_templates,
)
from legolization.repetition import repeated_components
from legolization.stability.incremental import FrozenBoundaryAnalyzer
from legolization.stability.solver import SolverConfig, analyze
from legolization.template_cache import TemplateCache, TemplateCacheKey


def test_strict_toml_resolves_cache_path_relative_to_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[cache]\npath = "state/templates"\n')

    config = load_project_config(config_path)

    assert config.cache.path == (tmp_path / "state" / "templates").resolve()


def test_strict_config_rejects_unknown_and_incompatible_options() -> None:
    with pytest.raises(ConfigurationError, match="unknown placement keys"):
        project_config_from_mapping({"placement": {"mystery": True}})
    with pytest.raises(ConfigurationError, match=r"requires placement\.time_budget_s"):
        project_config_from_mapping(
            {"placement": {"exact": {"limit_policy": "continue"}}}
        )
    with pytest.raises(ConfigurationError, match="non-finite"):
        project_config_from_mapping({"placement": {"colour_weight": float("nan")}})
    with pytest.raises(ConfigurationError, match="incompatible"):
        project_config_from_mapping(
            {"input": {"mesh": {"target_studs": 16, "auto_scale": [8, 24]}}}
        )


def test_corrected_profile_is_the_default() -> None:
    config = project_config_from_mapping({})
    solver = config.stability.effective_solver()

    assert solver.mode == "lp"
    assert solver.torque_z is True
    assert solver.paper_knob_rule is True
    assert solver.rotate_contact_pattern is True
    assert solver.ground_pull is True
    assert config.instructions.options.stability_policy == "strict"
    assert config.instructions.options.insertion_check is True


def test_exact_ldu_boxes_preserve_half_offsets_and_yaw() -> None:
    box = LduBox(minimum=(-10, -4, 0), maximum=(10, 4, 8))

    rotated = box.rotated_yaw(90).translated((10, 4, 4))

    assert rotated.minimum == (6, -6, 4)
    assert rotated.maximum == (14, 14, 12)
    assert not box.intersects(LduBox(minimum=(10, -4, 0), maximum=(20, 4, 8)))


def test_half_offset_catalog_parts_expose_exact_geometry() -> None:
    catalog = default_catalog()
    headlight = catalog["brick_1x1_headlight"]
    bracket = catalog["bracket_1x2_inverted_side_detail"]

    lateral = [item for item in headlight.top_connectors_ldu if item.direction[2] == 0]
    assert lateral[0].point == (0, 8, 12)
    assert {item.point[2] for item in bracket.bottom_connectors_ldu} == {16}
    assert len(bracket.collision_boxes_ldu) == 2


@pytest.mark.parametrize(
    ("columns", "expected_key", "offset_expected"),
    [
        (1, "brick_1x1_headlight", True),
        (2, "bracket_1x2_inverted_side_detail", False),
    ],
)
def test_compound_side_detail_templates_align_exact_connectors(
    columns: int,
    expected_key: str,
    offset_expected: bool,  # noqa: FBT001 - pytest supplies parameter sets positionally.
) -> None:
    catalog = default_catalog()
    face = (0, -1)
    carrier = _carrier_for(catalog, columns, prefer_compound=True)
    cladding = _cladding_for(catalog, columns)

    assert carrier is not None
    assert carrier.key == expected_key
    assert cladding is not None
    carrier_yaw = _carrier_yaw(carrier, face)
    tile_yaw = _cladding_yaw(cladding, face)
    assert carrier_yaw is not None
    assert tile_yaw is not None

    offset = _tile_connector_offset(
        _ConnectorPose(part=carrier, anchor=(0, 0, 0), yaw=carrier_yaw),
        _ConnectorPose(part=cladding, anchor=(0, -1, 0), yaw=tile_yaw),
    )

    assert offset is not None
    assert any(offset) is offset_expected


def test_inverted_slopes_fill_the_upper_profile() -> None:
    part = default_catalog()["slope_inverted_45_2x1"]

    assert (0, 0, 2) in part.filled_cells
    assert (0, 0, 0) not in part.filled_cells
    assert {item.cell for item in part.bottom_connectors} == {(0, 1, 0)}


def test_plate_caps_preserve_target_cells_while_splitting_bricks() -> None:
    layout = Layout(catalog=default_catalog())
    brick = layout.add("brick_2x2", 0, 0, 0, 0, 4)
    before = set(layout.filled_cells_of(brick))

    assert apply_plate_caps(layout) == 2

    assert len(layout) == 3
    assert {cell for item in layout for cell in layout.filled_cells_of(item)} == before


def test_template_cache_round_trip_inspection_and_quarantine(tmp_path: Path) -> None:
    cache = TemplateCache(root=tmp_path / "cache")
    key = TemplateCacheKey(
        component_signature="component",
        catalog_hash="catalog",
        configuration_hash="configuration",
        physics_profile="corrected",
        algorithm_version="1",
    )
    path = cache.write(key, {"placements": [1, 2, 3]})

    assert cache.read(key) == {"placements": [1, 2, 3]}
    assert [entry.key for entry in cache.inspect()] == [key.digest]

    path.write_text("corrupt")
    assert cache.read(key) is None
    assert tuple((cache.root / ".corrupt").iterdir())


def test_repeated_components_accept_yaw_and_colour_remapping() -> None:
    codes = np.full((7, 7, 1), EMPTY, dtype=np.int16)
    codes[0, 0, 0] = codes[1, 0, 0] = 4
    codes[0, 1, 0] = 1
    codes[5, 5, 0] = codes[5, 6, 0] = 2
    codes[4, 5, 0] = 14

    repeated = repeated_components(VoxelGrid(codes=codes))

    assert len(repeated) == 1
    assert len(repeated[0].instances) == 2
    yaws = [instance.canonical_yaw for instance in repeated[0].instances]
    assert (yaws[0] - yaws[1]) % 360 in {90, 270}


def test_repeated_component_uses_one_cached_canonical_placement(
    tmp_path: Path,
) -> None:
    codes = np.full((6, 1, 3), EMPTY, dtype=np.int16)
    codes[0:2, 0, :] = 4
    codes[4:6, 0, :] = 2
    grid = VoxelGrid(codes=codes)
    repeated = repeated_components(grid)
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", 4, 0, 0, 0, 2)
    layout.add("brick_1x1", 5, 0, 0, 0, 2)
    cache = TemplateCache(root=tmp_path / "templates")
    context = TemplateContext(
        catalog_hash="catalog",
        configuration_hash="configuration",
        physics_profile="corrected",
    )

    first = instantiate_repeated_templates(
        layout,
        repeated,
        cache=cache,
        context=context,
    )
    second = instantiate_repeated_templates(
        layout,
        repeated,
        cache=cache,
        context=context,
    )

    assert [item.status for item in first] == ["write"]
    assert [item.status for item in second] == ["hit"]
    assert sorted(brick.part_key for brick in layout) == ["brick_1x2", "brick_1x2"]
    assert sorted(brick.colour_code for brick in layout) == [2, 4]


def test_build_writes_byte_stable_manifest_and_validate_accepts_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tiny.npy"
    output = tmp_path / "tiny.ldr"
    np.save(source, np.full((2, 2, 3), 4, dtype=np.int16))

    assert commands_main(["build", str(source), "-o", str(output)]) == 0
    manifest_path = Path(f"{output}.manifest.json")
    first = manifest_path.read_bytes()

    assert commands_main(["build", str(source), "-o", str(output)]) == 0
    assert manifest_path.read_bytes() == first
    assert commands_main(["validate", str(manifest_path)]) == 0

    manifest = read_manifest(manifest_path)
    payload = json.loads(first)
    Draft202012Validator(manifest_json_schema()).validate(payload)
    assert manifest.schema == MANIFEST_SCHEMA
    assert payload["configuration"]["sha256"]
    assert payload["exactness"]["catalog_sha256"]
    assert payload["exactness"]["status"] == "optimal"
    assert payload["occurrences"][0]["collision_volumes_ldu"]


def test_explicit_exact_limit_has_dedicated_exit_code(tmp_path: Path) -> None:
    source = tmp_path / "too-large.npy"
    output = tmp_path / "too-large.ldr"
    config = tmp_path / "config.toml"
    np.save(source, np.full((2, 1, 1), 4, dtype=np.int16))
    config.write_text(
        '[placement]\nstrategy = "global-exact"\n[placement.exact]\nmax_cells = 1\n'
    )

    code = commands_main(
        ["build", str(source), "-o", str(output), "--config", str(config)]
    )

    assert code == 4
    assert not output.exists()


def test_emitted_support_is_physical_but_not_target_geometry(tmp_path: Path) -> None:
    source = tmp_path / "supported.npy"
    output = tmp_path / "supported.ldr"
    config = tmp_path / "support.toml"
    np.save(source, np.full((2, 2, 3), 4, dtype=np.int16))
    config.write_text("[output]\nemit_support = true\n")

    code = commands_main(
        ["build", str(source), "-o", str(output), "--config", str(config)]
    )

    assert code == 0
    manifest = read_manifest(Path(f"{output}.manifest.json"))
    support_ids = set(manifest.support["occurrence_ids"])
    support_occurrences = [
        item for item in manifest.occurrences if item["occurrence_id"] in support_ids
    ]
    model_occurrences = [
        item
        for item in manifest.occurrences
        if item["occurrence_id"] not in support_ids
    ]
    instruction_ids = {
        occurrence_id
        for step in manifest.action_graph["steps"]
        for occurrence_id in step["occurrence_ids"]
    }

    assert manifest.support["physical_parts_emitted"] is True
    assert support_occurrences
    assert all(item["role"] == "support" for item in support_occurrences)
    assert all(not item["target_cells"] for item in support_occurrences)
    assert (
        min(cell[2] for item in model_occurrences for cell in item["target_cells"]) == 0
    )
    assert support_ids <= instruction_ids
    assert manifest.stability["instruction_certification"]["status"] == "complete"


def test_directional_blockers_cover_all_six_exact_sweeps() -> None:
    layout = Layout(catalog=default_catalog())
    lower = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    upper = layout.add("brick_1x1", 0, 0, 3, 0, 4)
    right = layout.add("brick_1x1", 1, 0, 0, 0, 4)

    blockers = directional_blockers(layout)

    assert upper.brick_id in blockers["z+"][lower.brick_id]
    assert lower.brick_id in blockers["z-"][upper.brick_id]
    assert right.brick_id in blockers["x+"][lower.brick_id]
    assert lower.brick_id in blockers["x-"][right.brick_id]
    assert not blockers["y+"][lower.brick_id]
    assert not blockers["y-"][lower.brick_id]


def test_frozen_boundary_screen_preserves_forces_and_requires_cold_solve() -> None:
    layout = Layout(catalog=default_catalog())
    layout.add("brick_2x2", 0, 0, 0, 0, 4)
    upper = layout.add("brick_2x2", 0, 0, 3, 0, 4)
    config = SolverConfig()
    baseline = analyze(layout, config)
    analyzer = FrozenBoundaryAnalyzer.create(
        layout,
        config=config,
        result=baseline,
        k_rings=0,
    )
    recoloured = layout.copy()
    recoloured.bricks[upper.brick_id] = replace(upper, colour_code=1)

    certification = analyzer.certify(
        recoloured,
        changed_ids=(upper.brick_id,),
    )

    assert certification.screen.feasible
    assert certification.screen.frozen_forces
    assert certification.cold_result == analyze(recoloured, config)

    disconnected = recoloured.copy()
    disconnected.remove(upper.brick_id)
    rejected = analyzer.certify(
        disconnected,
        changed_ids=(upper.brick_id,),
    )
    assert not rejected.screen.feasible
    assert rejected.cold_result is None
