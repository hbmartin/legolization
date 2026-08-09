# Papers and provenance

Thirteen references live in `references/`, each as converted Markdown alongside the
PDF. This page maps each to the code that implements it — and, more usefully, records
every place the implementation **deliberately differs** from the paper.

---

## The map

| Reference | Citation | Implemented in |
| --- | --- | --- |
| `legolization` | Luo, Yue, Huang, Chung, Imai, Nishita, Chen — *Legolization: Optimizing LEGO Designs*, SIGGRAPH Asia 2015 | [`luo` strategy](placement/merge-and-repair.md#luo), [merge engine](placement/merge-and-repair.md), [maximin $C_M$](stability/exactness.md#maximin-luos-c_m), side-contact corner equivalence, beam "path of best stability" |
| `stablelego-...` | Liu, Deng, Wang, Liu — *StableLego: Stability Analysis of Block Stacking Assembly*, RA-L 2024 | The entire [stability package](stability/index.md): [RBE model](stability/rbe.md), all constants, equilibrium-in-the-objective, the score→colour heatmap ramp, the `stablelego-parity` profile, and the [insertion press](stability/warm-solving.md#the-insertion-press) |
| `bricksim-...` | Wen, Liu, Piao, Li, Liu (CMU) — *BrickSim: A Physics-Based Simulator for Manipulating Interlocking Brick Assemblies* | [Reduced model](stability/screens.md#the-reduced-model) (parameterization only), [screening QP](stability/screens.md#the-screening-qp) (§VI-B **rejected**), [BrickSim research basis](stability/screens.md#the-bricksim-research-basis) (§VI-A faithful) |
| `models-and-algorithms-...` | Kollsker & Malaguti — *Models and algorithms for optimising two-dimensional LEGO constructions*, EJOR 289(1):270–284, 2021 | [`bond`](placement/constructive.md#bond), [`kollsker`](placement/exact.md#kollsker), [$h_3$ lookahead](placement/constructive.md#the-lookahead-function-hr), bond $\alpha_1/\alpha_2$, [ALNS repair](placement/merge-and-repair.md#alns-repair), [artificial-link QP](stability/exactness.md#the-artificial-link-qp) |
| `split-and-merge-...` | Lee, Kim & Myung — *Split-and-Merge-Based Genetic Algorithm (SM-GA) for LEGO Brick Sculpture Optimization*, IEEE Access 6, 2018 | [`smga`](placement/metaheuristics.md#smga), [`perpendicularity_error`](placement/index.md#perpendicularity_error) ($n_p$) |
| `legorization-from-silhouette-...` | Min, Park, Yang, Yun — *Legorization from silhouette-fitted voxelization*, KSII TIIS 12(6), 2018 | [`beauty`](placement/metaheuristics.md#beauty), [`symmetry_error`](placement/index.md#symmetry_error) ($g_a$, corrected to one global mirror plane; the paper's per-layer form survives as `layer_symmetry_error`), [`seam_priority`](placement/layered-engine.md#seam_priority), [`stackable_footprints`](placement/layered-engine.md#stackable_footprints) ($g_v$) |
| `streamlining-lego-model-design-...` | Bao, Zhang, Fan, Simeone — *Streamlining LEGO model design: an automated optimisation approach*, Procedia CIRP 126:945–950, 2024 | [`fast`](placement/metaheuristics.md#fast) |
| `planning-assembly-sequence-...` | Ma, Gong, Xu, Chen, Zhao, Huang, Zhou — *Planning Assembly Sequence with Graph Transformer* | [Blocking relations](sequencing.md#blocking-what-obstructs-an-insertion), [spatial continuity](sequencing.md#the-greedy-loop), [metrics](sequencing.md#metrics) |
| `reinforcing-lego-...` | Hsiao, Kong, Sy, Ruiz, Ureta (De La Salle) — *Reinforcing LEGO Generated Models by Applying Force Based Metrics* | Background for the force-based reinforcement loop (`greedy._reinforce`, `repair.py`). No direct 1:1 module. |
| `automatic-generation-of-vivid-...` | Zhou, Chen, Xu — *Automatic Generation of Vivid LEGO Architectural Sculptures*, CGF 2019 | Background; informs the [SNOT and slope finishing](placement/finishing.md) direction |
| `lambrecht-legovoxels` | Lambrecht — *Voxelization of boundary representations using oriented LEGO plates* (UC Berkeley CS284) | Background for the [oriented-plate treatment](pipeline.md#loading) and the 2.5-plates-per-stud pre-stretch |
| `generating-physically-stable-...` | Pun, Deng, Liu, Ramanan, Liu, Zhu (CMU) — BrickGPT / *Generating Physically Stable and Buildable Brick Structures from Text* | The lexicographic, hard-gate-first [selection discipline](../guide/quality-and-budgets.md#how-the-winner-is-chosen) |
| `building-lego-using-deep-generative-models-of-graphs` | Thompson, Ghalebi, DeVries, Taylor (Guelph/Vector) | The permutation analysis is adapted as the [drift validation harness](../reports/aesthetics-validation.md) (`scripts/aesthetics_drift.py`). **No generative model is implemented.** |
| *(not vendored)* | Dev — *Modeling Aesthetic Preferences in 3D Shapes: A Large-Scale Paired Comparison Study*, arXiv:2505.12373 (dataset unreleased; registry entry `shape-aesthetics-pairs`) | Methodology only: forced-choice pairs → Bradley-Terry latent scores → attribution onto interpretable terms, as the [preference program](../reports/aesthetics-validation.md) (`judge-aesthetics` skill, `scripts/fit_preference_weights.py`) |

Also present: `references/Summary of Mathematical Models and Algorithms for
Optimisation of the LEGO Construction Problem.md`, a distillation of Kollsker's 2020
DTU/LEGO industrial PhD thesis. It is the source for the friction and normal capacity
discussion, the max-flow/min-cut yield-line reading behind
[connector cuts](analysis-stack.md#load-paths-and-cuts), and the fix-and-optimise
matheuristic framing that scopes `kollsker` per layer.

---

## Deliberate deviations

The interesting part. Each of these was a decision, and each is recorded in the code
that makes it.

### `kollsker` omits $h_3$

The paper's lookahead guides **sequential commitment** — estimating what a remainder
will cost before committing to a placement. Simultaneous exact cover subsumes that
entirely: there is no remainder to estimate when all placements are chosen at once.

Adding it would be a heuristic bias on top of an exact solve.

### `beauty` is a beam search, not A*

The paper calls it A*. The implementation caps its OPEN list at 512 and uses a
non-admissible heuristic, so neither condition for A*'s optimality guarantee holds. The
module docstring says so rather than inheriting the paper's name for a different
algorithm.

### BrickSim §VI-B was measured and rejected

The paper's three-stage lexicographic relaxation was **ported first**. Its stage-2
exact pinning of every equilibrium row needs tens of thousands of ADMM iterations on
force-propagation chains; a single weighted QP — the certifier's own objective shape,
squared — solves the same fixtures in hundreds.

The rejected implementation survives as `bricksim_fields.py`, a faithful §VI-A research
basis, so the comparison remains reproducible.

### BrickSim's parameterization, not its constants

`reduced.py` ports the affine-field parameterization but keeps StableLego's constants.
`bricksim_fields.py` keeps BrickSim's own ($\mu = 0.2$, $\mu F_0 = 0.7$ N) and is
explicitly on a **different scale**.

Mixing constant sets between two papers' models would produce numbers belonging to
neither.

### Two errata in Ma et al.'s metrics

| Paper | Implementation |
| --- | --- |
| Kendall's $\tau$ denominator printed as $n(n-2)/2$ | Uses $n(n-1)/2$ — with distinct ranks, concordant + discordant $= n(n-1)/2$, so the printed form is an obvious typo |
| RLSD presented as a normalized measure | Documented that its normalizer caps a **full reversal at 2/3, not 1.0** |

### `bond` generalizes 1D rows to 2D

The paper's model is one-dimensional rows. The implementation applies it per-row along
each layer's scan axis — the natural 2D lift, documented as an adaptation rather than
presented as the paper's method.

### StableLego's three simplifications are corrected by default

The `corrected` profile enables yaw torque, the paper's knob rule, and
rotation-invariant contact patterns. Each was A/B tested before becoming default. The
`stablelego-parity` profile preserves the paper's behaviour exactly for reproduction.

The most consequential is `rotate_contact_pattern`: without it, the same physical
structure scores differently when built rotated 90° — measured at a 36% distortion on a
single-knob cantilever. See [the three corrections](stability/rbe.md#the-three-corrections).

### `fast` omits the full DFS

Bao et al.'s connectivity check is a full depth-first search per layer. The
implementation relies on the engine's connectivity repair instead, since duplicating
that work per layer would be wasted.

### No generative model

Two references cover learned approaches — BrickGPT and the graph generative model.
Neither is implemented. BrickGPT contributes its *selection discipline* (hard gate
first, then rank), not its model.

---

## Where the papers disagree with each other

Worth knowing, because the code has to pick:

| Question | Luo | Kollsker | StableLego | This project |
| --- | --- | --- | --- | --- |
| How is stability scored? | Maximin $C_M$ | Artificial-link deficit $q$ | Per-brick RBE score | **All three**, for [three different questions](stability/index.md#the-four-solvers) |
| Is yaw torque modelled? | Corner-point equivalent | — | No | **Yes**, by default |
| Optimize part count how? | Random merge | Exact set partitioning | — | Both, plus six others |
| What breaks a tie? | Random | Deterministic rank | — | Deterministic rank everywhere |

The "all three" row is the design: the papers built one scorer each because each was
answering one question. Certification, ranking, and localization genuinely are different
questions, and using one formulation for all three would compromise at least two of
them.

---

## Reading the sources

Converted Markdown lives at `references/<slug>/paper.md`, with the original PDF under
`papers PDFs/`. Conversion tooling is in `references/markdown_conversion/` and
`papers PDFs/pdf-to-markdown/`.

One conversion is text-only: BrickSim, because the repository's pdf-to-markdown tool
needs undeclared dependencies. Its figures are in the PDF.
