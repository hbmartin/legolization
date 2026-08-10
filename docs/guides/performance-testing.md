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
`legolization corpus collect` sweep selects synthetics only. Use
`uv run pytest --run-slow` for the full test suite and opt into mesh
evaluation explicitly with `--kind mesh`; CI uses the full-test flag.

## 1. Tools

### `scripts/profile_pipeline.py` (the primary tool)

```bash
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
`docs/reports/mesh-baseline-pending.md`.

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

### The v8 follow-up: where a cold analyze actually goes

Measured 2026-07-22 on hollow-shell greedy layouts (seed 0, default
`SolverConfig`), one cold `analyze` per row:

| bricks | LP variables | `build_model` | LP solve (`highs`) | `highs-ipm` | ipm no-crossover |
|---:|---:|---:|---:|---:|---:|
| 348 | 20,677 | — | 0.874 s | 1.905 s | 1.567 s |
| 505 | 30,434 | — | 2.830 s | 3.457 s | 3.006 s |
| 712 | 41,362 | — | 6.487 s | 6.631 s | 5.747 s |
| 902 | 52,911 | 0.12 s | 12.391 s | 10.244 s | 8.724 s |

Three conclusions, each closing a backlog hypothesis:

- **The LP solve is ~99% of a cold analyze.** Vectorizing `build_model`
  (the old backlog item) is a measured dead end: 0.12 s of a 12.5 s
  call at 902 bricks. The scipy wrapper is also not the cost — the
  direct highspy path solves the identical polytope in the same 13 s
  with zero per-brick score drift.
- **Cost scales ~n^2.8 in brick count.** Armadillo-class layouts
  (2,000+ bricks) pay minutes per full-structure solve, so any loop
  that re-solves the whole structure per candidate round (ALNS repair,
  Luo's split-remerge, hollow-restore) is unbounded in practice.
- **There is no cheap solver-level win.** `highs-ipm` is at best 1.4x
  with crossover off, and its solutions drift at 1e-9 in the
  objective — below the exactness bar for a default swap.

The v8 response is budget enforcement, not a faster LP:
`PipelineConfig.time_budget_s` now derives ONE absolute monotonic
deadline at `run()` start, honoured at round boundaries by ALNS
`repair_stability`, the hollow-restore loop, and Luo's `_stabilize`.
Repair and Luo also check it before their first full-structure solve,
and repair reuses the pipeline's existing exact verdict when no layout
change was accepted (telemetry: `repair.deadline_stop`,
`pipeline.hollow_restore.deadline_stop`,
`luo.stabilize.deadline_stop`). A running solve is never interrupted;
`time_budget_s=None` keeps every historical byte. The real per-solve
fix remains the incremental re-analysis workstream (warm append-only
scoring, frozen-boundary ring analysis with exact-on-accept), which
must clear the section-3 gates before it can ship.

### The reduced-QP screen: BrickSim port measurements (2026-08-07)

BrickSim (arXiv:2603.16853; `references/bricksim-*/paper.md`) collapses
per-contact-point force variables into affine per-interface fields.
That parameterization is ported over OUR force families and capacity
(`stability/reduced.py`, a provable restriction of the exact LP's
polytope) and solved as ONE OSQP QP mirroring the certifier objective
(`stability/screen.py`) — not the paper's three-stage lexicographic
scheme, whose stage-2 exact equality pinning measured 20_000+ ADMM
iterations on force-propagation chains. It ships as an opt-in
accept/reject screen (`SolverConfig.screen = "bricksim"`, default
"off" — every historical byte preserved) inside
`FrozenBoundaryAnalyzer.certify` and Luo `_stabilize`; accepted
candidates always cold-solve.

Measured by `scripts/benchmark_screen.py` (thin-shell greedy layouts,
seed 0, default `SolverConfig`; same-session cold `analyze` per row per
the section-2 protocol; artifact
`eval/profiles/20260807T175431Z-screen-bench.json`):

| bricks | cold analyze | screen total | screen build | ratio |
|---:|---:|---:|---:|---:|
| 378 | 2.69 s | 0.849 s* | 0.036 s | 0.316* |
| 622 | 13.35 s | 0.322 s | 0.061 s | 0.024 |
| 673 (collapsing) | 8.37 s | 2.007 s | 0.058 s | 0.240 |
| 1_172 | 51.54 s | 1.374 s | 0.149 s | **0.027** |

\* the 378-brick row absorbs first-call warmup; repeat runs measure
0.187 s / ratio 0.077. The collapsing 673-brick shell shows the QP's
honest worst case — a large active set still solves 4x faster than
cold.

Every pre-registered threshold passed: screen ≤ 1/10 of cold at the
largest shell (0.027), setup ≤ 30% of screen total (11%), candidate
pairwise-ranking agreement ≥ 95% (measured 100% over 305 pairs with
cold-q gaps above the margin), confident-false-reject ≤ 2% (0%, but
see the caveat below on which acceptance rule that measured),
stable/unstable verdict agreement ≥ 98% (100% across all shells plus
the 13 synthetic corpus models, both unstable rows correctly flagged),
OSQP nonconverged < 1% (0%).

Three conditioning requirements were measured, not assumed — all three
are load-bearing and documented in the modules:

- **Torque rows must be scaled to force units** (`1/KNOB_PITCH_M`):
  unscaled, a first-order solver "converges" with entire overhang
  torques unbalanced (their metric coefficients sit below its stopping
  tolerance) — the cantilever screen verdict was silently wrong.
- **The affine basis must be in stud units**: with meter offsets the
  field coefficients are O(1e3) and the ridge regularizer competes
  with the actual objective (a 26-brick tower stuck at a 3.7e-4
  false optimum).
- **Never form the Gram matrix `A^T A`**: force-propagation chains
  make it numerically singular; explicit residual variables keep the
  Hessian identity-conditioned (the BrickSim paper's own long-chain
  caveat, rediscovered at QP level).

Two honest caveats. Conservatism ("screen q >= exact q") is a strong
tendency, not a theorem — the soft equilibrium term permits small
undershoots (measured 0.090 vs 0.151 at 1_172 bricks); either
direction is safe because accepts are always cold-certified. And the
accept/reject gate needs an absolute margin floor on top of the
relative one (`should_reject`): a purely relative gate measured a 30%
confident-false-reject rate in the relaxed regime (candidate stress a
few percent of capacity, where restriction noise dwarfs real
differences); with the floor, 0%.

*Both false-reject figures above (30% and 0%) were measured with a
proxy for acceptance — "the cold solve says this candidate is stable
and scores better than the baseline" — not with a consumer's actual
rule. The three consumers disagree: Luo compares maximin capacity by
default, Luo under `acceptance="rbe"` compares
`(unstable count, min capacity)`, and ALNS repair compares the
localizer `q`. `benchmark_screen.py` now scores all three separately
per candidate domain and reports the worst as the gate number.
Re-baselined 2026-08-08 under the production rules: **0% for every
consumer in both domains** (artifact
`20260808T165542Z-screen-bench.json`; vertical ranking 100% over the
same run). One measurement lesson en route: the ALNS rule must carry
repair's own loop guard (`base.link_q > _Q_TOLERANCE`) — without it
the harness compares localizer q at noise level on *stable* baselines,
a regime `repair_stability` never enters, and misreports those noise
orderings as false rejects (measured 5.6-7.1% before the guard, 0%
with it).*

Enablement evidence (2026-08-07, full-pipeline A/B at seed 0, screen
off vs on): cantilever, staircase-overhang, mushroom, wide-arch, and
topple-arm all produce **identical outputs** in both arms. Four of the
five never engage the screen — ALNS repair only runs on unstable
initial placements — which is itself the correct dark-by-default
behaviour. Topple-arm is the engaged case: 3 confident rejects + 9
passes, 57 cold analyzes instead of 63, same final layout and verdict.
The `engine_cross_check=True` sequencing run over mushroom is
span-for-span identical between screen off and on (14
`stability.rescue.cross_check_mismatch` + 13
`stability.prefix.cross_check_mismatch` + 3 warm-fails in BOTH arms) —
the screen adds zero mismatches; those pre-existing mushroom mismatch
spans are independent of this work (plausibly the section-5
degenerate-alternative-optima drift class, but flagged for a look —
the historical proof runs on spot showed none). No
`stability.screen.nonconverged`/`.error` fired anywhere in the A/B or
the gate runs.

Enablement status: **dark by default.** The screen is recommended only
as an explicit opt-in (`[stability.solver] screen = "bricksim"`) for
`time_budget_s`-constrained runs on repair-heavy models. A default
flip would be an intentional output move (baseline + goldens
regenerated in the same commit) and is not currently planned.

### Screen follow-ups (2026-08-08): SNOT, adoption, hull vertices, fields

Four follow-ups landed together; measurements below.

**SNOT lateral support.** `build_reduced_model` no longer declines
lateral mates: the field plane rotates onto the mating plane
((transverse, vertical) coordinates in stud units, shear generators as
the press family, connections grouped by (pair, normal)). Structural
verdict agreement is 100% on the clad fixtures (letter-h +116 tiles:
screen q 0.017 vs cold 0.006; mushroom +173: 0.055 vs 0.043) and the
yaw 0/90/180/270 rotation equivalence is pinned. Two measured
consequences shipped with it:

- `screen_max_iter` default 4_000 → 20_000 — lateral-heavy layouts
  need ~5x the vertical-only iteration budget (the clad letter-h
  nonconverged at 4_000 and solves at ~20_000 in 0.7 s; converged
  solves stop early, so the cap only prices slow convergence).
- **The feather-light tie zone.** On damaged clad candidates the exact
  LP's own objective (`sum(t) + ALPHA·sum(dmax)`) can prefer leaving a
  sub-tolerance equilibrium residual on a feather-light SNOT part over
  paying drag for it — cold then flags bricks (q = 1.0) that the
  screen, which drives residuals to ~1e-7, reports balanced. Measured
  per domain (artifact `20260808T164332Z-screen-bench.json`): vertical
  candidate ranking stays at **100%**, clad-candidate ranking against
  cold q degrades to **75.8%** — the tie-zone q = 1.0 verdicts are not
  a rankable ordering ground truth. Response: **rank-rejection is
  scoped to vertical-only layouts** (`ScreenReport.lateral`;
  `should_reject` keeps only the unstable-count clause on clad
  layouts). Under that scoped gate the clad domain measures **0% false
  rejects** (4 gated, every one justified under both production
  rules). Binary confident-unstable rejection — what the tier pre-empt
  and the redesign gate use — stays available everywhere; the flip
  direction only ever costs a wasted cold certify.

**Adoption sites.** New opt-in consumers, all reject-only, verdicts
still cold: the redesign repair search pre-gates candidates before its
three exact solves (`failed_gate="screen"` + `screen_q` recorded in
the analyze artifact — additive, emitted only when the screen is on),
`final_remerge` compares like-for-like screened totals (the accepted
layout is still cold-certified: the screened arm deliberately returns
`RemergeResult(report=None)`), and `_snot_tiers` reverts a
confidently-unstable tier without its cold solve. Two
screen-independent reuse fixes landed alongside: ALNS localization
reuses certify's cold result (one fewer whole-structure solve per
accepted round), and `_remerge` reuses `final_remerge`'s accepted
report instead of re-solving. Measured NOT worth screening:
hollow-restore (no accept test), `_add_support`, redesign's envelope
baselines (need `interface_forces`), global-exact cuts (MILP
dominates), `_canonicalize_templates`/`_guarded_finish`.

**Hull-vertex constraint reduction — SHIPPED (gate passed).** The
affine fields attain their extrema at each connection's convex-hull
vertices, so pointwise nonnegativity and drag rows are constrained
only there (`ReducedModel.constraint_mask`, collinear-safe monotone
chain — scipy's ConvexHull raises QhullError on the common collinear
1xN interface). Noise-controlled in-process comparison (masked vs
full rows, alternating order, 3 reps per shell): **16.6% total
shell-series screen-time improvement** with 17.7% of rows dropped
(r=14: 1.16 → 0.83 s; the collapsing r=12: 3.54 → 3.01 s), against
the pre-registered ≥ 10% ship gate. Scores are exact by construction
(per-brick max drag is attained at a hull vertex); the masked-vs-full
equivalence test pins it.

**`screen_fields="bricksim"` (research flag) — studied, bar NOT met.**
The paper's own basis solves behind the flag: nine coefficients per
connection plus a co-located compression field (the paper's contact
family C — without it, tension-only axial fields cannot support a
stacked brick and everything flags unstable), linearized friction
pyramid under the paper's constants (module-local mu = 0.2,
mu·F0 = 0.7 N — NOT `T_CAPACITY_N`), utilization scoring on its own
scale. Study run (`--fields bricksim`, shells r=8/10 + corpus + clad;
artifact `20260808T161914Z-screen-bench.json`): stable/unstable
**verdict agreement 100%** — the physics is sound. A re-run under the
restricted protocol with the lateral fix in place (`--fields bricksim
--radii 8 10 --candidates 30 --skip-corpus --seed 0`; artifact
`20260808T204443Z-screen-bench.json`) supersedes that first run's
headline figures: candidate ranking **100% vertical / 81.4% SNOT**,
worst-consumer false rejects **0% in both domains**. What still fails
the pre-registered bar ("beat restricted on agreement at comparable
speed"): per-brick score correlation near zero (utilization and drag
stress order bricks differently), a 1.03% non-converged share, and
slightly slower solves. The restricted basis remains the production
screen; the flag stays research-only and earns no further work.

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
- any corpus scorecard row change that `legolization corpus assemble`
  classifies as
  hard (buildable-count drop, expectation failure, winner objective
  worsening beyond tolerance);
- a dual-engine plan test diff (`tests/test_prefix_solver.py`);
- a changed `result` verdict block on any pinned profile input;
- unstable-step counts changing in `legolization instructions audit`
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
equality tests; `verify_plan`/`legolization instructions audit` clean on the proof
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

- `docs/guides/self-evaluation-playbook.md` — the wider quality loop;
  "seconds are noise" guidance.
- `docs/reports/unstable-prefix-report.md` — the profiling campaign that
  identified LP solves as 99% of large-model runtime.
- `ROADMAP.md` v3/v4 progress notes — every measured claim with its
  commands.
