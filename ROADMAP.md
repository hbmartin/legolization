# Roadmap

This document is the current source of truth for legolization's pre-release
engineering program: verified current state, active work, and the open
engineering backlog. The dated progress journal is preserved in
[`docs/history/roadmap-history.md`](docs/history/roadmap-history.md);
historical measurements there are evidence, not descriptions of current
defaults. User- and contributor-facing documentation lives in
[`docs/`](docs/index.md) and is published at
<https://hbmartin.github.io/legolization/>; this file wins any disagreement
about what is currently being worked on.

## Verified current state

- The primary interface is a local, deterministic command-line application:
  `build`, `analyze`, `validate`, and `cache inspect|clear`. Domain functions
  are called in-process; there is no network service or daemon.
- New command paths use strict nested TOML configuration. Precedence is
  built-in defaults, then TOML, then explicit command-line overrides. Unknown
  keys, invalid path kinds, incompatible modes, and non-finite values fail
  validation.
- Successful builds and analyses emit a canonical
  `legolization.assembly-manifest` version 1 sidecar by default. Canonical JSON
  contains no wall-clock timestamp and includes hashes, algorithms, exact LDU
  poses, normalized contacts, capability results, stability evidence, action
  relations, instructions, BOM data, artifacts, and cache provenance.
- Generated placement is yaw-only. Imported LDraw matrices are preserved
  exactly and analyzed capability by capability; unsupported operations are
  reported as partial, never silently snapped or described as exact.
- Geometry uses integer LDraw units for transforms, connector locations, and
  collision volumes. Stud/plate cells remain a separate coarse target-coverage
  representation. Exact collision checks follow coarse spatial bucketing.
- Placement defaults to `auto`: bounded global exact placement is selected
  when preflight is within the cell and candidate-row caps; deterministic
  `bond` placement is selected otherwise. Explicit exact placement defaults to
  failing at a limit, with explicit `fallback` and deadline-bound `continue`
  policies available.
- Heuristic restarts are deterministic and opt-in (`restarts = 1` by default).
  Exact placement is not raced across seeds.
- The corrected six-degree-of-freedom rigid-body-equilibrium profile is the
  default. It enables yaw torque, yaw-rotated contacts, the paper contact-point
  rule, and virtual-baseplate support. `stablelego-parity` remains an explicit
  reproduction profile. Loose-table analysis and optional emitted physical
  support plates are supported.
- Production physics uses the linear RBE solver. The former complementarity
  placement/configuration mode is obsolete; only a private small-instance
  reference oracle remains for equivalence tests.
- Incremental rejection screening uses exact changed bounds, contact-graph
  rings, and frozen interface forces. Every accepted modified layout and every
  emitted instruction sequence is certified by a full cold solve.
- Contact evidence is normalized into physical connectors while preserving all
  raw parser evidence. Assembly planning uses typed connector, support,
  six-direction blocker, order, subassembly, and template relations and records
  the earliest failing action when certification fails.
- Shape-preserving slope and tile finishing is on by default. It preserves
  target occupancy, includes inverted slopes, and uses mesh normals when
  choosing orientation. Plate caps are opt-in because they change layering and
  part count. Headlight bricks and inverted brackets provide exact compound
  side-detail templates with half-stud/half-plate geometry.
- MagicaVoxel input uses the format's default palette when RGBA is absent.
  Multi-model scenes are composed through PACK and scene transforms; ambiguous
  unplaced models and conflicting overlaps are errors.

## Completed foundation work

- Canonical nested configuration, typed errors/results, typed progress events,
  and a shared monotonic deadline.
- Canonical deterministic manifests and manifest-derived BOM/action/stability
  views, including build, analysis, and validation command paths.
- Exact LDU catalog schema, collision geometry, physical connectors, half-unit
  offsets, arbitrary imported-matrix preservation, and exact yaw invariance.
