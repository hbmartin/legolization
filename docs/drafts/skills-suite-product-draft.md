# Legolization skills suite — product draft

> Draft, not implementation. This records decisions confirmed through 2026-08-06.

## Purpose

The skills suite makes Legolization approachable through natural conversation while preserving the package's advanced generation, analysis, rendering, instruction, catalog, and evaluation capabilities. It is designed for mixed-experience users: a skill should use plain language, ask only material questions, recommend an option when it asks, and reveal advanced controls on request.

The suite is Codex-first but portable to every agent supported by `npx skills`. Users install the complete collection with:

```sh
npx skills add hbmartin/legolization --all
```

Each skill will require `legolization>=0.6.0`. When the CLI is missing or too old, the skill installs or upgrades the latest stable release through `uvx.sh`; a failed installation is reported and stops the requested operation.

## Canonical skill catalog

| Skill | User-facing purpose | Primary CLI surface |
| --- | --- | --- |
| `legolize-model` | Turn a mesh or voxel model into a stable LEGO-style model and complete bundle. | `bundle` |
| `prepare-lego-input` | Inspect, orient, color, and normalize a source model before generation. | `input inspect` |
| `optimize-lego-build` | Compare, retile, or improve an existing LDraw assembly. | `bundle --retile` |
| `publish-lego-instructions` | Produce a step-annotated MPD and, when rendering is available, HTML/PDF instructions. | `bundle` / `instructions audit` |
| `analyze-lego-assembly` | Diagnose stability, load paths, insertion concerns, and assembly risks. | `analyze` |
| `repair-lego-assembly` | Propose and validate a repair or redesign without overwriting the source. | `bundle` / repair workflow |
| `extend-lego-part-support` | Research, estimate, validate, and activate catalog extensions. | `catalog infer`, `catalog validate` |
| `render-ldraw` | Render a model or bundle from requested views. | `model render` |
| `inspect-instructions` | Audit buildability, ordering, insertion pressure, and booklet readiness. | `instructions audit` |
| `eval-corpus` | Run repeatable strategy evaluation across the available corpus. | `corpus evaluate` |

There is deliberately no routing skill. `legolize-model` owns complete new-model creation; `optimize-lego-build` owns improvement of an existing assembly.

## Conversation contract

All skills support ordinary natural-language requests as well as explicit invocation such as `$legolize-model`. Their metadata permits implicit invocation, and the default prompts quote the explicit skill name. Every skill provides an `agents/openai.yaml` interface description.

The interaction model is:

1. Explain the likely outcome in plain language.
2. Inspect the supplied input where possible before asking questions.
3. Ask only when a choice materially changes the result; recommend a choice when the user has not expressed a preference.
4. Run the corresponding CLI operation and present the resulting bundle, verdict, warnings, and next useful action.
5. Preserve identity-matched incomplete work and resume it by default.

The direct CLI remains noninteractive. The skills, not hidden scripts, are responsible for conversational setup and for calling supported CLI commands.

## Installation and platform setup

The root `skills/` directory is the only canonical skill location. Existing `.claude/skills` copies will be removed rather than retained as aliases or wrappers. The installer/discovery CI uses `npx skills@1` to pin the major version while accepting current `1.x` releases.

Two setup operations are intentionally provided as paired shell and PowerShell scripts:

| Owner skill | Setup operation | Platforms and policy |
| --- | --- | --- |
| `legolize-model` | Install or update the managed official LDraw parts library. | Store under platform user-data locations without admin privileges; check weekly and continue silently if a valid existing library remains usable. |
| `render-ldraw` | Install or configure the renderer. | macOS: LDView; Windows: LeoCAD through `winget`; Ubuntu: LeoCAD plus Xvfb. Ask before installing a renderer. |

All other operational behavior belongs in the CLI rather than additional helper scripts. The managed parts library is validated after download and records its source/version/hash metadata. Existing renders are retained when the renderer or library changes.

## Visual and brand direction

Each of the ten skills receives original, directly drawn geometric-vector SVG glyphs in small and large forms. The visual language is multicolor brick-palette geometry with brand red `#D92B2B`. It may use generic brick imagery but not the LEGO logo, wordmark, minifigure silhouette, copied packaging, or other trademarked visual assets.

Repository documentation will say the project is unaffiliated with and not endorsed by the LEGO Group.

## Results and handoff

Skills produce portable bundle directories, not opaque chat-only artifacts. Operation-specific sibling names are used by default:

```text
<name>-legolization
<name>-prepared
<name>-optimized
<name>-instructions
<name>-analysis
<name>-repair
<name>-legolization-support
```

If a target already exists, numeric suffixes avoid overwriting it. A bundle directory can be supplied to downstream skills, which locate its primary model through `bundle.json`.

Every full generation outcome includes the winning model, BOM, instructions when rendered, comparison report, and diagnostics. If no candidate is buildable, the best rejected model is retained in diagnostics and clearly labeled unbuildable.

## Verification and documentation scope

Skill validation is intentionally static and Ubuntu-only in CI: discovery using `npx skills@1 add . --list`, exact ten-skill inventory validation, repository-vendored `quick_validate.py`, SVG/XML checks, shell syntax checks, and PowerShell parsing. It does not run expensive forward agent trials in CI.

The repository README will cover installation, runtime/setup behavior, the full catalog, natural-language examples, and the affiliation notice. Existing self-evaluation and CLAUDE guidance will be moved to the canonical root skill names.

