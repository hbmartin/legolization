# legolization

Turn a 3D model into a **physically buildable LEGO-style model** in LDraw
format, with step-by-step build instructions and a bill of materials.

This is the classic "LEGO construction problem" from the research literature
(see `references/`): voxelize → hollow → place bricks → check structural
stability → repair → export. The stability check is a full
**Rigid-Block-Equilibrium (RBE)** model (StableLego formulation, cross-validated
against its released test fixtures): per-brick force *and* torque balance with
knob-friction capacities, solved as a provably exact linear program on an open
solver stack — no Gurobi required.

> **Legolization is an independent open-source project. It is not affiliated
> with, sponsored by, or endorsed by the LEGO Group. LEGO® is a trademark of
> the LEGO Group, which does not authorize or endorse this project.**
>
> The skill icons in this repository are original artwork and are licensed
> under the repository license; they do not use LEGO trademarks.

## Install the skills

The conversational way to use Legolization is the ten-skill suite. Install
the complete collection into your coding agent with:

```sh
npx skills add hbmartin/legolization --all
```

The suite is Codex-first but works with every agent supported by
`npx skills`. Each skill requires `legolization>=0.6.0`; when the CLI is
missing or too old, the skill installs or upgrades the latest stable release
automatically through `uvx.sh` and, if that installation fails, reports the
failure and stops the requested operation.

## Install the CLI

The CLI is a normal Python package (PyPI publication of 0.6.0 is pending
release):

```sh
uv tool install legolization   # persistent install via uv
uvx legolization --help        # ephemeral run via uv
pip install legolization       # classic pip
```

or use the `uvx.sh` one-liner (the same path the skills use):

```sh
curl -LsSf https://uvx.sh/legolization/install.sh | sh
```