- Bounded whole-model exact cover with hard target coverage, color, collision,
  rooted connectivity, brick-count or mass objectives, deterministic tie
  breaking, physics certification, and weakest-region no-good cuts.
- Deterministic grid-phase selection, opt-in automatic scale selection, mesh
  feature annotations, normal-aware finishing, inverted slopes, tile finishing,
  and opt-in plate caps without source deformation.
- Repeated-component signatures under translation and yaw with color remapping,
  reusable instruction submodels, and a content-addressed platform cache with
  locking, atomic writes, corruption quarantine, deterministic inspection, and
  scoped clearing.
- Corrected physical-contact normalization, full directional blocker analysis,
  hierarchical assembly verification, causal prefix-failure traces, frozen-
  boundary screening, and final cold certification.
- Strict dependency declarations for the editable LDraw sibling projects.

## Active work

The remaining pre-release work is maintenance rather than a new product area:

- Keep the manifest schema and exact catalog migrations explicit and versioned
  as supported parts and imported poses expand.
- Continue decomposing algorithm implementations when they approach the
  enforced complexity limits; preserve typed state/context boundaries rather
  than growing orchestration functions.
- Expand small deterministic property and structural golden fixtures when a
  geometry, instruction, or cache regression is found. Optional LDView visual
  snapshots remain developer infrastructure, not a required renderer feature.
- Finish removing legacy report duplication once all internal evaluation
  consumers read manifest-derived views. Existing evaluation and BOM behavior
  remains available during that mechanical migration.
- Take the pending mesh-kind baseline cut when an idle machine is available.
  This is an existing planned baseline, not corpus expansion; the measured
  history, the release decision, and the exact commands are in
  [`docs/reports/mesh-baseline-pending.md`](docs/reports/mesh-baseline-pending.md).

## Engineering backlog

Open engineering items, carried forward from the v3–v8 program backlogs.
Items those programs closed have been removed rather than struck through;
the dated entries in
[`docs/history/roadmap-history.md`](docs/history/roadmap-history.md) hold the
completion evidence. Items ruled out above under "Deferred by product
decision" are not repeated here.

### Performance

- **Incremental re-analysis** — the stability-perf workstream. Refinement
  changes a k-ring but re-solves the whole structure at ~n^2.8; v8 measured
  that no solver-level swap helps (direct highspy identical, IPM ≤1.4x with
  1e-9 drift). Design directions, in order: warm append-only scoring through
  `PrefixSolver` for placement-time verdicts; frozen-boundary ring analysis
  for ALNS/Luo accept/reject with a full exact solve on acceptance. Both must
  clear the golden, scorecard, and dual-engine gates. Until then the v8 budget
  guardrail (`time_budget_s` → one pipeline deadline honoured by repair,
  hollow-restore, and Luo stabilize) bounds the tails.

  *Current mechanism (2026-08-07/08):* BrickSim's reduced-variable
  parameterization (arXiv:2603.16853; `references/bricksim-*/paper.md`) is
  ported as an opt-in candidate screen — `stability/reduced.py` (affine
  per-interface fields, a restriction of the exact LP's variable space, so
  errors skew conservative; the screen's soft equilibrium term makes that a
  strong tendency rather than a theorem, and measured undershoots do occur —
  safety comes from cold-certifying every accepted candidate, not from the
  error direction) + `stability/screen.py` (single OSQP QP mirroring the
  certifier objective), behind `SolverConfig.screen = "bricksim"` (default
  `"off"`, every historical byte preserved). Wired into
  `FrozenBoundaryAnalyzer.certify`, Luo `_stabilize`, the
  `redesign._validate_candidate` pre-gate (`failed_gate="screen"`), screened
  `final_remerge`, and the `_snot_tiers` revert pre-empt. Lateral/SNOT mates
  are supported (field plane rotated onto the mating plane), with
  rank-rejection scoped to vertical-only layouts and binary
  confident-unstable rejection everywhere. Hull-vertex constraint masking
  shipped (16.6% shell-series win vs its ≥10% gate); `screen_fields=
  "bricksim"` is a working research basis.

  Sites measured NOT worth screening: hollow-restore (unconditional accept,
  no test to skip), `_add_support` (no candidates), redesign's envelope
  baselines (need `interface_forces`), global-exact stability cuts (MILP
  dominates, needs `weakest_pair`), and
  `_canonicalize_templates`/`_guarded_finish` (one binary-gated solve each).
  Measurement gate: `scripts/benchmark_screen.py` against the pre-registered
  thresholds in
  [`docs/guides/performance-testing.md`](docs/guides/performance-testing.md).

