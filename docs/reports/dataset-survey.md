# External dataset survey for testing and verification

Written 2026-08-08 on branch `bricksim-screen`, against the corpus in
`src/legolization/data/corpus/manifest.toml` (6 meshes + 13 synthetics),
the nine vendored StableLego fixtures in `tests/data/stablelego/`, and
`scripts/stablelego_sweep.py`.

A web survey of publicly available datasets that could widen this
project's test and verification surface. Each entry records what
verification gap it closes, the exact access path, the licence, and the
practical caveat. **Nothing here is a commitment** — this ranks a space,
`ROADMAP.md` owns what actually gets built.

## The gaps, in priority order

| # | Gap today | Best dataset | Why it closes the gap |
|---|---|---|---|
| 1 | RBE verdicts pinned by 9 fixtures + an optional 50k sweep | **StableText2Brick** | 47k structures, all labelled stable, per-voxel scores, MIT, 44 MB |
| 2 | Zero human-designed assemblies; `ldraw_in.py` only ever parses our own output | **LDraw OMR** | Thousands of real MPDs, real parts, human step order, CC BY |
| 3 | 6 meshes, all organic, all from one repo | **Thingi10K** + **ShapeNetCore** | Stratified mesh pathologies; ShapeNet gives same-input comparison |
| 4 | One `.vox` file (`heart.vox`); colour path barely exercised | **3D-Craft** houses | 2,586 multi-colour voxel builds *with human build order* |
| 5 | Instruction order checked only against internal invariants | **OMR `0 STEP`** + **WorkBenchMark** | An external answer key for step order and subassembly splits |
| 6 | Catalog facts come from a rate-limited API | **Rebrickable bulk CSV** + **LDraw parts library** | Hermetic, offline, daily-refreshed part truth |
| 7 | No independent check on the toppling / CoM branch | **ShapeStacks** + **COMPAS CRA** | Non-interlocking stability ground truth our LEGO sets never cover |

---

## 1. Brick-assembly stability ground truth

### StableText2Brick — the top recommendation

- <https://huggingface.co/datasets/AvaLovelace/StableText2Lego> (the
  dataset kept its `StableText2Lego` repo id; the paper renamed it
  `StableText2Brick`).
- 47,389 rows — 42,600 train / 4,790 test. **44.3 MB, Parquet, MIT.**
- Fields: `structure_id`, `object_id` (ShapeNetCore), `category_id`
  (21 categories), `captions`, `bricks`, `stability_scores`.
- `bricks` is one line per brick, `hxw (x,y,z)`, 1-unit-tall cuboids on a
  20×20×20 grid — the same world our `stablelego.layout_from_task_graph`
  already reads, so the adapter is a format shim, not new physics.
- `stability_scores` is a **20×20×20 per-voxel array**, the same
  convention `scripts/stablelego_sweep.py` already decodes from
  `stability_score.npy`.

Why this one first:

1. **It is a 47k-row "must return stable" regression.** Every row passed
   Liu et al.'s stability analysis during dataset construction (the
   BrickGPT paper, §3 and Appendix A: 62,000+ generated, ~47,000
   survived the filter). Any row where our RBE says *unstable* is either
   a real solver divergence or a documented modelling difference — both
   are findings.
2. **It is scored, not just labelled.** The per-voxel array lets us
   compare `max_score` numerically against the reference, not merely the
   binary verdict. That is strictly more signal than the nine fixtures.
3. **It is the accepted benchmark in this literature.** BrickSim's
   headline "100% accuracy on 150 real-world assemblies" is 150 rows
   sampled from exactly this dataset (see
   `references/bricksim-.../paper.md:194`). Sampling the same way gives a
   number directly comparable to the paper we are currently
   implementing a screen from.
4. It costs 44 MB and needs no Google Drive scraping, unlike the
   StableLego release.

Caveat: it is a *positive-only* set. The ~15k structures that failed the
filter are not published, so it tests false-negatives (we call a stable
thing unstable) and not false-positives. Pair it with StableLego's mixed
valid/invalid release for the other direction.

### PointCloud2Brick (STABLE, 2026)

- <https://github.com/miniHuiHui/STABLE>, models at
  <https://huggingface.co/miniHui/STABLE>.
