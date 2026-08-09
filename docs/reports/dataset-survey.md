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

> **Scope, decided after this survey was written.** The programme it fed
> narrowed to **voxel and brick data only**. **Thingi10K**, **ShapeNetCore**,
> and **ShapeStacks** are therefore *not* adopted: they are pure geometry or
> non-brick physics, and none of them tests a brick placement, a connector, or
> an assembly order. They are out of the dataset registry
> (`scripts/datasets.toml`) and recorded as **speculative follow-up work at the
> end of `ROADMAP.md`**. Their sections below are kept as the dated record of
> what the survey found — read them as research, not as plans. COMPAS CRA is
> the one non-brick source that *was* adopted, because it is the only
> independent check on the equilibrium formulation itself.

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
4. ~~**Thingi10K stratified subset**~~ — **cut on scope** (pure geometry); see
   the banner at the top and `ROADMAP.md` "Speculative follow-up work".
5. ~~**ShapeStacks cuboid subset**~~ — **cut on scope**, and separately its host
   turned out to be dead (see the addendum). COMPAS CRA took over its role as
   the independent check.
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
| MobileBrick | ~~CC BY-NC-ND 4.0~~ **MIT** | **Yes** — corrected, see addendum |

---

# Addendum, 2026-08-08 — corrections and measured figures

Written the same day as the survey above, after acquisition planning. The survey
body is left as written; this section supersedes it where they disagree.

## Two licence corrections

**MobileBrick is MIT, not CC BY-NC-ND 4.0.** The survey read the licence badge
off the arXiv abstract page, which describes *the paper*. The dataset and code
are MIT: <https://github.com/ActiveVisionLab/MobileBrick/blob/main/LICENSE>
("MIT License, Copyright (c) 2023 Active Vision Laboratory"). It is therefore
usable, and it is adopted — see `ROADMAP.md`, "External validation datasets".

**BrickNet's licence is MIT and most of its value is not gated.** The survey
listed it as "form-gated, unstated, do not plan on it". Verified: the repo is
MIT (`api.github.com/repos/kulits/BrickNet` → `spdx_id: MIT`), it ships on PyPI
as `bricknet`, and the request form covers **only** the training graph splits.
Not gated, and adopted:

- bundled in the wheel — `part_names.json` (14,583 parts), `labels.json.xz`
  (per-part connector labels, 14,603 parts), `part_aliases.json.xz`
  (2,522 rows), `color_names.json` (258 colours). Vendored to
  `references/bricknet-data/`.
- direct download, no form — `inset.tar.xz` (368,855,964 B, per-part watertight
  collision PLYs) and `ldraw.tar.xz` (97,779,272 B, the part-library snapshot
  the vocabulary was built from).

The gated splits (pt 253,623 / sft 67,185 / val 512 graphs) are **declined** on
terms, not on value.

## Measured download figures

Every figure below is a measured `Content-Length` or `Content-Range` from an
HTTP probe on 2026-08-08, not a published estimate.

| Dataset | Download | Resident after extract |
|---|---:|---:|
| StableText2Brick (7 parquet) | 44.3 MB | 44 MB |
| BrickNet `inset.tar.xz` | 368.9 MB | ~1.6 GB |
| BrickNet `ldraw.tar.xz` | 97.8 MB | ~350 MB |
| LDraw `complete.zip` | 144.7 MB | ~450 MB |
| Rebrickable bulk CSVs (12 files) | 17.6 MB | ~90 MB |
| COMPAS CRA samples (16 JSON) | 221 KB | 221 KB |
| 3D-Craft `house_data.tar.gz` | 562.6 MB | ~2.5 GB |
| 3D-Craft `instance_segmentation_data.tar.gz` | 2.0 MB | ~10 MB |
| MobileBrick `MobileBrick_Mar23.zip` | **13.1 GB** | ~300 MB selective |
| OMR `.mpd` crawl | ~0.6–1.5 GB | same |

Measured but **cut on scope** — kept for the speculative section in
`ROADMAP.md`, not in the registry:

| Dataset | Download | Resident after extract |
|---|---:|---:|
| ShapeStacks `mjcf` + `meta` | 39.2 MB | ~150 MB |
| Thingi10K metadata CSVs (6) | 6.6 MB | 6.6 MB |
| Thingi10K `raw_meshes.tar.gz` | 9.6 GB | ~12 GB |

Two figures worth calling out because they are far off the published
impressions:

- **MobileBrick is 13.1 GB** (13,128,305,497 B), not the tens of megabytes its
  "153 objects" framing suggests. It is ~99% RGBD imagery. Only
  `*/mesh/gt_mesh.ply` matters here, so extract selectively — ~300 MB — and
  never unpack the archive whole.
- **Thingi10K's full HF repo is 70.3 GB**; the raw meshes alone are 9.6 GB. The
  `metadata/` CSVs are 6.6 MB and carry the manifoldness / closure /
  component-count / self-intersection / licence columns, so a stratified subset
  can be *chosen* for 6.6 MB and only the selected meshes fetched.

## Access notes the survey did not have

- **ShapeStacks** does not need the 33 GB RGB tarball. `shapestacks-mjcf.tar.gz`
  (39 MB) plus `shapestacks-meta.tar.gz` (156 KB) carry the geometry and the
  stability labels, which is the whole of what a solver check needs.
- **OMR has no bulk archive and no API.** Set pages are
  `https://library.ldraw.org/omr/sets/<id>` for id ≈ 1..4000 (5000 → 404), each
  linking direct MPDs at `https://library.ldraw.org/library/omr/<name>.mpd`
  (`10001-1.mpd` is 621,839 B). `library.ldraw.org/library/omr/` itself returns
  403, so a polite id crawl is the only route. `scripts/fetch_omr.py` does this
  at ≥1 s/request, resumably, and records each file's `0 !LICENSE` header so
  attribution is machine-derived.
