---
name: analyze-lego-assembly
description: >-
  Diagnose an LDraw assembly's stability, load paths, connectivity,
  floating parts, and assembly risks, producing a complete -analysis
  diagnostics bundle. Use when asked whether a model is stable or
  buildable, why it might collapse, which bricks are weak or floating,
  what load or scenario breaks it, or for a structural report before
  building.
license: GPL-3.0-or-later
---

# Analyze a brick assembly

Runs the physics and connectivity analysis on an `.ldr`/`.mpd` model and
writes an `INPUT-analysis/` sibling bundle: JSON reports, the connection
graph, per-component and floating-brick MPDs, an HTML diagnostic report,
and rendered views when a renderer is available. Diagnosis only — it
changes nothing and repairs nothing.

## Requirements

Needs `legolization` 0.6.0 or newer — check with `legolization --version`.
If it is missing or older, install the latest stable release with
`curl -LsSf https://uvx.sh/legolization/install.sh | sh`
(PowerShell: `irm https://uvx.sh/legolization/install.ps1 | iex`).
If installation fails, report the failure and stop.

## Conversation contract

- Explain the likely outcome in plain language before running anything.
- Ask only when a choice materially changes the result (an unusual
  support mode or load scenario) and always recommend one option; the
  defaults are right for "is this stable?".
- Run the CLI with `--json`; present the bundle path, verdict, warnings,
  and the next useful action (usually the repair-lego-assembly skill
  when problems were found).
- Report indeterminate results exactly as reported: when the analysis
  labels a recommendation **unverified** (topology-only, physics
  indeterminate), repeat that label verbatim — never launder it into
  confident advice.

## Workflow

1. Analyze without repair:

   ```sh
   legolization analyze MODEL.ldr --no-repair --json
   ```

   This writes the `MODEL-analysis/` sibling (numeric suffix if taken)
   with the JSON reports, graph, component and floating MPDs, HTML
   diagnostics, and renders when a renderer exists.

2. Read the envelope `data` and report in plain language: stable or
   not, worst joints and their scores, floating or disconnected bricks,
   load paths, and any per-scenario failures.

3. Present the bundle path and walk the user to the most useful
   artifact — the HTML report for browsing, the floating MPD for seeing
   exactly which bricks hang unsupported.

4. If problems were found, offer the repair-lego-assembly skill as the
   next action; do not attempt fixes from this skill.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts`, `warnings`, `data`, and `error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | analysis finished; model passed |
| 1 | error | operational error or invalid usage |
| 2 | unbuildable | the assembly fails physics as built |
| 3 | partial | indeterminate — treat recommendations as unverified |
| 130 | interrupted | resumable — rerun the same command to continue |

## Advanced controls (only on request)

- `--support MODE` (`auto`, `free`, `wheels`, `auto-ground`,
  `anchored-baseplate`, `selected:IDS`) and repeatable
  `--scenario` (`rest`, `lift-body`, `lift-chassis`, `front-torsion`,
  `rear-torsion`, `side-load`) probe specific handling loads;
  `--gravity-g`, `--side-load-g`, `--torsion-load-g` scale them.
- `--topology-only` skips mass/equilibrium work for a fast
  connectivity-only view; its recommendations are always unverified.
- `--path-between LEFT RIGHT` traces load paths between regions;
  `--catalog` / `--catalog-estimates` / `--connector-catalog` extend
  part knowledge, estimates carrying their recorded provenance.