- 42,604 train / 4,785 test, converted from StableText2Brick; same
  20×20×20 world, 8 brick types / 14 oriented variants, 21 ShapeNet
  categories. Adds point-cloud inputs.
- Mostly a re-cut of the same corpus — take it only if the point-cloud
  pairing is wanted for mesh-input work.

### BrickSim's own repository

- <https://github.com/intelligent-control-lab/BrickSim>.
- The repo README does not ship the 150-assembly evaluation set, but the
  set is reproducible: random sample from StableText2Brick. The paper's
  table (`paper.md:200`) reports solvable counts 98/150/150 across
  methods — a concrete target for our reduced-QP screen to be measured
  against.

### StableLego (already partly integrated)

- <https://github.com/intelligent-control-lab/StableLego>, MIT repo,
  dataset via the Google Drive link in its README.
- 50k+ objects across 55 ShapeNetCore categories; each has `vox.npy`,
  `task_graph.json`, `stability_score.npy`, `vis.png`. **Contains a mix
  of valid and invalid layouts** — the negative class StableText2Brick
  lacks.
- Already wired: nine fixtures vendored in `tests/data/stablelego/`,
  full sweep in `scripts/stablelego_sweep.py`. The unclosed part is that
  the sweep is manual and the dataset is Drive-gated, so it never runs in
  CI.

---

## 2. Human-designed assemblies (real LDraw)

Every `.ldr`/`.mpd` this project has ever parsed it also wrote. That is a
blind spot in `ldraw_in.py`, in catalog coverage, and in the SNOT and
submodel paths.

### LDraw Official Model Repository (OMR)

- <https://library.ldraw.org/omr>. Official LEGO sets rebuilt in LDraw,
  one MPD per model, with submodels and human-authored `0 STEP` lines.
- **Licence: CC BY 2.0 (CCAL 2.0) for existing files; new submissions
  require CC BY 4.0.** Files carry
  `0 !LICENSE Redistributable under CCAL version 2.0 : see CAreadme.txt`
  in the header, so per-file licence is machine-checkable — which suits
  the manifest's existing `license =` field.
- No official bulk archive from the site. The practical bulk path is
  **LTRON**: `pip install ltron && ltron_asset_installer` caches the OMR
  LDraw files to `~/.cache/ltron/collections/omr/ldraw` (~3 GB with the
  rest of its assets). <https://github.com/aaronwalsman/ltron>
- What it unlocks:
  - **Parser robustness** — real MPDs use submodels, mirrored transforms,
    unofficial parts, and colour codes we never emit.
  - **Catalog coverage as a metric** — "what fraction of parts in N real
    models does our catalog support" is a concrete, reportable number
    for `extend-lego-part-support`, replacing guesswork about which part
    to add next.
  - **Step-order answer key** — see gap 5.
- Caveat: OMR models use hundreds of part types far outside our catalog,
  so most will be *analysis* inputs, not generation targets. Scope it to
  parser + catalog-coverage tests first.

### LTRON random construction (`rc_6_6`)

- Same install. LDraw files **plus JSON metadata recording brick shapes,
  colours, instances per scene, and connections per scene**.
- The connection metadata is an external ground truth for
  `assembly_connections.py` — currently our connection graph is only
  ever checked against itself.

### BrickNet (CVPR 2026)

- <https://kulits.github.io/BrickNet>, arXiv 2604.22984.
- **100,000+ human-designed LDraw brick objects and scenes**;
  BrickNet-PT 320,808 samples, BrickNet-SFT 67,185.