For a development checkout, see [Development](#development).

## Skill catalog

| Skill | Purpose | Say something like |
| --- | --- | --- |
| `legolize-model` | Turn a mesh or voxel model into a stable LEGO-style model and complete bundle. | "Turn dragon.stl into a LEGO model I can actually build." |
| `prepare-lego-input` | Inspect, orient, color, and normalize a source model before generation. | "Is spot.obj oriented right, and how big should the brick version be?" |
| `optimize-lego-build` | Compare, retile, or improve an existing LDraw assembly. | "Can you rebuild castle.ldr with fewer bricks without making it fragile?" |
| `publish-lego-instructions` | Produce a step-annotated MPD and, when rendering is available, HTML/PDF instructions. | "Make a printable instruction booklet for my mushroom model." |
| `analyze-lego-assembly` | Diagnose stability, load paths, insertion concerns, and assembly risks. | "Why does my spaceship model feel wobbly at the wings?" |
| `repair-lego-assembly` | Propose and validate a repair or redesign without overwriting the source. | "This model falls apart at the arch — fix it without changing how it looks." |
| `extend-lego-part-support` | Research, estimate, validate, and activate parts-catalog extensions. | "Add support for part 4070 so my headlight-brick model analyzes correctly." |
| `render-ldraw` | Render a model or bundle from requested views. | "Show me what heart.ldr looks like from the front and the top." |
| `inspect-instructions` | Audit buildability, ordering, insertion pressure, and booklet readiness. | "Do these build steps make sense, or will something fall off halfway through?" |
| `eval-corpus` | Run repeatable strategy evaluation across the available corpus. | "Did my placement change make the whole corpus better or worse?" |

There is deliberately no routing skill: `legolize-model` owns complete
new-model creation and `optimize-lego-build` owns improvement of an existing
assembly.

## What it does

- **Input**: MagicaVoxel `.vox`, NumPy `.npy`, and `.obj`/`.stl`/`.ply` mesh
  files, plus existing `.ldr`/`.mpd` assemblies for analysis, optimization,
  and publication. Missing VOX RGBA data uses the MagicaVoxel default
  palette. PACK and scene-graph transforms are composed; conflicting overlaps
  and ambiguous unplaced multi-model files are rejected.
- **Placement**: covers every voxel with bricks and plates at true heights
  (plate = 8 LDU, brick = 24 LDU). `build` defaults to `auto`:
  bounded whole-model exact placement is used below its cell and candidate
  caps and deterministic `bond` placement otherwise. Exact placement enforces
  target coverage, LDU collision, colour, and rooted stud connectivity, then
  cold-certifies physics. Shape-preserving slopes (including inverted slopes)
  and exposed-region tile splitting are default finishing passes; neither may
  change target occupancy. Plate caps are opt-in because they change layering.
  The available heuristic strategies remain:
  - `greedy` (default): largest-first bottom-up fill with Kollsker's
    remainder-lookahead h(r) and distance-decayed stretcher-bond scoring,
    then delete-and-rebuild reinforcement around the weakest bricks.
  - `luo`: Luo et al. (2015) maximal random merge with split-and-remerge
    refinement, accepted by Luo's maximin friction capacity C_M; supports
    soft colour constraints (`--set placement.colour_mode=soft`).
  - `bond`: Kollsker & Malaguti's constructive brick-bonding heuristic —
    remainder lookahead + staggering reward + per-layer repair.
  - `fast`: Bao et al.'s greedy per-layer merge with a dominant big-brick
    weight, perpendicularity term, and connectivity retries.
  - `smga`: Lee et al.'s split-and-merge genetic algorithm per layer
    (`--set placement.ga_generations=N`, `--set placement.time_budget_s=S`).
  - `beauty`: Min et al.'s objective-driven tiling with symmetry/balance,
    stability-priority, and big-brick terms (`--set placement.beauty_preset=
    {balanced,stability,aesthetics,efficiency}`).
  - `kollsker`: Kollsker & Malaguti's exact set-partitioning MILP, solved
    per 4-connected component of each layer — stage 1 minimizes the part
    count, stage 2 maximizes stagger quality at that optimum; falls back
    to `bond` per component on timeout.
  - `global-exact`: bounded 3D exact placement with a brick-count or mass
    objective and explicit `fail`, `fallback`, and deadline-bound `continue`
    limit policies.
- **Physics**: every layout is scored by the RBE — gravity, support, press,
  drag/pull friction (capacity T = 0.98 N per contact point), knob presses,
  and torque-capable side presses at shared-face extremes (side-supported
  structures shed load like Luo's bridges). Equilibrium residuals sit *in the
  objective*, so even collapsing structures solve and failures localize to
  specific bricks.
- **Repair**: unstable layouts go through an ALNS destroy-and-repair pass
  (Kollsker's artificial-link QP pinpoints the deficit; the freed region is
  refilled by the merge engine or an exact-cover MILP) before any material is
  added back by the stability-aware hollow-restore loop.
- **Auto-hollow**: interiors are hollowed to a shell (~1 brick thick, tunable
  with `--set geometry.shell_plates=N`); interior cells are colour-free so
  merges never fragment on invisible boundaries.
- **Instructions**: smart step sequencing (default) chunks each layer into
  spatially coherent steps sized to the model, keeps mirror-symmetric halves
  together, prefers spatially adjacent steps (Ma et al.'s continuity
  heuristic), validates every expanded action for collision, insertion,
  connectivity, support, and cold prefix stability, and adds `0 ROTSTEP`
  view hints. When the greedy pass hits an unstable stretch it re-plans the
  remainder by assembly-by-disassembly along a maximal-stability path
  (Tian et al. / Luo); an opt-in beam search
  (`InstructionsConfig(search="beam")`) explores whole build orders. The
  bundle's `bom/bom.json` records the bill of materials with per-step
  callouts.
- **SNOT cladding**: the opt-in SNOT pass clads tall flat wall faces with
  sideways tiles on exact connector geometry, including 87087 carriers, 4070
  headlight bricks, and 99781 inverted brackets — real receiving
  geometry, priced by the same RBE physics through genuine lateral stud
  contacts. Only free-standing 1x1 wall columns are converted (carving a
  bracket out of a wall-spanning brick would destroy its bonding), and
  the pass reverts wholesale if it would flip the stability verdict.
- **Subassemblies**: instruction generation always detects stretches that
  float in every build order (mushroom caps, arches), lifts them out as
  separately built units — each constructed stably on the table, then
  attached as one piece — and emits them as `.mpd` submodel FILE sections.
  Booklets get per-unit sections and attach callouts; the `.ldr` fallback
  flattens attach steps back to world-frame bricks.
- **Booklets**: a completed bundle's `instructions/` directory carries
  `instructions.html` and `instructions.pdf` — cover page with model stats,
  parts list, and one rendered image per step with new bricks highlighted and
  per-step part callouts. Step images render through LeoCAD (preferred;
  batched per-step export) or LDView, auto-detected from
  `$LEGOLIZATION_RENDERER`, PATH, then `/Applications`; the parts library
  comes from the managed store (`legolization parts sync`) or `$LDRAWDIR`.
  Rendering policy is explicit: under the default `--render auto`, no
  available renderer means the HTML/PDF booklet is **omitted entirely** — no
  placeholder pages — and the omission is recorded; `--render required`
  turns missing or failed rendering into a partial result (exit 3);
  `--render off` never renders. If only some steps render, the booklet keeps
  explicit missing-step markers and the bundle exits 3.
- **Output**: a valid `.ldr` or `.mpd` written through
  [pyldraw3](https://pypi.org/project/pyldraw3/). Open it in
  [LDView](https://tcobbs.github.io/ldview/) or
  [BrickLink Studio](https://www.bricklink.com/v3/studio/download.page).
- **LDraw analysis**: `analyze` preserves exact arbitrary occurrence matrices
  and reports geometry, contact, physics, and planning capabilities separately.
  Supported pitch/roll poses are analyzed without snapping; unsupported
  capabilities retain completed evidence and produce a partial manifest.

## Usage

`legolization bundle` is the complete pipeline: it accepts every supported
input and writes a portable bundle directory (see
[Runtime behavior](#runtime-behavior)) containing the winning model, BOM,
instructions when rendered, comparison report, and diagnostics.

```sh
# Full pipeline into a portable sibling bundle (balanced quality by default)
legolization bundle data/examples/heart.vox
legolization bundle model.obj --quality fast
legolization bundle model.npy --quality exhaustive --duration 3600
legolization bundle model.vox --config legolization.toml --render required

# Inspect and normalize a source model before generating
legolization input inspect model.obj
legolization input inspect model.obj --write --up y --target-studs 24

# Preserve-or-improve an existing LDraw assembly
legolization bundle assembly.mpd                 # preserved by default
legolization bundle castle.ldr --retile          # regenerate from occupancy

# Retry an unbuildable result (exit 2) down the material ladder:
# four-plate shell -> six-plate shell -> solid, sharing the budget fairly
legolization bundle model.obj --retry-materials --duration 1800

# Analysis and repair
legolization analyze model.ldr
legolization analyze assembly.mpd --repair --effort fast
legolization analyze model.ldr --artifact-dir diagnostics

# Rendering and instruction auditing
legolization model render heart-legolization
legolization model render model.mpd --views front,iso --size 1024
legolization instructions audit heart-legolization --render-dir steps/

# Parts-catalog extensions
legolization catalog infer 4070
legolization catalog validate 4070-legolization-support
legolization bundle model.vox --catalog 4070-legolization-support

# Corpus evaluation
legolization corpus evaluate --kind synthetic

# Low-level single-strategy build and supporting commands
legolization build data/examples/heart.vox -o heart.ldr
legolization build model.obj -o model.ldr --target-studs 24
legolization build model.obj -o model.ldr --auto-scale 16 32
legolization validate model.manifest.json --against model.ldr
legolization cache inspect
legolization cache clear --key SHA256
legolization parts sync
```

`--quality` selects the candidate policy: `fast` (greedy, 2 minutes),
`balanced` (full strategy sweep plus eligible global exact, 15 minutes; the
default), `exhaustive` (seeds 0–2; requires `--duration`), and `direct`
(single configured strategy). Candidates are gated on buildability (stable,
one connected component, nothing floating) and the survivors are ranked by
the weighted objective plus colour error; the full comparison lands in the
bundle's `comparison/report.json` and only the winner model is published.
When no candidate is buildable, diagnostics retain the best rejected model
with explicit reasons.

Builds write `<output>.manifest.json`; analyses write
`<input-stem>.manifest.json`. Use `--manifest PATH` or `--no-manifest` to
override that per run. Exit codes are listed under
[Runtime behavior](#runtime-behavior).

Configuration is strict nested TOML. Built-in defaults are applied first,
then the file, then only command-line options explicitly supplied by the user
(`--config PATH` plus repeatable dotted `--set KEY=VALUE` overrides).
Relative paths are resolved from the TOML file; unknown keys and incompatible
options fail before work starts.

```toml
[placement]
strategy = "auto"
objective = "bricks"
restarts = 1

[placement.exact]
max_cells = 256
max_candidates = 100000
time_limit_s = 60
limit_policy = "fail"

[stability]
profile = "corrected"
support = "baseplate"

[output]
manifest = true
emit_support = false
```

### Mesh inputs

Mesh inputs (`.obj`/`.stl`/`.ply`) are voxelized directly at plate
resolution (always aspect-correct): `--target-studs N` sets the footprint
width and `--auto-scale MIN MAX` searches a stud range. Orientation is
classified automatically; `legolization input inspect` reports the detected
up-axis (override with `--up`), a recommended stud size, and sampled mesh
colours before you generate, and `--write` emits a normalized `.npy` target
plus JSON sidecar into a `-prepared` sibling bundle. Embedded texture/vertex
colours are sampled automatically (nearest-vertex, quantized to the LDraw
palette); a mesh with no colour data — e.g. a loose `.obj` without its
`.mtl`/texture — needs a uniform colour choice. Disconnected mesh components
are preserved by default. Finer mesh options are configuration keys under
`input.mesh.*`, settable via `--config` or `--set`.

Heuristic multi-seed restarts on `build` are opt-in (`--restarts`; default
1). Seeds are deterministic, exact placement is never raced, and the winner
is ordered by buildability, components/floating parts, worst stability
score, the configured cost objective, and a canonical layout signature.

```text
restart race: seeds 0..2 -> seed 2
wrote heart.ldr
  bricks: 12   mass: 17.9 g   steps: 8   slopes: 0   tiles: 0
  stability: STABLE (worst score 0.001, min capacity 0.979 N)
```

Exit code 0 means the model is stable, one stud-connected component, and
ground-connected. Exit 2 means it is not buildable as-is (try
`--retry-materials`, another `--quality`, or a different `--seed`) — note
that an input made of several disconnected voxel islands is reported as
multiple components even when every island stands on the ground.

### Catalog extensions

`legolization catalog infer PART_ID [--key] [-o DIR] [--offline]` researches
authoritative online sources for an unsupported part (offline mode skips the
lookups), captures provenance, and drafts geometry and physical estimates
into a `-legolization-support` sibling bundle
(`catalog-extension.json`, `draft-estimates.json`, `sources.json`,
`validation.json`, `geometry/`). `legolization catalog validate PATH` runs
the import, round-trip, collision, connector, and topology validation gate;
only a validated support bundle activates, via `--catalog <dir>` on
`build`, `bundle`, and `analyze`. Repeatable `--catalog-estimates PATH`
sidecars carry labeled-provenance physical estimates. Changes to the
built-in upstream catalog always require confirmation.

### Analyze an existing LDraw model

`analyze` is the non-generative feasibility workflow. It always builds a
geometry-first assembly from pyldraw's resolved occurrences, so arbitrary
parts, MPD transforms, SNOT/half-stud placements, angled mechanisms, and every
LDraw colour can produce topology even when the legacy voxel `Layout` adapter
rejects them. Confirmed and potential connector graphs remain separate, and an
unsupported connector produces partial evidence instead of aborting the model.

```sh
legolization analyze model.ldr
legolization analyze assembly.mpd --topology-only --no-repair
legolization analyze vehicle.mpd --support wheels --scenario side-load
legolization analyze model.mpd \
  --path-between pages:1-20 pages:80-100 --artifact-dir diagnostics
legolization analyze model.ldr \
  --report evidence.json --output repaired.ldr --time-budget 120 --seed 7
legolization analyze model.ldr \
  --connector-catalog connectors.json --ldcad-metadata shadow-library/
```

Connection evidence comes from pyldraw3's typed connection subsystem.
`--ldcad-metadata` registers an LDCad shadow library (a directory or a
ZIP/CSL archive) and `--studio-metadata` a Studio connectivity JSON export as
connection-feature sources on the parts catalog; unreadable sources exit with
an error before analysis starts. `--connector-catalog` (schema-1 JSON,
validated against the packaged `connector-catalog-v1` schema) remains the
only source for mass, centre of mass, inertia, collision proxies, region
tags, force capacities, and custom connector kinds — Studio `mass_g`/`tags`
fields are no longer read.

Normal runs write the canonical version-1 assembly manifest. The old schema-2
analysis and schema-1 assembly JSON are available only as explicitly requested
derived views through `--report` and `--assembly-report`. Graph, component MPD,
and floating MPD artifacts remain available; `--no-data-artifacts` suppresses
them. `--artifact-dir` additionally enables a
self-contained HTML report, missing-connector callouts, and before/after
renders when a renderer is installed. Only one JSON report may target stdout.

Support defaults are adaptive: strict voxel-compatible models use an anchored
baseplate, detected vehicles rest on their wheels, and other arbitrary models
use loose lowest-surface contacts. `--support free` deliberately reports no
static verdict. Unknown load-bearing capacities keep physics indeterminate
even when an optimistic equilibrium exists. Topology-only recommendations on
an indeterminate result are explicitly marked unverified. Exit codes are
assembly-driven: `0` means connected/feasible, `2` means definitely
disconnected/infeasible, `3` means partial/indeterminate, and `1` means
invalid input or runtime error.

On a definite failure, the repair search tries one BOM-preserving source
edit at a time (orthonormal rotations/reflections and nearby stud/plate
translations). A suggestion must improve real connector topology, never just
bounding-box overlap. `--effort` sets the tier — `fast` (60 s), `balanced`
(300 s; the default), or `exhaustive` (requires an explicit
`--time-budget`) — and repair artifacts land in a `-repair` sibling bundle
(`--repair-output DIR` overrides). Repair never overwrites the source input:
the best validated edit is written to `model.repaired.ldr` or `.mpd`, a
failed repair explains why and retains its best rejected candidate, and
`--no-repair` disables the search.

The preserved legacy report uses schema 2. Its `ldraw` block records pyldraw's prepared
catalog state, tolerant-load diagnostics, exact transformed bounds, official
BOM, occurrence provenance, and renderer-neutral instruction sections. Exact
stud contacts and AABB gaps are included through 1,000 occurrences and marked
as skipped above that safety limit. The analyze command prepares the configured
catalog automatically; if the library is missing, run
`legolization parts sync`.

The assembly schema records exact occurrence transforms and provenance,
connector coverage, confirmed/optimistic component counts, detected grid
frames, resolved support, region-to-region cuts, load scenarios, and ranked
counterfactual evidence. Public occurrence IDs are one-based; pyldraw's
zero-based traversal index is retained separately.

LDraw analysis preserves the imported matrix and position exactly. Catalog and
assembly capabilities decide whether collision, connector, and physics
operations support that pose; no meaningful pitch, roll, or half offset is
silently snapped to the generated-placement lattice.

Legacy voxel catalog extensions declare `"schema": 2` and a `parts` list.
Rectangular
bricks, plates, and tiles use explicit `size`, `height_plates`, and measured
`mass_g`. A custom non-rectangular part must instead declare its complete
`occupied_cells`, `filled_cells`, `top_connectors`, `bottom_connectors`,
`orientations`, `origin_offset`, `height_plates`, and measured `mass_g`.
Extensions cannot override keys or introduce an ambiguous LDraw decode.

Python API:

```python
from pathlib import Path
from legolization import (
    AnalysisConfig,
    AssemblyAnalysisConfig,
    PipelineConfig,
    VoxelGrid,
    analyze_assembly,
    analyze_ldraw,
    run,
    run_file,
)

result = run_file(Path("model.vox"), Path("model.ldr"), PipelineConfig(seed=1))
print(result.buildable, result.step_count, result.stability.max_score)

analysis = analyze_ldraw(
    Path("existing.mpd"),
    AnalysisConfig(repair_time_budget_s=120, seed=7),
)
print(analysis.report.verdict, analysis.report.to_json())

assembly = analyze_assembly(
    Path("vehicle.mpd"),
    AssemblyAnalysisConfig(support="wheels", scenarios=("auto",)),
)
print(assembly.report.topology_verdict, assembly.report.physics_verdict)
```

## Runtime behavior

**Portable bundle directories.** Every operation writes its results into a
portable bundle directory, by default a sibling of the input named for the
operation:

| Operation | Default sibling directory |
| --- | --- |
| `bundle` (full generation) | `<name>-legolization` |
| `input inspect --write` | `<name>-prepared` |
| `bundle --retile` (assembly optimization) | `<name>-optimized` |
| instruction publication | `<name>-instructions` |
| `analyze` diagnostics | `<name>-analysis` |
| repair search | `<name>-repair` |
| `catalog infer` / `catalog validate` | `<name>-legolization-support` |

If a target directory already exists for different work, a numeric suffix is
appended rather than overwriting it. `bundle.json` is the authoritative
record of each bundle: source identity, effective configuration,
software/library/catalog versions and hashes, artifacts, stage status,
warnings, and verdicts — updated atomically after each stage, and never
containing an absolute source path. Downstream skills and commands accept a
bundle directory and locate its primary model through `bundle.json`.

**Resume by default.** Bundle identity is the input content hash, effective
configuration hash, Legolization version, and parts-catalog hash — never
timestamps or output paths. Identity-matched complete or incomplete work is
resumed by default; `--fresh` opts out and forces a fresh numeric-sibling
run. Artifact drift is regenerated in place.

**Detached soft-deadline workers.** Candidate placement runs in detached,
identity-stamped worker processes capped at the logical CPU count. At a soft
deadline the bundle publishes the best completed buildable candidate as a
provisional partial result (exit 3) while late workers keep running; a later
identity-matched resume may adopt a better late result.
`bundle --cancel-pending` terminates only this bundle's identity-matched
detached workers, records the cancellation atomically, and keeps completed
artifacts and diagnostics for a later resume.

**One JSON envelope.** Every command accepts `--json`: stdout then contains
exactly one result envelope (`schema: "legolization.result/v1"` with
`version`, `command`, `status`, `exit_code`, `artifacts`, `warnings`, and an
`error` object on failures) while progress and warnings go to stderr. Errors
under `--json` still emit the envelope on stdout.

| Exit code | Meaning |
| ---: | --- |
| 0 | Complete. |
| 1 | Operational error. |
| 2 | Unbuildable or failed physics. |
| 3 | Partial or indeterminate outcome, including incomplete instruction ordering, partial rendering, and provisional timed results. |
| 4 | Exact-solver limit under the `fail` policy. |
| 130 | Interrupted after atomically recording resumable partial state. |

## Platform setup

| Setup | Behavior |
| --- | --- |
| Managed LDraw parts library | `legolization parts sync` installs or updates the official library into platform user-data storage without admin privileges, validates the download, checks weekly for updates (`--force` re-downloads early), and continues silently offline while a valid existing library remains usable. Paired shell/PowerShell scripts ship with the `legolize-model` skill under `skills/`. |
| Renderer | Ask-first installation: LDView via Homebrew on macOS, LeoCAD via `winget` on Windows, LeoCAD plus Xvfb via `apt` on Ubuntu. Paired shell/PowerShell scripts ship with the `render-ldraw` skill under `skills/`. Existing renders are retained when the renderer or library changes. |

## How the stability model works

Each mated stud contributes 3 or 4 contact points (per StableLego's measured
geometry) carrying a shared normal force and a friction (drag/pull) force, so
Newton's third law holds by construction; each knob adds four horizontal
knob-press forces, and laterally touching bricks exchange side presses at
exact shared-face extremes so lateral load transfer carries torque. The
corrected default enforces all three force and all three torque balance rows,
rotates contact patterns with part yaw, and uses the paper contact-point rule.
A named `stablelego-parity` profile retains reproduction behavior. A brick
scores `1` when it
cannot reach equilibrium or its friction demand exceeds T; otherwise
`drag_max / T` — so the score doubles as a stress heatmap. The default solver
is a hand-assembled LP on scipy/HiGHS, and the relaxation is provably exact
(each contact's press and pull columns are exact negatives, so no optimum ever
uses both). Complementarity MILP is not a production CLI mode; a private
small-instance oracle remains for equivalence tests.

## Benchmark

Existing evaluation and comparison utilities are preserved for regression and
research work (`legolization corpus ...`; see
`docs/self-evaluation-playbook.md`). Expanding their corpora, metrics, or
baselines is intentionally deferred; they are not acceptance gates for the
foundation program.

## Development

```sh
uv sync
uv run ldraw download --yes   # once: fetch the LDraw parts library
uv run ldraw generate --yes   # once: generate ldraw.library.* part/colour modules
```

```sh
uv run pytest          # fast inner loop; slow integrations skip by default
uv run pytest --run-slow  # full suite, including benchmark/sweep/renderer tests
uv run ruff format --check . && uv run ruff check .
uv run ty check src tests
uv run pyrefly check src tests
uv run deptry .
uv run lizard --languages python --CCN 15 --length 120 --arguments 8 src/legolization
```

## License

GPL-3.0-or-later (inherited from pyldraw3).
