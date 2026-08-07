---
name: extend-lego-part-support
description: >-
  Research, estimate, validate, and activate catalog support for a part
  the generator does not know yet. Use when a build fails on an
  unsupported or unknown part, when asked to add or extend part
  support, infer geometry and mass estimates for an LDraw part number,
  validate a catalog extension, or activate a -legolization-support
  bundle.
license: GPL-3.0-or-later
---

# Extend part support

Builds a reviewable `KEY-legolization-support/` bundle for an LDraw part
the catalog does not cover: inferred draft geometry, mass estimates with
recorded sources, and validation results. The extension becomes usable
only after every validation gate passes, and only by explicitly passing
the bundle to other commands — nothing activates silently.

## Requirements

Needs `legolization` 0.6.0 or newer — check with `legolization --version`.
If it is missing or older, install the latest stable release with
`uv tool install legolization@latest`
(or `pip install --upgrade legolization`). Only if neither package
manager is available, fall back to the standalone installer:
`curl -LsSf https://uvx.sh/legolization/install.sh | sh`
(PowerShell: `irm https://uvx.sh/legolization/install.ps1 | iex`).
If installation fails, report the failure and stop.

## Conversation contract

- Explain the likely outcome in plain language before running anything.
- Ask only when a choice materially changes the result and always
  recommend one option.
- Run the CLI with `--json`; present the support-bundle path, verdict,
  warnings, and the next useful action.
- Estimates are estimates: they carry recorded provenance and receive
  no hidden safety adjustment — present them with their sources, never
  as certified figures.
- Changing the project's upstream `catalog.json` requires the user's
  explicit confirmation in this conversation; the default deliverable
  is the sidecar support bundle, not an upstream edit.

## Workflow

1. Infer a draft for the part number:

   ```sh
   legolization catalog infer PART_ID --json
   ```

   Optional: `--key KEY` names the catalog key (default `part_ID`),
   `-o DIR` picks the parent directory (default: the current one), and
   `--offline` skips keyed/network sources while still consulting the
   local dump. This writes the `KEY-legolization-support/` directory
   with draft geometry, `sources.json`, and `draft-estimates.json`.

2. Review the draft with the user before validating: open
   `sources.json` (where each figure came from) and
   `draft-estimates.json` (the estimated mass/geometry values) and
   summarize both. Flag anything with weak or missing provenance.

3. Validate the bundle:

   ```sh
   legolization catalog validate KEY-legolization-support --json
   ```

   Exit 0 means every gate passed; exit 3 means the draft exists but
   some gate failed — report each failure and what would fix it.

4. Activate only after validation passes, by passing the bundle
   explicitly where it is needed:

   ```sh
   legolization bundle IN --catalog KEY-legolization-support --json
   ```

   The same `--catalog` flag works for `build`, `analyze`, and repair
   runs. Never suggest activation while any gate is failing.

5. If the user wants the extension promoted into the project's upstream
   `catalog.json`, confirm that explicitly first, then apply and rerun
   validation.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts`, `warnings`, `data`, and `error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | inference or validation passed every gate |
| 1 | error | part not found, or operational error |
| 3 | partial | draft written but validation gates failed — review and fix |

## Advanced controls (only on request)

- `--catalog-estimates PATH` supplies a separate estimate sidecar with
  labeled provenance to physics-consuming commands.
- Multiple `--catalog` flags stack extensions for a single run.