- **Candidate caching in greedy `_fill`.** `_placements` recomputes rotations
  and validity per seed; memoize per (part, yaw) footprints and test cells
  against numpy masks instead of Python sets.

- **Multi-seed restarts.** Layout quality varies noticeably across seeds on
  shell shapes (the r=4 hollow sphere spans ~135–205 parts over seeds), and
  restarts are opt-in at `restarts = 1`. `--strategy all` (`compare.run_all`)
  already fans out over a spawn process pool across strategies at one seed;
  a multi-seed sweep that harvests the good tail is a small extension of the
  same runner, and all state is copyable via `Layout.copy`.

### Placement quality

- **Validate the beauty scalar against human judgement.** Min-style
  symmetry/balance and SM-GA/Bao perpendicularity are live objective terms
  (`placement/aesthetics.py`) and the beauty strategy optimizes them
  directly, but the scalar has never been checked against human preference
  (the permutation-drift methodology).

  *Unblocking data (2026-08-08):* the missing ingredient was a corpus of
  human-authored assemblies to score against. Three now exist — LDraw OMR
  (official sets, designed by LEGO), MobileBrick's curated brick models, and
  StableText2Brick as the algorithmic contrast class. See "External validation
  datasets" below; `scripts/aesthetics_baseline.py` is the consumer.

### External validation datasets

Externally sourced, independently labelled data used to check the existing
implementation. Surveyed in
[`docs/reports/dataset-survey.md`](docs/reports/dataset-survey.md); acquisition
is `scripts/fetch_datasets.py` (pinned sha256, https-only, atomic) and
`scripts/fetch_omr.py` (a polite crawl). Nothing here is a runtime dependency
and nothing is vendored except small attribution-carrying reference tables.

- **BrickNet** — <https://github.com/kulits/BrickNet>, **MIT**, Kulits & Schmid,
  CVPR 2026 (arXiv:2604.22984). Adopted as a **dev-group dependency plus
  vendored data tables** (`references/bricknet-data/`: part vocabulary,
  per-part connector labels with exact part-local LDU rows, the part-alias
  canonicalization table, and the extended colour table). Current use is the
  independent cross-check in `scripts/ldraw_coverage.py`. Future iterations, in
  rough value order:
  1. `bricknet.parse_ldr` as a second opinion on `ldraw_in.py` — where two
     independent parsers disagree on part count or connectivity, one is wrong;
  2. `labels.json.xz` connector rows to validate `assembly_connections.py`,
     whose connection graph is presently only ever checked against itself;
  3. the per-part collision meshes (`inset.tar.xz`, 369 MB, watertight PLYs
     inset 0.25 LDU) as an independent check on catalog collision geometry;
  4. `part_aliases.json.xz` feeding `catalog_infer/sources.py`, whose part-id
     canonicalization is currently one regex stripping a trailing revision
     letter.

  **The gated dataset is deliberately declined.** The 253,623 / 67,185 / 512
  pretraining, SFT, and validation graphs sit behind a request form with
  restrictive terms; we use only the MIT package and its bundled tables, both
  of which are unconditionally redistributable. The two large archives
  (`inset.tar.xz`, `ldraw.tar.xz`) are direct downloads and are *not* gated.

