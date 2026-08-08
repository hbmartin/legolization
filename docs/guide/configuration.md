# Configuration reference

Configuration is strict nested TOML. Every key below is real, every default is taken
from the dataclass that owns it, and anything not listed here is an error.

---

## How values are resolved

```
built-in defaults  →  --config FILE  →  --set KEY=VALUE  →  explicit CLI flags
```

Only flags the user **explicitly supplied** participate in the last step. An omitted
`--seed` does not overwrite a TOML `placement.seed` with the parser's default.

```sh
legolization bundle model.obj --config legolization.toml
legolization bundle model.obj --set geometry.hollow=false --set placement.seed=7
```

`--set` accepts one dotted key per occurrence and is repeatable.

### `--set` values are TOML

The right-hand side is parsed as a TOML value, so types come out right:

| You write | You get |
| --- | --- |
| `--set geometry.hollow=false` | boolean `false` |
| `--set placement.seed=7` | integer `7` |
| `--set placement.colour_weight=2.5` | float `2.5` |
| `--set placement.strategy=luo` | string `"luo"` |
| `--set input.mesh.auto_scale=[16,32]` | array `[16, 32]` |

A value that fails TOML parsing falls back to a bare string, which is why bare
identifiers like `luo` work. A missing `=`, or an empty key, is an error.

### Paths are relative to the TOML file

`cache.path`, `catalog.extensions`, and `catalog.estimate_sidecars` resolve against
the directory containing the `--config` file — not the current working directory. A
project config stays valid no matter where you invoke it from.

### Validation happens before work starts

- **Unknown keys fail.** A typo is an error, never a silently ignored setting.
- **Non-finite values fail.** `NaN` and `inf` anywhere are rejected.
- **Sections must be tables.**
- **Cross-field rules** (below) fail at parse time, not after 20 minutes of solving.

### Cross-field rules

| Rule | Message |
| --- | --- |
| `input.mesh.auto_scale` with an explicit `target_studs` | incompatible |
| `input.mesh.auto_scale` with an explicit `pitch` | incompatible |
| `output.emit_support = true` requires `stability.support = "baseplate"` | `output.emit_support requires stability.support='baseplate'` |
| `placement.exact.limit_policy = "continue"` requires `placement.time_budget_s` | `exact continue policy requires placement.time_budget_s` |
| `stability.solver.mode` must be `"lp"` | `stability.solver.mode is production LP only` |
| `instructions.options`: `min_step_size ≤ target_step_size ≤ max_step_size` | `step sizes must satisfy minimum <= target <= maximum` |

### Configuration hash

The effective configuration is hashed (SHA-256 of its canonical JSON) into bundle
identity and manifests. The `cache` and `output` sections are excluded from that
hash, so a bundle resumes across machines with different cache locations.

---

## A worked example

```toml
# legolization.toml

[input]
plates_per_voxel = 3

[input.mesh]
target_studs = 28
up = "y"                    # most .obj files are y-up
colour_mode = "sampled"

[geometry]
hollow = true
shell_plates = 3

[placement]
strategy = "auto"
objective = "bricks"
colour_mode = "soft"
restarts = 1

[placement.exact]
max_cells = 256
time_limit_s = 60
limit_policy = "fail"

[finishing]
slopes = true
tiles = true
snot = true                 # opt-in sideways cladding

[stability]
profile = "corrected"
support = "baseplate"

[instructions.options]
target_step_size = 7
stability_policy = "strict"

[output]
manifest = true
```

---

## `[input]`

Voxel and mesh input options.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `plates_per_voxel` | int > 0 | `3` | Vertical plate layers per source voxel. `3` makes one voxel one brick tall. |
| `aspect_correct` | bool | `false` | Stretch voxel grids to correct the 2.5 plates-per-stud aspect ratio. Mesh inputs are aspect-correct regardless. |
| `dither` | bool | `false` | Floyd–Steinberg colour dithering per horizontal slice. Error diffuses only into filled cells of the same slice. |

## `[input.mesh]`

