# Legolization skills suite — decision log

> Draft decision record through 2026-08-06. “Locked” means explicitly confirmed during the requirements discussion, not yet implemented.

## Locked product decisions

| Area | Decision |
| --- | --- |
| Release | Implement for `0.6.0`; skills require `legolization>=0.6.0`. Publishing that package to PyPI is a prerequisite but a separate scope. |
| Distribution | Root canonical `skills/` collection, installed with `npx skills add hbmartin/legolization --all`; CI pins `npx skills@1`. |
| Skill count | Exactly ten canonical skills: `legolize-model`, `prepare-lego-input`, `optimize-lego-build`, `publish-lego-instructions`, `analyze-lego-assembly`, `repair-lego-assembly`, `extend-lego-part-support`, `render-ldraw`, `inspect-instructions`, and `eval-corpus`. |
| Skill UX | Natural-language and explicit `$skill-name` invocation; implicit invocation allowed; plain-language guided flow; advanced controls only on request. |
| Branding | Directly drawn original multicolor geometric SVG glyphs; `#D92B2B` brand red; generic brick imagery only; include non-affiliation notice. |
| Runtime | Missing or old CLI auto-installs/upgrades through `uvx.sh`; installation failure stops the operation. |
| Setup scripts | Exactly two conceptual setup operations, each with shell and PowerShell variants: managed LDraw parts under `legolize-model`, renderer setup under `render-ldraw`. Other behavior belongs in CLI commands. |
| Platforms | macOS, Windows, and Linux are supported; renderer policy is LDView/macOS, LeoCAD via winget/Windows, LeoCAD + Xvfb/Ubuntu. |
| Legacy migration | Remove bare `legolization INPUT`; retain explicit `build`, `validate`, `cache`, `analyze`; remove named old compatibility scripts and `.claude/skills` duplicates without wrappers. |
| JSON protocol | New commands provide a one-envelope `--json` stdout contract, warnings/progress on stderr, including structured failures. |
| Bundle identity | Input content hash + effective config hash + Legolization version + catalog hash. Resume identity matches by default; ignore timestamps/output path. |
| Bundle lifecycle | Atomic stage manifest updates; numeric siblings prevent destructive overwrite; artifact drift regenerates in place; `--fresh` forces a sibling rerun. |
| Pending workers | Soft-deadline candidate workers continue detached and identity-stamped; later resume can adopt valid results. Provisional best completed work exits 3. `bundle --cancel-pending` terminates identity-matched workers, marks them cancelled, and retains completed valid candidate artifacts and diagnostics for later resume. |
| Input policy | Native `.vox`, `.npy`, `.obj`, `.stl`, `.ply`; LDraw `.ldr/.mpd`; no general converter. LDraw defaults to preserved assembly, with explicit `--retile` for generation. |
| Color policy | Sample embedded mesh color automatically; ask for uniform color only if no color data exists. Compare hard/no-dither, soft/no-dither, soft+dither automatically. |
| Quality | Fast: greedy seed 0/2 min. Balanced: ordinary full sweep seed 0 + eligible exact/15 min. Exhaustive: ordinary seeds 0/1/2 + eligible exact; user supplies duration. |
| Retry policy | After initial exit-2 result, a separate explicitly approved retry uses four-plate shell, six-plate shell, solid, sharing total budget fairly; never silently scales or drops components. |
| Rendering | `auto` omits unavailable booklets successfully; `required` makes unavailable/failed rendering partial; `off` intentionally omits booklets. Partial booklet receives missing-step markers. No renderer means no placeholders. |
| Instructions | Step-annotated MPD always; English-only booklets; locale-based Letter/A4 with Letter fallback; subassemblies and insertion audit always on; automatic density curve 3/5/7/10 parts per step. |
| Analysis/repair | Full diagnostics always; indeterminate results label topology-only recommendations unverified; repair is sibling-only and may redesign. |
| Catalog support | Extensions write a `-legolization-support` sibling; overlay activates after validation; upstream change requires confirmation. Explicit estimate sidecars may support fully validated physics with provenance but no safety adjustment. |
| Corpus | Package the corpus helpers; user-data storage for inputs; `./legolization-eval/` output default; mutation only through explicit baseline-write confirmation. |
| Validation | Ubuntu static workflow only: discovery/list, exact expected skill inventory, vendored validator, SVG/XML checks, Bash syntax, PowerShell parsing. No forward agent trials. |

## Still open

No product-policy decisions identified in this draft remain open. The next discussion should move to implementation sequencing and any newly discovered repository constraints.
