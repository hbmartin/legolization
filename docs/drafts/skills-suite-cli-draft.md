# Legolization 0.6 CLI and bundle contract — draft

> Draft, not implementation. This records decisions confirmed through 2026-08-06.

## Command model

The legacy bare invocation (`legolization INPUT ...`) is removed immediately. The existing explicit `build`, `validate`, `cache`, and `analyze` commands remain. The CLI gains `--version` plus these commands:

```text
legolization bundle
legolization input inspect
legolization model render
legolization instructions audit
legolization catalog infer
legolization catalog validate
legolization corpus list
legolization corpus generate
legolization corpus download
legolization corpus verify
legolization corpus collect
legolization corpus assemble
legolization corpus evaluate
```

Legacy compatibility scripts for instruction checking, corpus evaluation/assembly/collection, and render preflight/rendering are removed rather than wrapped. Their reusable behavior moves into `src/legolization` and the new CLI commands. Unrelated benchmark, profiling, ingestion, and stability scripts remain.

All new commands provide readable default output. With `--json`, stdout contains exactly one envelope; progress and warnings go to stderr. Errors under `--json` still emit the same envelope on stdout.

```json
{
  "schema": "legolization.result/v1",
  "version": "0.6.0",
  "command": "bundle",
  "status": "complete",
  "exit_code": 0,
  "artifacts": [],
  "warnings": []
}
```

The optional `error` object is present on failures. Exit codes are fixed:

| Code | Meaning |
| ---: | --- |
| 0 | Complete. |
| 1 | Operational error. |
| 2 | Unbuildable or failed physics. |
| 3 | Partial or indeterminate outcome, including incomplete instruction ordering, partial rendering, and provisional timed results. |
| 4 | Existing exact-limit error. |
| 130 | Interrupted after atomically recording resumable partial state. |

## Bundle contract

`legolization bundle` is the complete noninteractive pipeline. It accepts native `.vox`, `.npy`, `.obj`, `.stl`, and `.ply` input, plus `.ldr` and `.mpd`.

- Native input is generated into a new assembly.
- LDraw input is preserved for analysis/publication by default.
- `--retile` explicitly turns an imported assembly into a colored target grid and optimizes it; the report records that imported assembly was the shape authority.
- Project TOML and normal CLI overrides use the current configuration system, with CLI values taking precedence.
- Direct invocation defaults to balanced quality, SNOT off, and slopes/tiles preserved.

The default directory is a sibling with the operation-specific suffix. A mismatched existing explicit output also receives a numeric sibling. `--fresh` forces a fresh numeric sibling run.

### Bundle contents

Stages omit directories/artifacts that do not apply, but a completed full generation has this portable layout:

```text
bundle.json
model/
  model.mpd
  model.ldr
bom/
  bom.json
instructions/
  instructions.html
  instructions.pdf
comparison/
  report.json
diagnostics/
renders/
```

`bundle.json` is the authoritative record. It stores source identity, effective configuration, software/library/catalog versions and hashes, artifacts, stage status, warnings, and verdicts. When safe it records a relative source path; otherwise it records only filename/hash, never an absolute source path. The manifest is atomically updated after stages complete.

MPD output preserves subassemblies and LDR output is flattened. Candidate model files are not retained for every successful candidate: the winner and complete comparison report are enough. When no candidate is buildable, diagnostics retain the best rejected model with explicit reasons.

### Resume, retries, and cancellation

Bundle identity is the input content hash, effective configuration hash, Legolization version, and parts-catalog hash. It intentionally ignores timestamps and output paths. Identity-matched completed or incomplete work resumes by default. Artifact drift is regenerated directly in place without archiving old artifacts.

At a soft deadline, detached, identity-stamped candidates may keep running. A later identity-matched resume may adopt valid late results. If completed buildable candidates exist at the deadline, the bundle publishes the best completed candidate as a **provisional** partial result (exit 3), eligible to be replaced on resume by a better late candidate.

`bundle --cancel-pending` terminates its identity-matched detached candidate workers and records them as cancelled. It retains already-completed valid candidate artifacts and diagnostics for a later identity-matched resume, and keeps the partial bundle as the cancellation record.

An initial unbuildable result is exit 2. The skill may then ask for a total retry budget and invoke a second bundle operation. That operation tries only this material ladder: four-plate shell, six-plate shell, then solid; it stops at the first buildable result. It never silently rescales or drops components. The total retry time is shared fairly between rungs, and unused time rolls forward.

## Quality and generation selection