- **MobileBrick** — <https://github.com/ActiveVisionLab/MobileBrick>, **MIT**
  (the CC BY-NC-ND badge on the arXiv page covers the paper, not the dataset).
  153 sequences: 135 random brick shapes plus **18 manually curated LEGO models**,
  each with a `mesh/gt_mesh.ply` ground-truth shape derived from known brick
  geometry. Two uses:
  1. aesthetics calibration against assemblies a human actually built;
  2. a genuine **mesh to brick round-trip** — legolize `gt_mesh.ply` and compare
     against the real model it was built from, the only answer key of its kind
     we have.

  Acquisition policy: the distribution zip is **13.1 GB and ~99% RGBD imagery**.
  Extract selectively (`unzip -j … '*/mesh/*.ply'`, ~300 MB); never unpack the
  whole archive.

- **StableText2Brick** (MIT, 47,389 rows, 44 MB parquet) — a positive-only
  stability regression and the set BrickSim's headline 150-assembly number is
  sampled from, so our figure is directly comparable. **StableLego** (MIT repo)
  supplies the mixed valid/invalid negative class the existing
  `scripts/stablelego_sweep.py` already consumes. **ShapeStacks** is the only
  independent check on the toppling/CoM branch, which `topple-arm` pins with a
  single fixture. **Thingi10K** stratifies mesh pathologies that `mesh.py`'s
  fallback paths currently meet one fixture at a time.

## Research traceability

The implementation deliberately borrows mechanisms rather than claiming paper
parity:

| Source | Applied foundation | Deliberate boundary |
| --- | --- | --- |
| *Legolization* and silhouette-fitted voxelization | Deterministic voxel phases, surface/silhouette error, exact coverage, and scale selection | The source mesh is not deformed to improve brick fit. |
| *Automatic Generation of Vivid LEGO Architectural Sculptures* | Normal-aware architectural slopes, exposed detail regions, and repeated structural motifs | Only catalog-declared, occupancy-preserving templates are generated. |
| *StableLego* | Rigid-body equilibrium, contact forces, weakest contacts, and a named reproduction profile | The corrected production profile intentionally includes additional torque/contact corrections. |
| Force-based reinforcement work | Weakest-region localization and repair feedback | Every repair is rechecked by a cold solve rather than accepted from a proxy score. |
| Assembly-sequence graph work | Typed contact/blocker action graph, hierarchical subassemblies, and prefix checks | Planning remains deterministic and rule/optimization based; no learned planner is introduced. |
| Kollsker, Luo, split-and-merge, and streamlining work | Existing deterministic placement/refinement strategies and exact subproblems | Whole-model exact placement is separately bounded and explicitly reports limits/fallbacks. |
| Segmentation and component methods | Independent component analysis/search and reusable component templates | The paper's weak force heuristic is not copied; certification uses the RBE solver. |

Converted paper text is retained under [`references/`](references/) and the
source PDFs under `papers PDFs/`. When a paper and the implementation differ,
tests and the explicit configuration/profile name define package behavior.

## Deferred by product decision

These items are intentionally outside the current implementation program:

- standalone release/package-boundary work;
- expansion of *internally authored* benchmark corpora, metrics, baselines, or
  performance gates. Adopting **externally sourced, independently labelled
  validation data** is not deferred — see "External validation datasets" in the
  backlog. The distinction is that we do not invent new synthetic fixtures or
  new gates to chase; we do check the existing implementation against ground
  truth someone else produced;
- inventory-constrained placement, availability, sourcing, or pricing;
- a product viewer;
- a TUI;
- vision or image-to-assembly workflows;
- learned or creative-generation modes; and
- general-orientation generation beyond the current yaw-only generator.

Existing benchmark/evaluation utilities and BOM output are preserved, but no
new work is planned in the deferred benchmark or inventory areas.

## Assessment: screen-follow-ups review triage (2026-08-08)

Eight review comments were raised against the reduced-QP screen branch, all
but one labelled major. Three were adopted, five rejected as contrary to
checked-in repo policy, and one adopted for a reason other than the one given.

