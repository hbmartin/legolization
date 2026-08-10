# Choosing a strategy

A *placement strategy* decides which bricks cover which cells. Eight are available.
They solve the same problem with very different trade-offs between part count, seam
quality, structural soundness, and time.

!!! tip "You probably do not need to choose"

    `bundle --quality balanced` runs all seven heuristic strategies plus global exact
    when it qualifies, gates them on buildability, and publishes the winner on
    evidence. Reach for a specific strategy when you are benchmarking, reproducing a
    result, or you know something the objective does not.

---

## At a glance

| Strategy | Optimizes for | Cost | Reach for it when |
| --- | --- | --- | --- |
| [`greedy`](../theory/placement/constructive.md#greedy) | Part count with good seam staggering, then reinforces weak spots | Low | You want one fast, sturdy answer |
| [`bond`](../theory/placement/constructive.md#bond) | Brick-bonding quality — running-bond seams | Low | Walls and slabs; the default fallback |
| [`fast`](../theory/placement/metaheuristics.md#fast) | Big bricks, perpendicular to their supports | Low | Speed on large models |
| [`luo`](../theory/placement/merge-and-repair.md#luo) | Structural soundness under the maximin criterion | Medium–high | Fragile shapes; supports soft colour |
| [`smga`](../theory/placement/metaheuristics.md#smga) | Part count with connectivity, via a genetic algorithm | High | Awkward layers where greedy tiling stalls |
| [`beauty`](../theory/placement/metaheuristics.md#beauty) | Symmetry, balance, and stability priority | High | Display models where looks matter |
| [`kollsker`](../theory/placement/exact.md#kollsker) | **Provably minimal parts per layer**, then stagger | High | Minimum part count matters most |
| [`global-exact`](../theory/placement/exact.md#global-exact) | **Provably optimal whole model** under its objective | Very high, small models only | Small models where you want the true optimum |

`auto` (the default) picks `global-exact` when the model has at most
`placement.exact.max_cells` filled cells (256), and `placement.exact.fallback_strategy`
(`bond`) otherwise.

---

## The objective they are ranked by

Whatever a strategy optimizes internally, candidates are compared with one weighted
sum. Lower is better, and every term is normalized to roughly `[0, 1]`:

$$
J = w_{\text{cost}}\frac{|\text{parts}|}{|\text{filled voxels}|}
  + w_{\text{stab}}\,\mathrm{max\_score}
  + w_{\text{aes}}\,\mathrm{seam\_alignment}
  + w_{\text{col}}\,\mathrm{colour\_mismatch}
  + w_{\text{perp}}\,\mathrm{perp\_error}
  + w_{\text{sym}}\,\mathrm{sym\_error}
$$

Default weights put stability first by a wide margin:

| Term | Weight |
| --- | ---: |
| `stability` | 4.0 |
| `cost` | 1.0 |
| `colour` | 1.0 |
| `aesthetics` (seam alignment) | 0.5 |
| `symmetry` (global mirror plane) | 0.25 |
| `perpendicularity` | 0.0 (reported only) |
| `speckle`, `profile` | 0.0 (audition, reported only) |

Tune them under [`[placement.weights]`](configuration.md#placementweights). If your
eye disagrees with the objective's winner, that is a finding about the weights worth
reporting — not necessarily a bug in the strategy.

The derivation of each term is in [Placement](../theory/placement/index.md).

---

## Two things every strategy respects

**Bonding.** Aligned vertical seams are the structural failure mode of brick
construction — a stack of bricks whose joints line up is a crack waiting to happen.
Every strategy penalizes a seam that continues from the layer below, most of them via
a distance-decayed term `α₁·exp(−α₂·d)` where `d` is the stud distance to the nearest
seam underneath.

**Colour compatibility.** Two bricks may only merge if their colours are compatible.
Under `hard` colour mode that means identical (or one is a colour-free interior
wildcard). Under `soft`, a merge that miscolours a few cells is accepted
probabilistically — fewer, larger bricks at the price of some wrong colour at
boundaries.

---

## Choosing by symptom

| Symptom | Try |
| --- | --- |
| Everything is unbuildable | `--retry-materials` first — this is usually a material problem, not a strategy problem |
| Unbuildable on a spanning shape (arch, bridge) | `luo` — its acceptance criterion is structural, not cosmetic |
| Too many parts | `kollsker` for a per-layer minimum, or `global-exact` if the model is small enough |
| Visible stacked seams | `bond`, or raise `placement.weights.aesthetics` |
| Model looks lopsided | `beauty` with `--set placement.beauty_preset=aesthetics` |
| Colours fragment the brickwork into 1×1s | `--set placement.colour_mode=soft` |
| Just too slow | `--quality fast`, or `--quality direct` with `greedy` |

```sh
legolization bundle model.obj --quality direct --set placement.strategy=luo
legolization bundle model.obj --quality direct \
  --set placement.strategy=beauty --set placement.beauty_preset=stability
legolization build model.vox -o out.ldr --strategy kollsker
```

---

## Strategy-specific knobs

| Strategy | Configuration key | Default |
| --- | --- | --- |
| `smga` | `placement.ga_generations` | `200` |
| `smga`, `beauty`, `kollsker` | `placement.time_budget_s` | `null` (unbounded) |
| `beauty` | `placement.beauty_preset` — `balanced`, `stability`, `aesthetics`, `efficiency` | `balanced` |
| `luo` | `placement.colour_mode`, `placement.colour_weight` | `hard`, `1.0` |
| `global-exact` | everything under `placement.exact` | see [reference](configuration.md#placementexact) |

The heavier searches (`smga`, `beauty`, `kollsker`) are the ones that will time out
first on a large model. If only those are timing out, `placement.time_budget_s` is
the knob.

---

## What happens after placement

No strategy's output ships as-is. Every layout then goes through:

1. **Physics** — the RBE scores every brick. See [Stability](../theory/stability/index.md).
2. **Repair** — unstable layouts get an ALNS destroy-and-repair pass that rearranges
   bricks at *constant volume*.
3. **Hollow restore** — only if repair failed, material is added back around the
   trouble spots and placement re-runs.
4. **Re-merge and finishing** — a final merge pass, then slopes, tiles, and
   optionally SNOT cladding.

Each of those is guarded: a pass that would flip a stable layout to unstable is
reverted wholesale. So a strategy's job is to produce a good *starting point*, not a
final answer — which is part of why the differences between them are smaller in the
finished model than their descriptions suggest.

Full treatment: [The pipeline](../theory/pipeline.md).
