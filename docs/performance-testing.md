# Performance testing

How to measure this project's speed, how to prove a performance change,
and what counts as a regression. Written 2026-07-19 alongside the v4
rescue-LP work; the canonical worked example is the v3 item-1 campaign
(`ROADMAP.md`, "Item 1: sequencer LP performance").

The one rule that governs everything else: **correctness gates come
before any timing claim.** A perf change that shifts a golden byte, a
scorecard row, a dual-engine plan, or an unstable-step count is a
regression regardless of how much faster it is.

## 1. Tools

### `scripts/profile_pipeline.py` (the primary tool)

```
uv run python scripts/profile_pipeline.py MODEL [--strategy greedy]
    [--seed 0] [--target-studs N] [--up x|y|z] [--label TEXT]
    [--out eval/profiles] [--cprofile] [--solid] [--no-repair]
    [--steps smart|layer]
```

`MODEL` is a file path or a corpus manifest name (`spot`, `suzanne`,
`letter-t`, ...). Runs the pipeline in-process under
`telemetry.record()` and writes
`eval/profiles/<UTC>-<name>-<strategy>.json` (schema 1):

- `git_sha` — the exact code state (read from `.git`, no subprocess);
- `host` — python/platform/cpu_count;
- `run` — model, input, strategy, seed, target_studs, hollow, repair,
  steps: the full input identity;
- `result` — brick_count, step_count, mass_g, stable, buildable: the
  **verdict block** every comparison must hold fixed;
- `total_seconds` and `spans` — per-span calls, seconds, and
  power-of-two `n` buckets.

`--cprofile` additionally writes a sibling `.pstats`. cProfile inflates
wall times; with it on, compare **call counts**, never seconds.

### `legolization ... --profile out.json` (CLI convenience)

Writes a leaner schema-2 payload (`source: "cli"`, `git_sha`, input,
strategy, seed, brick/step counts, total, spans). Rejected for
`--strategy all` (telemetry cannot cross spawn workers) and for
`.ldr`/`.mpd` inputs (import skips the profiled phases). Cross-producer
comparisons (script vs CLI artifacts) use span call counts only.

### The telemetry API (`src/legolization/telemetry.py`)

Ambient span recording: `with telemetry.record() as session:` activates
it; instrumented sites (`with telemetry.span("stability.lp", n=bricks):`)
accumulate calls + wall seconds; outside `record()` every span is a
shared no-op costing one `ContextVar.get`. Spans deliberately overlap
(`stability.analyze` contains `stability.build_model` and
`stability.lp`); attribute by family leaf, not by sum. `n` buckets by
power of two so seconds-vs-size scaling reads from one run.
`test_recording_never_changes_behaviour` pins that recording never
alters placements — keep it green when adding spans.

## 2. The before/after protocol

1. **Fresh before-run at the branch point.** Never reuse a historical
   JSON as the timing baseline — wall clocks are not comparable across
   sessions, machines, thermal states, or background load (the v4
   branch-point suzanne re-run measured 37.7 s where the v3 pin said
   30.3 s for identical code and config). Historical pins are the
   *structural* reference: call counts, span shapes, verdict blocks.
2. Pinned inputs, seed 0: `pyramid.npy` (clean greedy path), `suzanne
   --target-studs 16` (mid-size mesh), `spot --target-studs 24` (the
   rescue-heavy stress case). Label them (`--label v4-before`).
3. **After each perf commit**, re-run the identical commands
   back-to-back on the same machine in the same session.
4. Compare, in order: (a) the `result` verdict block — must be
   identical; (b) span **call counts** — e.g. cold `stability.analyze`
   calls migrating to a warm/direct span is the mechanism evidence;
   (c) `total_seconds` and the per-span seconds — the speedup claim;
   (d) new fallback spans — each firing on the proof set must be
   explained, not averaged away.
5. Record the numbers in the ROADMAP progress entry with the exact
   commands (the v3 item-1 table is the format to copy).

## 3. What counts as a regression

Hard failures (any one blocks the change):

- any golden `.ldr` byte diff (`tests/test_examples_regression.py`);
- any corpus scorecard row change that `eval_corpus` classifies as
  hard (buildable-count drop, expectation failure, winner objective
  worsening beyond tolerance);