| Quality | Candidate policy | Time policy |
| --- | --- | --- |
| Fast | Greedy, seed 0. | 2 minutes. |
| Balanced | Full ordinary sweep, seed 0, plus global exact when preflight is eligible. | 15 minutes. |
| Exhaustive | Ordinary strategies at seeds 0, 1, and 2, plus global exact once when eligible. | User must choose duration. |

Workers use up to the logical CPU count. The global exact strategy is skipped with a recorded reason when preflight makes it ineligible. Source colors are compared automatically across hard/no-dither, soft/no-dither, and soft+dither variants; duplicates are collapsed for simple or uncolored inputs. The winner report includes the variant identity and color-error/objective tradeoff.

## Input inspection

`input inspect` accepts only the five native source formats. Unsupported inputs receive clear conversion guidance; the CLI does not become a general 2D or 3D converter.

- Meshes are inspected and receive a recommended target stud size; the user confirms it through the skill.
- Voxel inputs receive a recommended plates-per-voxel value; the user confirms it through the skill.
- A high-confidence mesh up-axis is accepted automatically; ambiguous orientation is asked about.
- Embedded mesh colors are sampled automatically. Only colorless meshes prompt for a uniform LEGO color.
- A write option emits normalized `.npy` output plus JSON sidecar containing scale, orientation, hashes, and warnings.

## Rendering and instructions

`model render` creates requested view images. A requested set with some successes returns exit 3; zero successful views return exit 1; all successful views return 0. Existing outputs use numeric filename suffixes rather than archive directories.

Renderer setup is explicit and platform-specific: LDView on macOS, LeoCAD through `winget` on Windows, and LeoCAD plus Xvfb on Ubuntu. The managed official LDraw library is separate from renderer installation, lives in platform user-data storage, validates downloaded `complete.zip` content/hash/metadata, and checks for updates weekly.

`publish-lego-instructions` always emits a step-annotated MPD and an audit report. It emits HTML/PDF only if rendering is available. `bundle --render=auto` renders when dependencies are present; missing dependencies merely omit booklets and record the omission. `--render=required` makes missing or failed rendering partial (exit 3), while `--render=off` intentionally omits booklets with exit 0.

If the user declines renderer installation, the skill uses `--render=off`: no HTML/PDF or placeholder pages are written. If parts setup fails but rendering was requested, the skill continues core generation and uses required rendering, producing an exit-3 partial outcome. If rendering fails for some instruction steps, retain a partial booklet with explicit missing-step markers and exit 3. If no renderer was requested/available, omit booklets entirely.

Instruction generation always enables subassemblies and insertion-press auditing. A buildable model with incomplete stable/insertion-safe order still produces a warned booklet and exit 3. Booklets are English-only; page size is inferred as Letter or A4 from locale, falling back to Letter when unknown. Automatic step density targets 3, 5, 7, and 10 parts/step at model sizes under 50, 50–149, 150–399, and 400+ parts respectively, with further splits for physics, subassemblies, and insertion concerns.

## Analysis, repair, and catalogs

Assembly analysis always writes a diagnostic directory with JSON reports, graph/components/floating output, HTML diagnostics, and renders when available. Indeterminate results include topology-only suggestions explicitly marked unverified.

Repair never overwrites the input. It starts with the existing BOM-preserving counterfactual approach and may redesign if necessary. Effort tiers are fast (1 minute), balanced (5 minutes), and exhaustive (user-chosen duration). Failed repair retains the best rejected model and a complete explanation.

Catalog extension commands write a `-legolization-support` sibling bundle:

```text
catalog-extension.json
draft-estimates.json
sources.json
validation.json
geometry/
```

Authoritative online sources are looked up automatically and cited in the result; physical values may be estimates. Inferred geometry becomes active only after import, round-trip, collision, connector, and topology validation; otherwise generic complete geometry is required. A validated overlay activates automatically. Upstream built-in catalog changes require confirmation.

Generation, analysis, and repair support repeatable `--catalog PATH` / `catalog.extensions` and repeatable `--catalog-estimates PATH` / `catalog.estimate_sidecars`. Explicit estimate sidecars are allowed to satisfy fully validated physics with no safety adjustment; provenance remains labeled.

## Corpus commands

Corpus generators and manifests become package code. Generated/downloaded inputs live in platform user-data storage. Collections and scorecards default to `./legolization-eval/`; outside a checkout, `--write-baseline` defaults to `./legolization-eval/baselines/`.

`corpus evaluate` runs all currently available inputs—synthetic and downloaded mesh inputs—while skipping unavailable meshes. It collects and assembles in one operation, retaining current multi-seed behavior. Baselines change only through explicit `corpus assemble --write-baseline`; the skill must confirm before forwarding that mutation.