Mesh voxelization. Ignored for `.vox` / `.npy` inputs.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `target_studs` | int > 0 | `32` | Footprint width in studs. Incompatible with `auto_scale`. |
| `pitch` | float > 0 \| `null` | `null` | Model units per stud. Overrides `target_studs`. Incompatible with `auto_scale`. |
| `up` | `"x"` \| `"y"` \| `"z"` | `"z"` | The mesh's vertical axis. Most `.obj` files are **y-up**. |
| `colour_code` | int | `7` (light grey) | Uniform colour, and the fallback when a mesh carries no colour data. |
| `colour_mode` | `"uniform"` \| `"sampled"` | `"uniform"` | `sampled` colours each voxel from the nearest mesh vertex — texture and vertex colours both work. |
| `fill` | bool | `true` | Flood the enclosed volume. Disable for shell meshes. |
| `keep_largest` | bool | `false` | Keep only the largest connected component. Disconnected components are preserved by default. |
| `auto_scale` | `[min, max]` \| `null` | `null` | Search a stud range instead of fixing one size. Requires `0 < min ≤ max`. |
| `grid_phases` | `1` \| `2` \| `4` \| `8` | **`8`** | Half-cell sampling offsets tried when selecting a voxelization. |

!!! warning "`grid_phases` has two different defaults"

    The `MeshOptions` dataclass defaults it to `1`, but `InputConfig` constructs
    `MeshOptions(grid_phases=8)`. **Through configuration and the CLI, the default
    is 8.** Only a direct `MeshOptions()` construction in the Python API gets `1`.

## `[geometry]`

Target-shell construction. All integers must be non-negative.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `hollow` | bool | `true` | Erode the interior to a shell before placement. |
| `hollow_rounds` | int ≥ 0 | `5` | Maximum add-material rounds when repair alone cannot stabilize the layout. |
| `hollow_restore_radius` | int ≥ 0 | `2` | Chebyshev radius around a trouble column whose interior fill is restored. |
| `shell_studs` | int ≥ 0 | `1` | Lateral shell thickness in studs. |
| `shell_plates` | int ≥ 0 | `3` | Vertical shell thickness in plates. `3` ≈ one brick. |
| `ignore_interior` | bool | `true` | Mark interior cells colour-free so merges do not fragment on invisible boundaries. |

## `[placement]`

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `strategy` | `auto`, `global-exact`, `greedy`, `luo`, `bond`, `fast`, `smga`, `beauty`, `kollsker` | `"auto"` | See [Choosing a strategy](choosing-a-strategy.md). `auto` picks `global-exact` below the cell cap, `placement.exact.fallback_strategy` otherwise. |
| `seed` | int | `0` | Deterministic RNG seed. |
| `restarts` | int > 0 | `1` | Multi-seed race over `[seed, seed+restarts)`. **Error if the strategy resolves to `global-exact`.** |
| `jobs` | int > 0 | `1` | Parallelism for the restart race. |
| `refine` | bool | `true` | Run the strategy's refinement/reinforcement pass. |
| `time_budget_s` | float > 0 \| `null` | `null` | Per-strategy soft budget. Required by `exact.limit_policy = "continue"`. |
| `objective` | `"bricks"` \| `"mass"` | `"bricks"` | Minimize part count or total mass. |
| `colour_mode` | `"hard"` \| `"soft"` | `"hard"` | `soft` permits probabilistically accepted miscolouring merges. |
| `colour_weight` | float ≥ 0 | `1.0` | Discard weight in soft-colour sampling. Larger ⇒ closer to hard. |
| `ga_generations` | int > 0 | `200` | Generation cap for `smga`. |
| `beauty_preset` | `balanced`, `stability`, `aesthetics`, `efficiency` | `"balanced"` | Weight preset for `beauty`. |

## `[placement.exact]`

Guardrails for whole-model exact placement.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `max_cells` | int > 0 | `256` | Preflight cap on filled cells. Also the `auto` selector's threshold. |
| `max_candidates` | int > 0 | `100000` | Cap on enumerated placement candidates. |
| `max_stability_cuts` | int > 0 | `256` | Cap on stability no-good cuts before giving up. |
| `time_limit_s` | float > 0 | `60.0` | MILP solve time limit. |
| `limit_policy` | `"fail"` \| `"fallback"` \| `"continue"` | `"fail"` | What happens at a limit. `fail` exits 4; `fallback` switches strategy; `continue` keeps going until the deadline (requires `placement.time_budget_s`). |
| `fallback_strategy` | `"bond"` \| `"fast"` \| `"greedy"` | `"bond"` | Strategy used by `fallback`, and by `auto` above the cell cap. |