- Largest human-designed LDraw corpus found. **Access is form-gated**
  (<https://forms.gle/dm4eYSa5gh4DqzRT6>) and neither the licence nor the
  sourcing (Rebrickable MOCs? OMR? scraped?) is stated publicly. Request
  it, but do not plan a pinned-hash corpus entry around it until the
  terms are known.

### MobileBrick

- 153 real LEGO models, multi-view RGBD with *precise CAD ground-truth
  geometry* — a genuine mesh↔brick pair, which would make a true
  round-trip test (legolize the ground-truth mesh, compare against the
  actual brick model).
- **Licence: CC BY-NC-ND 4.0.** Non-commercial *and* no-derivatives makes
  it unusable as a shipped corpus entry for this repo. Noted for
  completeness only. <https://arxiv.org/abs/2303.01932>

---

## 3. Mesh inputs

The current six meshes are all organic, all from
`alecjacobson/common-3d-test-models`. Two additions cover different axes.

### Thingi10K — pathology coverage

- <https://github.com/Thingi10K/Thingi10K>,
  <https://huggingface.co/datasets/Thingi10K/Thingi10K>,
  `pip install thingi10k`.
- 10,000 real 3D-printing meshes from Thingiverse, **with per-model
  geometric metadata**: manifoldness, orientability, component count,
  self-intersection, degenerate faces, genus — and a per-model licence
  field.
- This is the point: it lets us build a *stratified* input-robustness
  suite instead of hoping a pretty mesh happens to be broken. Direct
  targets — `mesh.py`'s fill fallback (currently exercised by exactly one
  non-watertight model, Suzanne), the `largest_component_only` filter
  (exercised by exactly one, Homer), and the aspect-correct grid path.
- Licences vary per model; filter on the metadata's licence column to a
  CC0/CC-BY subset before pinning anything.

### ShapeNetCore — same-input comparison

- The source of both StableLego and StableText2Brick. Taking the same
  `object_id` a StableText2Brick row was built from and running it
  through our pipeline gives **their layout and our layout for the
  identical input**, both scorable by both solvers.
- That is the strongest available evidence for placement-quality claims:
  not "our objective went down" but "on the same object, our layout is
  stable/denser/fewer-bricks than the published one."
- Requires a ShapeNet account (research-use terms); the `object_id`
  column makes the join trivial once you have it.

### Objaverse / Objaverse-XL

- <https://objaverse.allenai.org/>, 800K+ objects, **ODC-By 1.0**,
  captions and tags, Hugging Face hosted.
- Scale and diversity for a broad regression sweep. Quality is uneven
  (artist uploads); needs the same kind of metadata filtering as
  Thingi10K, which it has less of.

### Toys4K

- <https://github.com/rehg-lab/lowshot-shapebias/tree/main/toys4k>
- 4,179 instances, 105 categories, ≥15 per category, sourced from
  CC/royalty-free Blendswap/Sketchfab/Poly/Turbosquid.
- **The best shape-prior match to what people actually legolize** —
  toys, not scans. Access is form-gated; obj files ship without
  materials, so colour work would need the `.blend` set.

---

## 4. Voxel and colour inputs

`data/examples/heart.vox` is the only `.vox` in the repo, and
`letter-h-bicolour.npy` is the only colour-constraint fixture.

### 3D-Craft / CraftAssist house data

- Houses: `https://craftassist.s3-us-west-2.amazonaws.com/pubr/house_data.tar.gz`
- Segmentation: `https://craftassist.s3-us-west-2.amazonaws.com/pubr/instance_segmentation_data.tar.gz`
- <https://github.com/facebookresearch/craftassist>,
  <https://github.com/facebookresearch/voxelcnn>
- **2,586 crowd-built Minecraft houses**, each recorded as a
  timestamped sequence `[timestamp, user_id, x, y, z, block_id,
  place/break]`, 253 distinct block IDs.
- Two distinct uses:
  1. Large **multi-colour voxel inputs** — block IDs map to a palette,
     which is exactly the colour-constraint regime
     `letter-h-bicolour` gestures at with 2 colours and 253 available.
  2. **Human build order.** The place/break sequence is a real person's
     construction order for a real structure. That is an external
     sanity check on the instruction sequencer's ordering heuristics
     that no LEGO dataset offers. MEPNet used this same house set for
     precisely this reason.
- Direct S3 download, no gating. Structures are architectural (walls,
  floors, roofs) — different failure modes from our organic meshes.

### MagicaVoxel collections

- <https://github.com/ephtracy/voxel-model> — the official `.vox` sample
  set from MagicaVoxel's author. Best `.vox` *format* conformance test
  (multi-model scenes, palette chunks, extension chunks).
- <https://github.com/enkisoftware/voxel-models> — **CC BY 4.0**, safe to
  pin in the manifest.
