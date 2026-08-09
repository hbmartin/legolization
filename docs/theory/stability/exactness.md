# Exactness and certificates

**Source:** `stability/solver.py`, `stability/links.py` · StableLego (LP), Luo et al.
2015 (maximin), Kollsker & Malaguti + Whiting-style masonry localization (QP)
{ .provenance }

Three formulations over the same physics. One certifies, one ranks, one localizes.

---

## The LP

Assembled by hand rather than through cvxpy, because per-call canonicalization would
dwarf the solve for a loop that calls it dozens of times.

$$
\begin{aligned}
\min_{F,\,t,\,\text{dmax}}\quad & \sum_r t_r \;+\; \alpha \sum_i \text{dmax}_i \;+\; \beta \sum_j \text{drag}_j \\
\text{s.t.}\quad & A F + b \;\le\; t \\
& -A F - b \;\le\; t \\
& \text{drag}_j - \text{dmax}_{\text{owner}(j)} \;\le\; 0 \\
& F \ge 0,\quad t \ge 0,\quad \text{dmax} \ge 0
\end{aligned}
$$

with $\alpha = 10^{-3}$, $\beta = 10^{-6}$.

The two inequality blocks together implement $|AF + b| \le t$. Minimizing $\sum t_r$
drives the residual to zero where that is possible; where it is not, the residual
localizes the failure. The $\alpha$ term pushes the *worst* drag per brick down; the
$\beta$ term breaks ties toward using less total friction.

---

## Why the relaxation is exact

**Proved.**

Physically, a contact point cannot simultaneously press and pull. The honest model of
that is a complementarity constraint:

$$
\text{normal}_f \cdot \text{drag}_f = 0 \quad \forall f
$$

which is bilinear and would make this a MILP. The LP simply drops it. That sounds like
an optimistic relaxation — a solution could cheat by using both.

It cannot, and here is why.

!!! success "The argument"

    For each contact point $f$, the normal and drag columns of $A$ are **exact
    negatives of each other**:

    $$
    A_{:,\,\text{drag}_f} = -A_{:,\,\text{normal}_f}
    $$

    (They act at the same point, along the same axis, in opposite directions.)

    Take any feasible solution with $\text{normal}_f > 0$ and $\text{drag}_f > 0$. Let
    $m = \min(\text{normal}_f, \text{drag}_f) > 0$ and subtract $m$ from both.

    - The contribution to $AF$ is $\text{normal}_f \cdot A_{:,n} + \text{drag}_f \cdot A_{:,d}
      = (\text{normal}_f - \text{drag}_f)\, A_{:,n}$, which is **unchanged** by
      subtracting $m$ from both. So every equilibrium residual is identical, and $t$
      stays feasible.
    - Both variables remain non-negative, and $\text{dmax}$ constraints only relax.
    - The objective's $\beta \sum_j \text{drag}_j$ term **strictly decreases** by
      $\beta m > 0$.

    So any solution violating complementarity is strictly improvable. **No LP optimum
    violates it.** The relaxation is exact, not optimistic. $\blacksquare$

This is why the complementarity MILP is not a production mode. It exists as a private
small-instance oracle for equivalence tests, and its big-M ceilings
(`drag_big_m = 10 T`, `normal_big_m = 100`) are artificial numbers with no counterpart
in the papers that never constrain LP mode.

The argument depends on $\beta > 0$. With $\beta = 0$ the LP would have alternative
optima that violate complementarity while being no worse — same verdict, but a
solution you could not read as a force assignment.

---

## Solver stack

LP mode always goes through scipy `linprog` → HiGHS, with a documented retry chain:

```
("highs", None) → ("highs", {"presolve": False}) → ("highs-ipm", None)
```

because HiGHS presolve occasionally errors on degenerate instances. This is a real
failure mode, not defensive padding — see the
[environment notes](../../guides/performance-testing.md).