**Adopted.**

- *Lateral status was lost on the research basis.* `_score_report` sets
  `lateral=reduced.has_lateral`, but `_score_bricksim` never did, so every
  `screen_fields="bricksim"` verdict reported `lateral=False`. Clad layouts
  then fell into the `ReducedScreen.should_reject` stress-margin clause that
  `ScreenReport.lateral` exists to disable. `BricksimModel.has_lateral` now
  mirrors `reduced.py`'s own predicate (`any(key[2] != (0, 0, 1) ...)`) and
  reaches the report. Pinned by
  `test_bricksim_screen_reports_lateral_like_restricted`, which fails without
  the fix.
- *Connection-key type.* `bricksim_fields.py` spelled
  `tuple[int, int, tuple[int, int, int]]` longhand three times and carried one
  bare `list`, while `reduced.py` has defined `_ConnectionKey` for the same key
  since the screen landed. The alias is now shared, as `_PLATES_PER_STUD`
  already was.
- *Undeclared fence language* in the performance-testing guide — the only
  unlabelled opening fence in `docs/`.

**Rejected.** Five comments asked for `-> None` and parameter annotations on
new test functions, citing "always use type hints". That guideline is
operationalized for tests by `pyproject.toml`, which ignores `ANN001`,
`ANN002`, `ANN003`, and `ANN201` under `tests/**/*.py`; `pyrefly`'s
`project-includes` covers `src/` and `scripts/` only. The convention the
repo actually holds is that fixtures and helpers are typed while `def test_*`
is bare, and the new code already followed it. Adopting the comments would
have made the added tests the only annotated functions in their own files.
Changing that is a repo-wide decision, not a per-branch one.

**Adopted for a different reason than given.** One comment claimed a
`monkeypatch` shim in `tests/test_repair.py` lacked annotations; it had them.
But the annotations it had (`*args: object`) were too loose for the
`analyze` passthrough and were producing five of the seven live `ty` errors on
the tree. The shim now mirrors `analyze`'s signature. `ty` is down to two
diagnostics, both pre-existing: deliberate negative tests whose
`# type: ignore[arg-type]` pragma `ty` does not honour.

### Follow-up finding: the bricksim fields study is not apples-to-apples

Auditing the lateral defect surfaced a measurement problem in the
`screen_fields="bricksim"` write-up in
[`docs/guides/performance-testing.md`](docs/guides/performance-testing.md).
The cited artifact (`20260808T161914Z`) predates the vertical/SNOT domain
split: it pooled two shells and a clad model into one bucket at
`--candidates 15`, and its clad candidates ran with the lateral guard
inactive because of the defect above. The restricted numbers it is compared
against (`20260808T165542Z`) came from the later domain-split protocol at
`--candidates 30`. The guide therefore sets a pooled vertical+SNOT figure
against a vertical-only one, at half the candidate count.

Re-running the research basis under the restricted run's protocol, with the
lateral fix in place (`--fields bricksim --radii 8 10 --candidates 30
--skip-corpus --seed 0`, artifact `20260808T204443Z-screen-bench.json`),
replaces both headline figures:

| Axis | Guide (superseded) | Corrected re-run |
| --- | --- | --- |
| Candidate ranking, vertical | 83.7% | 100% |
| Candidate ranking, SNOT | not reported | 81.4% |
| Worst-consumer false rejects | 20% | 0% (both domains) |
| Verdict agreement | 100% | 100% |

The "bar NOT met" conclusion is not thereby overturned, but the two figures
carrying it are gone; what remains against the research basis is per-brick
score correlation near zero and a 1.03% non-converged share (below its own
threshold). The guide paragraph is deliberately left unedited — updating it
is the open action, together with deciding whether the restricted basis's own
pre-split numbers quoted in `ScreenReport.lateral`'s docstring (90.5% / 5.6%)
should be restated on the current protocol.
