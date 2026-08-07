---
name: optimize-lego-build
description: >-
  Improve an existing LDraw assembly (.ldr or .mpd): validate and
  re-publish it exactly as built, or retile it into regenerated
  brickwork that keeps its shape and colours. Use when asked to
  optimize, rebuild, retile, strengthen, or reduce the part count of an
  existing brick model, compare candidate placements for it, or improve
  a build without touching the original file.
license: GPL-3.0-or-later
---

# Optimize an existing brick assembly

Takes an LDraw assembly and either keeps every brick placement
authoritative (preserve) or treats the assembly as a coloured shape
target and regenerates its brickwork (`--retile`), publishing the result
to a sibling bundle. The source file is never overwritten.

## Requirements

Needs `legolization` 0.6.0 or newer — check with `legolization --version`.
If it is missing or older, install the latest stable release with
`curl -LsSf https://uvx.sh/legolization/install.sh | sh`
(PowerShell: `irm https://uvx.sh/legolization/install.ps1 | iex`).
If installation fails, report the failure and stop.

## Conversation contract

- Explain the likely outcome in plain language before running anything.
- The one material question is shape authority. Ask it with a
  recommendation: keep the exact brickwork and just validate/publish it
  (**preserve**, the default), or let the optimizer replace the bricks
  while keeping shape and colours (**--retile** — recommend this when
  the user asked to optimize, strengthen, or shrink the build).
- Run the CLI with `--json`; present the bundle path, winner, warnings,
  and the next useful action.
- Rerunning the same command resumes identity-matched incomplete work by
  default; use `--fresh` only when the user asks for a fresh rerun.

## Workflow

1. Settle shape authority (above), then run one of:

   ```sh
   legolization bundle ASSEMBLY.ldr --json            # preserve as built
   legolization bundle ASSEMBLY.ldr --retile --json   # regenerate bricks
   ```

   Preserve writes an `ASSEMBLY-instructions/` sibling (validation,
   analysis, and instructions for the assembly exactly as built).
   `--retile` writes `ASSEMBLY-optimized/` with the regenerated winner.

2. Quality for retile runs follows the standard tiers: `--quality
   balanced` (default, recommended), `fast` for a quick preview, or
   `exhaustive` which requires the user to choose `--duration SECONDS`.

3. Read `comparison/report.json` in the bundle and present the winner
   and the recorded reason it won, plus a one-line verdict per candidate
   (strategy, buildable or not, brick count, objective).

4. Compare against the original honestly: if the retiled winner is not
   actually better than the imported assembly (more bricks, weaker),
   say so — the source is untouched and losing a comparison is a valid,
   useful answer.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts`, `warnings`, `data`, and `error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | bundle finished; result published to the sibling |
| 1 | error | operational error or invalid usage |
| 2 | unbuildable | physics rejected every candidate; best rejected kept in diagnostics |
| 3 | partial | provisional best at deadline; resuming may adopt a better result |
| 4 | error | exact-placement limit hit |
| 130 | interrupted | resumable — rerun the same command to continue |

## Advanced controls (only on request)

- `-o DIR` overrides the sibling; `--render auto|required|off` controls
  booklet rendering; `--set KEY=VALUE` tunes generation (e.g.
  `--set geometry.hollow=false`).
- `--catalog PATH` / `--catalog-estimates PATH` bring extended-part
  support into validation and regeneration.
- On exit 2, the retry material ladder (`--retry-materials --duration N`)
  is available with the user's consent and budget.