A direct `highspy.Highs` path solves the byte-identical polytope with its own attempt
chain `(choose, simplex) → (off, simplex) → (choose, ipm)`, used for large cold rescue
solves. It returns a `near_boundary` flag, and the caller re-solves through the
scipy-exact path whenever the verdict sits within $(1 \pm \mathrm{boundary\_margin})T$
of the drag threshold, or any residual is within a decade of its tolerance.

MILP mode uses cvxpy with HiGHS and pins `threads=1`, because HiGHS owns one
process-global scheduler and leaving cvxpy at auto poisons later one-thread warm
solves.

---

## Maximin: Luo's $C_M$

**Source:** Luo et al. 2015, eqs. 6–8 · `solver.solve_maximin`
{ .provenance }

The RBE score localizes but does not *order*. Two layouts can both be stable with the
same worst score; which is sturdier?

$$
\begin{aligned}
\max_{F,\,m}\quad & m \\
\text{s.t.}\quad & A F + b = 0 \qquad\text{(exact equilibrium, equality)} \\
& \text{drag}_j + m \le T \quad \forall j \\
& F \ge 0,\quad m \le T
\end{aligned}
$$

$m^\star = C_M$ is the extra force the weakest joint pair can still absorb. Higher is
sturdier.

Note what changed from the RBE LP: equilibrium is a **hard equality** here, not an
objective term. So this formulation *can* be infeasible — and LP status 2 (provably
infeasible) means no equilibrium exists at all, which is a collapse verdict.

| Property | RBE LP | Maximin |
| --- | --- | --- |
| Equilibrium | In the objective | Hard constraint |
| Always solves | ✅ | ❌ — infeasible means collapse |
| Per-brick localization | ✅ | ❌ |
| Strict ordering between layouts | ❌ | ✅ |

The two are complementary, and the `luo` strategy uses exactly that: maximin as its
acceptance criterion (a single strict order over candidates) paired with `analyze` for
failure seeds.

---

## The artificial-link QP

**Source:** Kollsker & Malaguti's QP; Whiting-style masonry localization adapted to
the RBE force model · `stability/links.py`
{ .provenance }

Repair needs a different answer again: not *is it stable*, not *which is sturdier*, but
**where must material be rearranged**.

Add one **free vertical shear** force per laterally touching brick pair — a force no
real LEGO connection could transmit — and demand exact equilibrium with real drags
bounded:

$$
\begin{aligned}
\min_{F,\,\lambda}\quad & \sum_k \lambda_k^2 \\
\text{s.t.}\quad & A F + A_{\text{link}} \lambda + b = 0 \\
& F \ge 0 \\
& \text{drag}_j \le T \quad \forall j
\end{aligned}
$$

$A_{\text{link}}$ gives each link a $+1$ on $f_z$ and moments $(r_y, -r_x)$ about each
endpoint's centroid, with opposite signs on the two bricks.

Reading the answer:

| $q = \sum \lambda_k^2$ | Meaning |
| --- | --- |
| $0$ | The structure stands on real forces alone |
| $> 0$ | Spread over exactly the links patching the deficit — **this is the localization** |
| $\infty$ (infeasible) | Unpatchable collapse |

The magnitudes $\lambda_k$ are what the ALNS destroy operator thresholds on: victims
are bricks touching a link with magnitude above $\max(\beta - \epsilon, 0)$ times the
strongest.

With no side contacts at all, the formulation degrades to a plain feasibility check —
there are no links to carry a deficit.

!!! important "The QP builds through the same physics"

    `links.py` constructs its model via `build_model_from_config`, not a private
    assembler. The QP must judge with the same physics the verdicts use; a localizer
    working from a different model would point at the wrong bricks.

Solved through cvxpy with `OSQP` then `CLARABEL`.

---

## Choosing between them

| You want to know | Use |
| --- | --- |
| Is this publishable? | RBE LP — it is the certifier |
| Which brick is about to fail? | RBE LP — `unstable_ids`, `weakest_pair`, per-brick scores |
| Is layout A sturdier than layout B? | Maximin $C_M$ |
| Where do I destroy and rebuild? | Artificial-link QP |
| Is this candidate worth a full solve? | [The screens](screens.md) |

Only the first publishes a verdict.