## `[placement.weights]`

Relative importance of the objective terms. All must be finite and non-negative.
The objective is a weighted sum of terms each normalized to roughly `[0, 1]`; lower
is better. See [Placement](../theory/placement/index.md).

| Key | Default | Term |
| --- | ---: | --- |
| `cost` | `1.0` | Parts used per filled voxel |
| `stability` | `4.0` | Worst per-brick stress from the RBE |
| `aesthetics` | `0.5` | Seam alignment — stacked seams are penalized |
| `colour` | `1.0` | Fraction of covered voxels whose brick colour is wrong |
| `perpendicularity` | `0.25` | Fraction of support pairs whose long axes are parallel |
| `symmetry` | `0.25` | Per-layer unbalanced-brick fraction |
| `bond_alpha1` | `4.0` | Stretcher-bond penalty magnitude |
| `bond_alpha2` | `0.8` | Stretcher-bond distance decay |

## `[finishing]`

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `slopes` | bool | `true` | Shape-preserving slope substitution, including inverted slopes. |
| `tiles` | bool | `true` | Replace exposed plates with tiles. |
| `snot` | bool | **`false`** | Sideways cladding on tall flat wall faces. Opt-in. |
| `plate_cap` | bool | **`false`** | Split exposed bricks into three plates. Opt-in — it changes layering and part count. |

Neither slopes nor tiles may change target occupancy, and every finishing pass is
reverted wholesale if it would flip the stability verdict.

## `[stability]`

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `profile` | `"corrected"` \| `"stablelego-parity"` | `"corrected"` | Physics profile — see below. |
| `support` | `"baseplate"` \| `"table"` | `"baseplate"` | `baseplate` lets ground contacts pull down; `table` models bricks resting loose, so top-heavy shapes may tip. |
| `repair` | bool | `true` | Run ALNS destroy-and-repair on unstable layouts. |

The profile is not a free-form switch set — it *derives* the solver flags:

| Profile | `torque_z` | `paper_knob_rule` | `rotate_contact_pattern` | `ground_pull` |
| --- | --- | --- | --- | --- |
| `corrected` *(default)* | `true` | `true` | `true` | `support == "baseplate"` |
| `stablelego-parity` | `false` | `false` | `false` | `true` |

`stablelego-parity` exists to reproduce the StableLego paper's numbers. Use
`corrected` for real work.

## `[stability.solver]`

Advanced. Most of these exist for research and cross-checking.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `mode` | `"lp"` | `"lp"` | Production is LP only; any other value is rejected. |
| `solver` | str \| `null` | `null` | cvxpy backend name (MILP debug mode only). |
| `tol_force` | float | `1e-6` | Force-residual equilibrium tolerance. |
| `tol_torque` | float | `1e-7` | Torque-residual equilibrium tolerance. |
| `drag_big_m` | float | `9.8` (`10 × T`) | Big-M ceiling for MILP complementarity only. Never constrains LP mode. |
| `normal_big_m` | float | `100.0` | As above, for normal forces. |
| `engine` | `"highspy"` \| `"scipy"` | `"highspy"` | Prefix-solve engine. `highspy` warm-starts incrementally; `scipy` keeps every solve cold. |
| `engine_cross_check` | bool | `false` | Cold-solve every warm probe and record drift. Debug/CI. |
| `boundary_margin` | float | `0.02` | Relative band around the stability threshold within which a warm verdict is discarded for a cold solve. |
| `rescue_direct_min_bricks` | int | `200` | Rescue components at or above this size cold-solve through highspy directly. |
| `torque_z` | bool | `true` | Model the yaw-torque row. Overridden by `profile`. |
| `paper_knob_rule` | bool | `true` | StableLego *paper* knob rule instead of the release's uniform rule. Overridden by `profile`. |
| `rotate_contact_pattern` | bool | `true` | Rotate the contact pattern with brick yaw, restoring rotation invariance. Overridden by `profile`. |
| `ground_pull` | bool | `true` | Whether layer-0 contacts may pull down. Derived from `stability.support`. |

## `[stability.repair_options]`