- a dual-engine plan test diff (`tests/test_prefix_solver.py`);
- a changed `result` verdict block on any pinned profile input;
- unstable-step counts changing in `scripts/check_instructions.py`
  output on spot/suzanne/mushroom/heart.

Soft signals to investigate:

- >5% same-session wall regression on a pinned model;
- call-count growth of `stability.analyze` / `stability.lp` on pinned
  inputs (an optimization quietly disabled);
- fallback spans (`*.warm_fail`, `*.boundary_fallback`,
  `*.cold_fallback`) firing where the proof runs showed none.

## 4. Correctness gates for any perf change

Every perf commit runs the full standard gates (ruff, pytest, ty,
pyrefly, lizard CCN 18) **plus**: goldens byte-identical; `eval-corpus`
synthetic scorecard vs the committed baseline; dual-engine plan
equality tests; `verify_plan`/`check_instructions` clean on the proof
models; and at least one `engine_cross_check=True` run over a rescue-
heavy model with zero mismatch spans. Optimizations must be
verdict-preserving with a cold fallback: near-threshold results
re-solve cold (the boundary guard), and any non-optimal warm solve
falls back to the legacy chain.

## 5. Engine and drift policy

- The LP polytope is identical across engines; scipy is the legacy
  bit-for-bit reference (`SolverConfig(engine="scipy")`).
- Byte-identical plans are guaranteed wherever the greedy path runs and
  for small rescue components below the direct-solve size gate; rescued
  plans above the gate are **verdict-equivalent** — equal stability
  verdicts and unstable-step counts, with solver-tolerance-level score
  drift on degenerate alternative optima (the same drift class scipy
  exhibits across its own versions).
- Presolve stays **off** on persistent warm models (required for basis
  reuse) and **on** for one-shot cold solves (scipy's default; the
  `_LP_ATTEMPTS` chain retries presolve-off then IPM on the known
  degenerate-presolve failures).

## 6. Measured dead ends (do not re-try without new evidence)

- **LP-deletion warm starts**: deleting rows/cols from a HiGHS model
  discards enough basis that re-solves are effectively cold (measured
  49-63 warm fails, no speedup). The rescue's win came from the
  floating shortcut + component-verdict caching instead.
- **Bound-deactivation warm rescue** (v4, mechanism built, measured,
  reverted): keep one persistent model of the rescue scope and "remove"
  chunks by fixing their columns to zero and relaxing their rows —
  basis dimensions preserved, dual simplex hot-starts, textbook
  branch-and-bound pattern. Correctness was perfect (tower-walk drift
  ~1e-18, clean fallback on the one warm_fail), but the economics
  lose: the persistent model cannot presolve (basis reuse forbids it),
  while one-shot cold solves presolve the RBE down dramatically.
  Measured on spot@24: warm re-solves ~23 s each vs ~5.6 s cold-direct
  at n≈1000, plus a 45 s scope build; totals 588 s warm vs 490 s cold
  (suzanne 46 s vs 32 s). **Presolve beats basis reuse on this LP
  family** — any future warm-rescue idea must beat the presolved cold
  solve, not the unpresolved one.
- **Candidate pruning in `_choose_removal`**: the dominant
  grounded-stable rescue state already costs exactly one LP (first
  stable candidate short-circuits); there is no fan-out to prune.
- **Parallel rescue solves**: one LP per state on the dominant path
  leaves nothing to run concurrently.

## 7. Known gaps

- The warm prefix/removal solvers decline SNOT layouts
  (`stability/prefix.py::_has_lateral_parts`): their contact discovery
  is z-up-only, so `--snot` models sequence on the cold engine.
  Revisit when SNOT contact semantics stabilize; the extension needs
  dual-engine equivalence tests mirroring `tests/test_prefix_solver.py`.
- Telemetry does not cross spawn workers: `--strategy all` sweeps
  cannot be profiled; profile single strategies in-process.

## 8. Pointers

- `docs/self-evaluation-playbook.md` — the wider quality loop;
  "seconds are noise" guidance.
- `docs/unstable-prefix-report.md` — the profiling campaign that
  identified LP solves as 99% of large-model runtime.
- `ROADMAP.md` v3/v4 progress notes — every measured claim with its
  commands.
