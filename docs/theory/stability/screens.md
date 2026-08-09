# Screens

**Source:** BrickSim (Wen, Liu, Piao, Li, Liu — CMU) · `stability/reduced.py`,
`stability/screen.py`, `stability/bricksim_fields.py`, `stability/incremental.py`
{ .provenance }

A screen answers "is this candidate confidently worse than the baseline?" cheaply
enough to be worth asking before a full solve. It never answers "is this stable?" —
that question belongs to the certifier.

---

## The reduced model

A full RBE has one variable per contact point per interaction. BrickSim's insight is
that the force distribution across a connection is smooth, so it can be represented by
a low-dimensional **affine field** instead of independent per-point magnitudes.

For each (brick pair, knob normal) *connection*, per-contact-point magnitudes become

$$
p(f) = \varphi_f \cdot \mathbf{c}, \qquad
\varphi_f = \big[\,1,\;\; u_f - \bar{u},\;\; v_f - \bar{v}\,\big]
$$

where $(u_f, v_f)$ are the contact point's in-plane coordinates and $\bar u, \bar v$
their means. Three coefficients for normal, three for drag — instead of two per point.

The four knob-press families collapse to one affine field per press direction, with an
**adaptive basis** sized to the geometry:

| Knobs in the connection | Basis | Width |
| --- | --- | ---: |
| 1 | constant | 1 |
| 2 | linear along their axis | 2 |
| 3+ | full $[1, u, v]$ | 3 |

A one-knob connection has no room for a gradient, so giving it three coefficients would
be pure numerical slack. Side contacts keep per-generator variables — there are few of
them and they are already minimal.

Lateral (SNOT) knobs get the same treatment with the field plane rotated onto the
mating plane: affine coordinates become `(transverse, vertical)` in stud units, and the
four shear generators take the press-field role.

---

## The restriction theorem

**Proved** (for the reduced formulation itself).

!!! success "The argument"

    The reduced model is built as a linear **restriction** of the exact model's
    variable space. An expansion matrix $E$ maps reduced coefficients to the exact
    model's per-point magnitudes, and the reduced system is constructed as

    $$
    A_{\text{reduced}} = A_{\text{exact}} \, E
    $$

    *by construction* — not by re-deriving the physics.

    Therefore any reduced-feasible solution $x$ with $Ex \ge 0$ expands to an
    exact-feasible force assignment $F = Ex$ with **the same equilibrium residual**:

    $$
    A_{\text{reduced}}\,x + b = A_{\text{exact}}\,(Ex) + b
    $$

    The reduced feasible set is thus a subset of the exact one. Under exact
    equilibrium, a reduced solve can therefore only **overestimate** the drag the
    exact LP needs — never underestimate it. $\blacksquare$

That direction is the safe one for a *reject* decision: if even the restricted model
says a candidate needs more friction than the baseline, the exact model will not
disagree by finding extra slack that the restriction hid.

### Where the theorem weakens

The screening QP does not enforce exact equilibrium — it penalizes the residual. That
soft term relaxes the theorem from a guarantee to a **strong tendency**.

Measured: `q` is typically 1–2× the exact score, with occasional small undershoots
from residual leakage. Either direction is safe in practice, because accepted
candidates are always cold-certified. The docstring says so plainly rather than
overclaiming.

### Two integrity checks

The reduced builder re-simulates the exact builder's variable counter to recover each
connection's exact column indices. Two guards catch drift between them:

1. A **total-count assertion** — "builder drift" if the counts disagree.
2. **Per-column drag attribution**, pinned against each `ContactPoint`'s own
   `(below_id, above_id)`.

The second exists because the first cannot catch a same-shape reordering: two builders
can agree on how many variables exist and disagree about which brick owns which.

---

## The screening QP

$$
\begin{aligned}
\min_{x,\,r,\,\text{dmax}}\quad & w_r \lVert r \rVert^2 + \alpha_q \lVert \text{dmax} \rVert^2 + \rho \lVert x \rVert^2 \\
\text{s.t.}\quad & A_{\text{scaled}}\, x - r = -b_{\text{scaled}} \\
& E_{\text{constraint}}\, x \ge 0 \\
& D_{\text{constraint}}\, x - M_{\text{dmax}}\, \text{dmax} \le 0 \\
& \text{dmax} \ge 0
\end{aligned}
$$

with $w_r = 1000$, $\alpha_q = 10^{-3}$, $\rho = 10^{-6}$.

### Why this shape, and not the paper's

BrickSim §VI-B solves a three-stage lexicographic relaxation instead. That scheme was
**ported first and measured**:

> Its stage-2 exact pinning of every equilibrium row ($Ax = y^\star$) needs **tens of
> thousands** of ADMM iterations on force-propagation chains, while this single
> weighted QP — the certifier's own objective shape, squared — solves the same
> fixtures in **hundreds**.

The paper's scheme is not wrong; it is badly conditioned for this solver on these
instances. Replacing it was a measured decision, and the rejected implementation is
still available as a research basis (below).

### Two conditioning requirements

Both were found the hard way and are baked in:

1. **Torque rows are scaled to force units** by $1/\kappa$ (`1/KNOB_PITCH_M`).
   Unscaled, whole overhang torques sit below the solver tolerance and go unbalanced —
   the screen would cheerfully approve a cantilever that cannot exist.
2. **Equilibrium residuals enter through explicit variables** $r$, not through the
   Gram matrix $A^\top A$, whose chain modes are numerically singular.

### OSQP specifics

- Solved **directly**, not through cvxpy — per-candidate canonicalization would dwarf
  the solve, the same reason the exact LP is hand-assembled.
- `polishing=False`, because OSQP 1.x prints polish diagnostics to stdout even with
  `verbose=False`.