- <https://github.com/lquesada/voxel-3d-models> — CC BY-NC-SA 3.0.
  Non-commercial; **do not** put it in a shipped corpus.

---

## 5. Instruction and step-order ground truth

### OMR step structure

The highest-value and cheapest option, because gap 2 already downloads
it. OMR MPDs carry human-authored `0 STEP` lines and submodel
decomposition — an answer key for `instructions/subassembly.py` and the
step sequencer. Metrics that become possible: step-size distribution
versus human authors, submodel-boundary agreement, and whether our
"floating parts" invariant ever fires on a model a human shipped.

### WorkBenchMark (arXiv 2606.19358, June 2026)

- <https://workbenchmark.github.io/>
- **400 LEGO Duplo assembly tasks in four complexity tiers**, plus a
  simulation environment and an assembly-by-disassembly baseline. Tiers
  explicitly scale order dependencies, interlocking sub-assemblies,
  tight tolerances, and multi-layer insertions.
- Directly relevant to the insertion-pressure audit and the
  disassembly-rescue path in the sequencer. Duplo geometry, so the
  physics constants differ, but the *ordering* structure transfers.
- Release stated as forthcoming — verify availability before planning
  around it.

### MEPNet (ECCV 2022, arXiv 2207.12572)

- "Translating a Visual LEGO Manual to a Machine-Executable Plan."
  Introduces three LEGO manual datasets plus the Minecraft house set.
- Public dataset links were not found in this survey; the datasets are
  described in the paper but no release URL surfaced. Worth an email if
  step-order ground truth becomes a priority.

### IKEA-Manual (arXiv 2302.01881)

- 102 objects, 3–21 parts each, with per-step part connections, manual
  segmentations, and 3D poses.
- Not bricks — but a well-designed precedent for *how to score* a step
  plan against a human one. Read for the metric design, not the data.

---

## 6. Parts-catalog fidelity

### Rebrickable bulk CSV

- <https://rebrickable.com/downloads/> — `parts.csv`, `elements.csv`,
  `colors.csv`, `sets.csv`, `inventories.csv`, `inventory_parts.csv`,
  `part_relationships.csv`, `themes.csv`, `minifigs.csv`. **Refreshed
  daily; the site states the files may be used for any purpose.**
- `catalog_infer/sources.py` already has a `rebrickable-api` rung
  (`REBRICKABLE_API_KEY`, rate-limited, network-dependent). The bulk dump
  gives the same facts **hermetically and offline** — which is what
  `test_catalog_infer.py` actually wants. `part_relationships.csv` also
  resolves the mould/print/pair variants that currently confuse part-id
  lookups.
- Mirror scripts if a SQLite view is preferred:
  <https://github.com/jncraton/rebrickable-sqlite>.

### LDraw parts library

- <https://library.ldraw.org/updates> — `complete.zip`, **17,052 unique
  shapes**, CCAL 2.0.
- Real `.dat` geometry for every official part. For
  `extend-lego-part-support`, footprint and height could be *derived from
  the actual primitive geometry* rather than estimated — turning
  `catalog_infer/geometry.py` estimates into measurements.
- Git mirrors with version control: <https://github.com/pybricks/ldraw>,
  <https://github.com/ctiller/ldraw>.

### BrickLink

- Already supported as a local export (`bricklink-catalog-dump` in
  `sources.py`, and `scripts/ingest_bricklink_masses.py`). No change
  recommended; noted so the ladder is documented in one place.

---

## 7. Independent (non-brick) physics cross-validation

Every LEGO dataset above was labelled by *the same family of solver we
are implementing*. Agreement with them is consistency, not correctness.
These two are independent.

### ShapeStacks

- <https://ogroth.github.io/shapestacks/>,
  <https://github.com/ogroth/shapestacks>, arXiv 1804.08018.
- **20,000 simulated block-stacking scenarios** — cubes, cuboids,
  cylinders, spheres — each with a **binary stability label**, plus
  segmentation identifying *which object violates stability* and which
  falls first.
- No knob interlocking, so it does not test our friction capacities. But
  it is exactly the `ground_pull=False` regime described in
  `physics-fidelity-notes.md` §1: bodies resting loose on a table, where
  stability is pure CoM-over-support-polygon and toppling. That branch —
  the one `topple-arm` pins with a single fixture — would get 20,000
  independently-generated cases, including the "which brick is at fault"
  label to check our per-brick attribution, not just the global verdict.
