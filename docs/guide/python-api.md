# Python API

Everything the CLI does is a thin wrapper over in-process functions. There is no
service, no daemon, and no network dependency in the pipeline itself.

```python
from legolization import (
    AnalysisConfig,
    AssemblyAnalysisConfig,
    MeshOptions,
    PipelineConfig,
    VoxelGrid,
    analyze_assembly,
    analyze_ldraw,
    mesh_to_grid,
    run,
    run_file,
)
```

!!! note "The CLI is where the guarantees live"

    Bundle directories, identity-based resume, detached workers, the candidate sweep,
    and the JSON envelope are all CLI-layer machinery. These functions are the engine
    underneath one candidate. If you want the sweep and the bundle, shell out to
    `legolization bundle --json` and read the envelope.

---

## Generate a model

```python
run_file(
    input_path: Path,
    output_path: Path,
    config: PipelineConfig | None = None,
    *,
    bom_path: Path | None = None,
    instructions_path: Path | None = None,
    grid: VoxelGrid | None = None,
    catalog: Catalog | None = None,
) -> PipelineResult
```

```python
from pathlib import Path
from legolization import PipelineConfig, run_file

result = run_file(Path("model.vox"), Path("model.ldr"), PipelineConfig(seed=1))
print(result.buildable, result.brick_count, result.stability.max_score)
```

To work with a grid you already have in memory:

```python
run(grid: VoxelGrid, config: PipelineConfig | None = None, *,
    catalog: Catalog | None = None) -> PipelineResult
```

### `PipelineResult`

| Field | Meaning |
| --- | --- |
| `layout` | The `Layout` — every placed brick, its part, colour, cell, and yaw |
| `stability` | `StabilityResult`: per-brick scores, `stable`, `max_score`, `unstable_ids`, `weakest_pair`, `min_capacity` |
| `grid` | The target `VoxelGrid` after hollowing |
| `brick_count`, `mass_g` | Totals |
| `component_count`, `floating_count` | Connectivity — both must be 1 and 0 for buildable |
| `slopes_added`, `tiles_added`, `snot_added`, `plate_caps_added` | Finishing-pass counts |
| `placement_strategy` | Which strategy actually ran (relevant after `auto` or a fallback) |
| `exact_status`, `exact_candidate_count` | Exact-placement outcome, when it ran |
| `support_ids` | Emitted support-plate bricks, when `output.emit_support` is on |
| `instruction_certification` | `InstructionCertification`: `valid`, `violations`, earliest failure |
| `plan` | The `InstructionPlan` — steps, subassemblies, ROTSTEP hints |
| `cache_provenance` | Template-cache hits, derivations, and rejections |

`buildable` is a property:

```python
buildable = (
    result.stability.stable
    and result.component_count == 1
    and result.floating_count == 0
)
```

---

## Load a grid

```python
from pathlib import Path
from legolization import MeshOptions, VoxelGrid, mesh_to_grid

grid = VoxelGrid.from_vox(Path("model.vox"))
grid = VoxelGrid.from_npy(Path("model.npy"))

grid = mesh_to_grid(
    Path("model.obj"),
    options=MeshOptions(target_studs=24, up="y", colour_mode="sampled"),
)
```

!!! warning "`MeshOptions()` defaults differ from the CLI"

    Constructing `MeshOptions()` directly gives `grid_phases=1`. The configuration
    layer builds `MeshOptions(grid_phases=8)`, so **the CLI default is 8**. Pass it
    explicitly if you want CLI-equivalent behaviour.

`VoxelGrid` is a frozen dataclass wrapping an int16 array of shape
`(nx, ny, n_plate_layers)`. Codes are LDraw colour numbers, `-1` for empty, and `-2`
for filled-but-colour-free. See [Representations](../theory/representations.md).

---

## Analyze an existing LDraw model

```python
analyze_ldraw(path: Path, config: AnalysisConfig | None = None) -> AnalysisResult
```