- **Only `OSQP_SOLVED` counts.** `OSQP_SOLVED_INACCURATE` is treated as
  non-convergence, because its equilibrium leakage can exceed the per-brick tolerance
  and drive a *confident* rejection that then skips the cold solve. That is the one
  path where a screen bug could cost quality silently.

---

## Scoring and confidence

Scoring mirrors the certifier's `_score`, with a per-brick equilibrium tolerance that
adapts to the brick's own weight:

$$
\text{tol}_i = \max\big(0.05\,\lvert m_i g \rvert,\;\; 20\,\varepsilon_{\text{screen}}\big)
$$

$$
\text{score}_i =
\begin{cases}
1.0 & \text{out of equilibrium, or } \text{dmax}_i \ge T\\
\text{dmax}_i / T & \text{otherwise}
\end{cases}
$$

Two aggregates are kept:

| Aggregate | Definition | Why both |
| --- | --- | --- |
| `q` | $\max_i \text{score}_i$ | Clamped at 1.0 — matches the certifier |
| `q_raw` | $\max_i \text{dmax}_i / T$, **unclamped** | The clamped `q` cannot rank candidates whose baselines are already collapsing — which is ALNS repair's normal regime |

**Confidence:** `confident = False` whenever any brick's `dmax` lands within
$(1 \pm \mathrm{screen\_margin})\,T$ (default margin 0.1). Callers must then fall
through to the cold solve.

Statuses are `ok | declined | nonconverged | deadline | error`. Every non-`ok` status
is a cold-solve fallthrough. The screen swallows all build and solve exceptions and
carries the repr in `detail` — a screen that crashes must degrade to "solve it
properly", never to a verdict.

---

## The reject predicate

```text
reject  ⟺  status == "ok"  and  confident  and
           (  |unstable(cand)| > |unstable(base)|
              OR ( not lateral(either)
                   and |unstable| equal
                   and q_raw(cand) > q_raw(base)·(1 + margin) + margin ) )
```

Two details in that expression were each forced by measurement.

### The absolute headroom term

Note `+ margin` at the end — an **absolute** floor on top of the relative one.

Deep in the relaxed regime the screen's restriction noise dwarfs real differences
between candidates. A purely relative gate therefore confidently rejected genuine
improvements: **measured at 30% false rejects on shell-removal candidates, 0% with the
absolute floor.**

### The lateral scope guard

The stress-margin clause is scoped away from lateral (clad) layouts entirely.
Measured ranking accuracy:

| Layout kind | Correct rankings | Wrong rankings |
| --- | ---: | ---: |
| Vertical-only | 100% | 0% |
| Clad (lateral mates) | 90.5% | 5.6% |

On clad layouts cold `q` is an unrankable ground truth, so the screen declines to rank
on stress there and falls back to the unstable-count comparison alone.

---

## The BrickSim research basis

`screen_fields = "bricksim"` — **never the production default** — ports the paper's
§VI-A parameterization faithfully: per connection, **nine** coefficients (an axial
field plus two in-plane fields), plus a fourth family for a total of twelve.

That fourth family is the paper's co-located frictionless contact family $C$, folded
onto the same points so gravity has a compression path. Without it, a tension-only
axial field cannot support a stacked brick at all.

Linearized friction pyramid:

$$
\lvert F_t \rvert + F_a \;\le\; \mu\,(F_r + F_0), \qquad
\mu = 0.2,\;\; \mu F_0 = 0.7\ \text{N}
$$

$$
u_k = \max_f \frac{\lvert F_t \rvert + F_a}{\mu (F_r + F_0)}
$$

Solved with a per-connection relaxation variable playing the paper's stage-2 role, so
overloaded structures stay feasible and **grade** rather than failing.

!!! warning "Different scale, deliberately"

    The BrickSim score is on a different scale from `max_score`. `1.0` marks a
    friction-pyramid boundary under the *paper's* constants, not a `T_CAPACITY_N`
    drag. The two numbers are not comparable and the constants are deliberately not
    unified.

Documented simplifications: ground connections are treated like any snap-fit (so
`ground_pull` does not apply), and side contacts reuse the exact model's per-generator
compression columns.

---

## The frozen-boundary screen

A different screen entirely, for a different situation: the candidate is a *small
modification* of a layout you already solved.

`FrozenBoundaryAnalyzer` captures one full cold solve's `interface_forces`, then
screens a candidate by:

1. **`_changed_ids`** — records that differ, **or whose exact collision boxes differ**;
2. expanding two rings over the union of baseline and candidate contact adjacency;
3. **freezing** interface forces that *cross* the affected boundary.

Rejection is conservative and always returns a reason:

| Reason | Meaning |
| --- | --- |
| Candidate removes a frozen interface | The frozen force has nowhere to go |
| Baseline carries invalid force evidence | Nothing to freeze against |
| A frozen boundary already exhausts drag capacity | No headroom for the change |

### The interlock, in code

- `certify()` **always cold-solves survivors**.
- `accept()` **raises** if asked to advance a baseline without full cold certification.
- The only path that skips a cold solve is a *confident reduced-QP rejection*, which
  returns `cold_result=None` — treated by the caller as a failed candidate.

That is the mechanism behind the guarantee on the [Stability overview](index.md#the-certification-interlock).

---

## Baseline rebasing

One subtle correctness requirement in the `luo` strategy, worth stating because it is
the kind of bug that produces slowly-degrading quality rather than a crash.

When a candidate is accepted, the screen's baseline must be rebuilt from the new layout
**whenever the accepted candidate's report was not a clean `ok`**. Otherwise the screen
would go on ranking later candidates against a layout that no longer exists.

The type system helps here: `_screened_remerge` deliberately returns `report=None` so
the caller *must* cold-certify rather than being able to reuse a screened report as a
verdict.
