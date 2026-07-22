# Performance testing

How to measure this project's speed, how to prove a performance change,
and what counts as a regression. Written 2026-07-19 alongside the v4
rescue-LP work; the canonical worked example is the v3 item-1 campaign
(`ROADMAP.md`, "Item 1: sequencer LP performance").

The one rule that governs everything else: **correctness gates come
before any timing claim.** A perf change that shifts a golden byte, a
scorecard row, a dual-engine plan, or an unstable-step count is a
regression regardless of how much faster it is.

The development inner loop is intentionally bounded:
`uv run pytest` skips tests marked `slow`, and a bare
`scripts/eval_corpus.py` sweep selects synthetics only. Use
`uv run pytest --run-slow` for the full test suite and opt into mesh
evaluation explicitly with `--kind mesh`; CI uses the full-test flag.

## 1. Tools

### `scripts/profile_pipeline.py` (the primary tool)

```
uv run python scripts/profile_pipeline.py MODEL [--strategy greedy]
    [--seed 0] [--target-studs N] [--up x|y|z] [--label TEXT]
    [--out eval/profiles] [--cprofile] [--solid] [--no-repair]
    [--steps smart|layer]
```

`MODEL` is a file path or a corpus manifest name (`spot`, `suzanne`,
`letter-t`, ...). The command supervises one isolated child process
under `telemetry.record()` and writes
`eval/profiles/<UTC>-<name>-<strategy>.json` (schema 1):

- `git_sha` — the exact code state (read from `.git`, no subprocess);
- `host` — python/platform/cpu_count;
- `run` — model, input, strategy, seed, target_studs, hollow, repair,
  steps: the full input identity;
- `result` — brick_count, step_count, mass_g, stable, buildable: the
  **verdict block** every comparison must hold fixed;
- `total_seconds` and `spans` — per-span calls, seconds, and
  power-of-two `n` buckets.

Voxelization, layer placement, vertical compaction, connectivity repair,
stability analysis, and stability repair are watched separately. Every
stage receives a fresh 600-second watchdog; stage transitions print
immediately and the parent emits a heartbeat every 30 seconds. Override
these only for a deliberate fixture with `--stage-timeout SECONDS` and
`--heartbeat SECONDS`. On timeout the parent terminates the child and
atomically writes a durable JSON artifact containing `active_stage` and
all telemetry completed before the timeout.

`--cprofile` additionally writes a sibling `.pstats`. cProfile inflates
wall times; with it on, compare **call counts**, never seconds. Supervised
runs also execute watchdog checkpointing synchronously around spans, so
their timings are not strictly comparable with historical unsupervised
profiles even though a span's own start notification is excluded from its
measured duration.

### Armadillo release gate

Armadillo is explicitly outside every inner loop. Profile it on an idle
machine, one process and one strategy at a time, with layer-only
instructions:

```bash
for strategy in greedy luo bond fast smga beauty kollsker; do
  uv run python scripts/profile_pipeline.py armadillo \
    --strategy "$strategy" --steps layer \
    --stage-timeout 600 --heartbeat 30 \
    --label "armadillo-isolated-$strategy"
done
```

Do not parallelize this loop and do not add it to pytest or the default
corpus collection. A timed-out artifact is a valid diagnostic result:
its `active_stage` distinguishes voxelization, tiling, compaction,
connectivity, stability analysis, and stability repair. The stage
classification run remains the explicit offline release measurement in
`docs/v5-pending-measurements.md`.

The sequential idle-machine run on 2026-07-20 used seed 0, layer-only
instructions, and the 600-second per-stage watchdog. All seven children
produced timeout artifacts:

| strategy | timeout stage | voxelize (s) | tile (s) | compact (s) | connectivity (s) | completed stability-analysis spans (s) |
|---|---|---:|---:|---:|---:|---:|
| greedy | generic placement | 0.191 | — | — | 0.265 | 486.3 |
| luo | generic placement | 0.181 | — | — | 0.547 | 234.6 |
| bond | stability repair (second pass) | 0.188 | 1.567 | 0.003 | 9.166 | 1,269.1 |
| fast | stability repair | 0.187 | 0.921 | 0.001 | 5.124 | 628.8 |
| smga | stability repair | 0.182 | 20.754 | 0.002 | 7.460 | 567.5 |
| beauty | stability repair | 0.179 | 36.579 | 0.001 | 4.133 | 581.9 |
| kollsker | stability repair | 0.172 | 2.325 | 0.001 | 4.419 | 580.7 |

Leaf stability spans overlap their owning placement/repair span and are
therefore diagnostic totals, not additive wall time. Bond completed one
596.9-second repair pass before its second repair timed out. The result
rules out voxelization, layered tiling, compaction, and connectivity as
the old 900-second wall-time failure: legacy greedy/Luo exceed the cap in
placement-time stability scoring, while every layered strategy reaches
stability repair and spends its budget there.

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

- Prefix sequencing now indexes vertical and lateral knob contacts from
  `ConnectionGraph`, including rotated SNOT patterns, and remains warm.
  Disassembly still solves newly encountered contact components through
  the presolved cold analyzer; its component cache, rather than LP
  deletion, is the measured optimization.
- Telemetry does not cross placement sweep workers: `--strategy all` sweeps
  cannot be profiled; profile each strategy in its own supervised process.

## 8. Deadline and enumeration guardrails

Placement owns one absolute monotonic deadline. Layered tiling,
connectivity repair, and every bridge phase consume that same budget;
no nested synthesizer may start a fresh default timeout. Rectangle
enumeration is incremental and checks both the deadline and
`candidate_limit` while yielding, stopping at limit+1 so callers can
distinguish an exact in-budget list from overflow without
materializing the full search space.

The re-phased bridge ablation makes this especially important: phases
0, 1, and 2 share one deadline, cheap per-slab candidates are gathered
before flow escalation, and promising phases run first. Telemetry
records the attempted and accepted phases plus candidate/arc counts.
This holds on the default phase-0 path too (intentional, PR #22): a
per-slab cover that merely reduces the component count no longer
preempts flow escalation — the flow candidate competes on the same
(components, bricks) key, so the default path can spend bounded extra
MILP time to return a strictly better-connected bridge.
Do not raise the 600-candidate / 8_000-arc flow defaults from a single
partial result: mushroom already measured 2_322 candidates / 44_162
arcs at the larger envelope without an end-to-end win.

## 9. Pointers

- `docs/self-evaluation-playbook.md` — the wider quality loop;
  "seconds are noise" guidance.
- `docs/unstable-prefix-report.md` — the profiling campaign that
  identified LP solves as 99% of large-model runtime.
- `ROADMAP.md` v3/v4 progress notes — every measured claim with its
  commands.