```python
from pathlib import Path
from legolization import AnalysisConfig, analyze_ldraw

analysis = analyze_ldraw(
    Path("existing.mpd"),
    AnalysisConfig(repair_time_budget_s=120, seed=7),
)
print(analysis.report.verdict)
print(analysis.report.to_json())
```

`AnalysisConfig` fields: `auto_ground`, `check_source_steps`, `repair`,
`repair_time_budget_s`, `seed`, `catalog_paths`, `estimate_sidecar_paths`,
`parity_solver`, `strict_solver`.

---

## Analyze an arbitrary assembly

The geometry-first path — arbitrary parts, MPD transforms, SNOT, angled mechanisms,
load scenarios.

```python
analyze_assembly(path: Path, config: AssemblyAnalysisConfig | None = None) -> AssemblyAnalysisResult
```

```python
from pathlib import Path
from legolization import AssemblyAnalysisConfig, analyze_assembly

assembly = analyze_assembly(
    Path("vehicle.mpd"),
    AssemblyAnalysisConfig(support="wheels", scenarios=("auto",)),
)
print(assembly.report.topology_verdict, assembly.report.physics_verdict)
```

`AssemblyAnalysisConfig` fields: `topology_only`, `support`, `scenarios`,
`gravity_g`, `side_load_g`, `torsion_load_g`, `path_between`,
`connector_catalog_paths`, `ldcad_metadata_paths`, `studio_metadata_paths`,
`voxel_catalog_paths`, `estimate_sidecar_paths`, `repair`, `repair_time_budget_s`,
`seed`, `infer_surface_contacts`, `auto_ground_strict`.

Topology and physics verdicts are **separate on purpose**. A model can be definitely
connected while its physics stays indeterminate because a part's load capacity is not
in the registry. See [The analysis stack](../theory/analysis-stack.md).

---

## Going lower

The public package also re-exports the subsystem modules, which is where you go to
build tooling rather than call the pipeline:

| Module | Contains |
| --- | --- |
| `legolization.stability` | The RBE model, LP solver, screens, warm prefix solver |
| `legolization.placement` | Every strategy, the merge engine, repair, finishing passes |
| `legolization.instructions` | Sequencer, chunking, blocking, subassemblies, BOM, booklet |
| `legolization.graph` | `ConnectionGraph`, contacts, component and floating analysis |
| `legolization.catalog` | Parts, categories, extension loading, catalog hashing |
| `legolization.ldraw_in` / `ldraw_out` | Reading and writing LDraw, including the stability heatmap writer |
| `legolization.ldraw_units` | `STUD_LDU`, `PLATE_LDU`, tolerances |
| `legolization.mesh`, `grid`, `hollow`, `layout` | Input handling and core data structures |

A useful example — write a stability heatmap and render it:

```python
from pathlib import Path
from legolization import run_file
from legolization.ldraw_out import write_heatmap

result = run_file(Path("model.vox"), Path("model.ldr"))
write_heatmap(result.layout, result.stability, Path("heatmap.ldr"))
```

Black bricks are at rest; dark red through red through light red is rising stress;
white is at or beyond capacity. The palette is quantized on purpose — headless LeoCAD
draws LDraw direct colours as grey.

---

## Determinism

Everything is seeded (default `0`). Mesh voxelization and the synthetic corpus
generators are fully deterministic, so identical inputs produce byte-identical
outputs. Exact placement ignores the RNG entirely.

Two caveats:

- The spawn process pool cannot hard-kill a running worker, so a timed-out sweep can
  return while workers still burn CPU. `bundle --cancel-pending` is the CLI answer.
- Solver-tolerance-level alternative optima can differ across solver versions on
  degenerate instances. Verdicts near the stability threshold are re-solved on the
  exact cold path for exactly this reason — see
  [Warm solving](../theory/stability/warm-solving.md).