ALNS destroy-and-repair knobs (Kollsker's defaults).

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `beta0` | float ≥ 0 | `0.8` | Initial destroy threshold. |
| `gamma` | float ≥ 0 | `0.5` | Per-round threshold decay. |
| `epsilon` | float ≥ 0 | `0.05` | Threshold slack when selecting victims. |
| `max_rounds` | int ≥ 0 | `12` | Maximum destroy-and-repair rounds. |
| `localizer` | `"qp"` \| `"rbe"` | `"qp"` | Artificial-link QP, or per-brick RBE scores. |
| `filler` | `"merge"` \| `"milp"` | `"merge"` | How the freed region is refilled. |
| `milp_cell_limit` | int > 0 | `200` | Above this many freed cells, the MILP filler is skipped. |

## `[instructions.options]`

!!! note "Two defaults differ from the dataclass"

    Through configuration, `stability_policy` defaults to **`"strict"`** and
    `insertion_check` to **`true`**, overriding the `InstructionsConfig` dataclass
    defaults of `"warn"` and `false`. The table below shows the **configuration**
    defaults, which are what the CLI uses.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `mode` | `"smart"` \| `"layer"` | `"smart"` | `smart` chunks each layer into spatially coherent steps; `layer` emits one step per layer. |
| `target_step_size` | int > 0 | `7` | Preferred bricks per step. |
| `max_step_size` | int > 0 | `10` | Hard cap per step. |
| `min_step_size` | int > 0 | `3` | Undersized tails below this fold into the previous step. |
| `rotstep` | bool | `true` | Emit `0 ROTSTEP` view hints. |
| `beam_width` | int > 0 | `4` | Ready-chunk candidates examined per step. |
| `stability_policy` | `"warn"` \| `"strict"` | **`"strict"`** | `strict` raises on an unstable prefix; `warn` emits it with a warning. |
| `solver` | table \| `null` | `null` | Override the solver config for sequencing only. |
| `spatial_tiebreak` | bool | `true` | Prefer steps spatially adjacent to the previous one. |
| `fallback` | `"disassembly"` \| `"band"` | `"disassembly"` | Rescue path when the greedy pass stalls. |
| `search` | `"greedy"` \| `"beam"` | `"greedy"` | `beam` explores whole build orders — much slower. |
| `beam_states` | int > 0 | `3` | Beam width in states, for `search = "beam"`. |
| `lp_budget` | int > 0 \| `null` | `null` | LP spend cap in beam mode. `null` = 8 × chunk count; at the cap, beam degrades to greedy. |
| `subassemblies` | bool | `true` | Lift persistently floating stretches into separately built units. |
| `min_sub_bricks` | int > 0 | `3` | Minimum bricks for a subassembly. |
| `max_subassemblies` | int > 0 | `6` | Cap on extracted units. |
| `insertion_check` | bool | **`true`** | Prefer press-robust orderings; flag steps that collapse when pressed home. |
| `insertion_mass_kg` | float > 0 | `1.0` | Virtual mass used to model the insertion press. |

## `[output]`

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `manifest` | bool | `true` | Write the canonical assembly manifest sidecar. |
| `emit_support` | bool | `false` | Emit a physical support plate. **Requires `stability.support = "baseplate"`.** |

## `[cache]`

Persistent architectural-template cache.

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Reuse derived placements for repeated components. |
| `path` | path \| `null` | `null` | Cache root. `null` uses platform user-data storage. Must be a directory if it exists. |

Inspect and clear it with [`legolization cache`](cli/parts-cache-validate.md).

## `[catalog]`

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `extensions` | list of paths | `[]` | Parts-catalog extension bundles or JSON files. |
| `estimate_sidecars` | list of paths | `[]` | Physical-estimate sidecars with labeled provenance. |

`--catalog` and `--catalog-estimates` on the command line **append** to these lists
in order, deduplicated. See [`catalog`](cli/catalog.md).

---

## Where each key lands

Configuration compiles into the pipeline engine's `PipelineConfig`. If you are
reading the code, `ProjectConfig.to_pipeline()` in
`src/legolization/configuration.py` is the complete mapping, and
`mapping_hash(self.to_dict())` there is the configuration hash that feeds bundle
identity, manifests, and template-cache keys.