- MuJoCo MJCF scene files; would need a converter to our layout, and the
  cuboids-only subset is the tractable slice.

### COMPAS CRA / compas_rbe

- <https://github.com/BlockResearchGroup/compas_cra>,
  <https://github.com/BlockResearchGroup/compas_rbe>,
  <https://blockresearchgroup.github.io/compas_cra/latest/>
- Coupled Rigid-Block Analysis for masonry — **the closest published
  analogue of our LP formulation**, from a group that ran "an extensive
  benchmark campaign" on 3D discrete-element assemblies of rigid blocks.
- The strongest available external evidence for the "LP is exact" claim
  recorded in the audit findings: run both formulations on the same
  block assemblies and compare contact forces, not just verdicts. CRA
  uses a nonlinear penalty formulation, so disagreement is informative
  in both directions.
- Python, open source, installable — the lowest-friction cross-check on
  this list after StableText2Brick.

### Fusion 360 Gallery Assembly / Assemble Them All

- <https://github.com/AutodeskAILab/Fusion360GalleryDataset> — 8,251
  multi-part assemblies, 154,468 bodies, 32,148 parametric joints with
  axis, pose, and contact metadata.
- <https://github.com/yunshengtian/Assemble-Them-All> — SIGGRAPH Asia
  2022; thousands of physically valid industrial assemblies with
  required assembly motions (two-part set 221.1 MB, rotational 5.4 MB).
- Both are about *insertion directions and disassembly feasibility* —
  the same question our straight-down-insertion audit asks, in a
  non-brick setting. Lower priority than the above, but the only
  large-scale insertion-motion ground truth found.

---

## Suggested sequencing

1. **StableText2Brick** — 44 MB, MIT, Parquet, same grid convention as
   code we already have. Add an adapter next to `stablelego.py`, pin a
   deterministic sample as a fast test, run the full 47k as an offline
   release sweep alongside `scripts/stablelego_sweep.py`. Report the
   150-sample number for direct comparison with BrickSim.
2. **Rebrickable bulk CSV** — makes `catalog_infer` tests hermetic and
   kills a network dependency. Small, permissive, immediately useful.
3. **OMR via LTRON** — real MPDs for parser robustness plus a
   catalog-coverage metric. CC BY, attribution is machine-readable from
   the file headers.
4. **Thingi10K stratified subset** — pick ~10 models by *metadata
   pathology*, not by looks; pin hashes in the manifest the same way the
   six meshes are pinned.
5. **ShapeStacks cuboid subset** — the only independent check on the
   toppling branch. Needs a converter; worth it because it tests the one
   claim no LEGO dataset can.
6. **COMPAS CRA** — cross-validate the LP formulation itself. Highest
   evidentiary value, highest effort.
7. Request **BrickNet** access and watch for the **WorkBenchMark**
   release; neither is plannable until terms and availability are known.

## Licence summary

| Dataset | Licence | Shippable in a pinned corpus? |
|---|---|---|
| StableText2Brick | MIT | Yes |
| StableLego (repo) | MIT | Yes (dataset terms unstated) |
| LDraw OMR | CC BY 2.0 / 4.0 | Yes, with attribution |
| LDraw parts library | CCAL 2.0 | Yes, with attribution |
| Rebrickable CSV | "any purpose" per site | Yes |
| Thingi10K | Per-model, in metadata | Yes, after filtering |
| Objaverse | ODC-By 1.0 | Yes, with attribution |
| enkisoftware voxel-models | CC BY 4.0 | Yes |
| 3D-Craft / CraftAssist | Repo is MIT | Likely; confirm data terms |
| ShapeStacks | Research use | Test-only |
| ShapeNetCore | Research-use account | Test-only |
| Toys4K | Form-gated, CC/royalty-free | Test-only |
| BrickNet | Form-gated, unstated | Unknown — do not plan on it |
| lquesada voxel-3d-models | CC BY-NC-SA 3.0 | **No** (non-commercial) |
| MobileBrick | CC BY-NC-ND 4.0 | **No** (NC + ND) |