- **LTRON** (the survey's suggested OMR bulk path) is Ubuntu-oriented — its
  renderer needs OpenGL 4.6 and `splendor-render` is Linux-only. The direct
  crawl avoids a 3 GB install for files we can fetch in the open.
- **ShapeStacks is gone.** Diagnosed rather than assumed:
  `shapestacks-file.robots.ox.ac.uk` resolves (129.67.94.117) but ports 80 and
  443 are closed/filtered; `http://shapestacks.robots.ox.ac.uk/static/download/v1/…`
  301-redirects to `https://ogroth.github.io/shapestacks/static/download/v1/…`,
  which 404s; and the `ogroth/shapestacks` GitHub release (v1.0) carries no
  assets. No public mirror found. **COMPAS CRA** is substituted as the
  independent physics check — MIT, 22 sample discrete-element assemblies
  (3.5 MB of JSON, readable without installing the package, which pins
  `pyomo==6.4.2`). Its `shelf-stable` vs `shelf-s1/s2/s3` variants are the
  independent verdict pairs, and `tests/test_cra.py` pins its force convention
  (a unit cube on a unit interface puts 0.25 on each of four corners), which is
  directly comparable to our per-contact forces.

## First results, 2026-08-08

Everything below is from a run of the scripts this addendum introduces.

### The score conventions of the two brick datasets are inverted

Established empirically, not read off a paper. Over the StableText2Brick test
shard, **every unoccupied voxel is exactly 1.0** (all 355,660 across 50
structures) and occupied voxels lie in [0.7176, 1.0]. So its `stability_scores`
is a **margin** — higher is better, and the paper's rule is "stable iff every
brick scores > 0".

StableLego's `stability_score.npy` is the **opposite**: its README legend reads
*"Black: more stable. Red: higher internal stress. White: collapsing bricks"*,
i.e. a stress, matching our `max_score` and confirming the convention already
assumed in `scripts/stablelego_sweep.py:112`. That assumption is correct; the
two datasets simply disagree with each other. Reading either backwards would
invert every verdict while still producing numbers in [0, 1], so both
conventions are now pinned by tests in `tests/test_datasets.py`.

### Solver agreement: 150/150, matching BrickSim

`uv run python scripts/stabletext2brick_sweep.py --sample 150 --seed 0` — the
same sample size BrickSim reports (`references/bricksim-*/paper.md:194`,
scoring 150/150 at `:200`):

- **150 agree, 0 disagree, 0 skipped.**
- Score residual `|(1 − their margin) − our max_score|`: median 0.0020,
  mean 0.0223, p95 0.0959, **max 0.5244**.
- Correlation of `(1 − margin)` with `max_score`: 0.781.

The median says the two quantities track each other almost exactly; the tail
says they are not the same function. The handful of large residuals (structures
where they see 5–25% stress and we see under 3%) are the interesting cases and
have not been diagnosed. Caveat that bounds all of it: the set is
**positive-only**, so this measures false negatives and says nothing about
false positives.

### Catalog coverage over real sets: 14.4%

`ldraw_coverage.py --cross-check` over the first 154 OMR sets, 78,409 part
occurrences:

- **0 of 154 models import completely**; coverage is 14.4% of occurrences.
- Failures: 62,494 part-not-in-catalog, 2,240 rotation-not-yaw,
  1,532 colour-not-solid, 178 collision, 83 off-grid, 82 sideways-unsupported.
- The 1,421 rejected codes are **not all parts**: classified against the
  official library, 41,916 occurrences are real parts, 19,914 are geometric
  primitives (`stud`, `4-4ring3`, `box4o8a`), and 664 are unknown. The
  primitives arrive because some OMR MPDs inline unofficial part *definitions*
  as submodels and the analysis descends into them. Only the real-part count is
  addressable by extending the catalog — reporting the raw 1,421 would have put
  primitives at the top of a what-to-support-next list where they can never
  belong.
- Top real parts by occurrence: `166` (7,387), `2780` (2,588), `u9190` (2,380),
  `77` (2,038), `6558` (1,296), `54200` (891).
- **BrickNet's independent parser disagrees with ours on 7 of 154 models**,
  always by 1–3 parts (e.g. `885-1.mpd` 26 vs 28, `891-1.mpd` 45 vs 48). Not yet
  diagnosed; one of the two parsers is wrong in each case.

### The beauty scalar: one term is pointing the wrong way

`aesthetics_baseline.py` — the ROADMAP's *"validate the beauty scalar against
human judgement"* item. Both terms are errors, lower is better. Medians:

| term | human (OMR) | ours | algorithmic (S2B) |
|---|---:|---:|---:|
| `symmetry` | **0.0761** | 0.3478 | 0.4694 |
| `perpendicularity` | 0.6165 | **0.5380** | 0.4698 |

- **`symmetry` points the right way.** Human builds are ~4.5x better than ours
  at the median, and the algorithmic set is worst. That gap is real headroom.
- **`perpendicularity` does not.** We already score *better* than official LEGO
  sets, and the delete-and-rebuild algorithmic set scores better still — an
  ordering exactly inverted from how the three populations actually look. Being
  ahead of human designers on a beauty term while our output still reads as
  machine-made is evidence the term is not measuring what makes a build look
  right, and a reason to revisit its weight in `ObjectiveWeights` rather than
  optimize harder into it.

Methodological limit: no OMR model imports completely, so the human rows score
each set's *basic-brick skeleton*. That skeleton is the vocabulary our generator
works in, which is why the comparison is meaningful, but it is not a comparison
of finished sets.
