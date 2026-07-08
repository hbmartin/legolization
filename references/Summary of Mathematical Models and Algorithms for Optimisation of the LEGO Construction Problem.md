Consider Torkil Kollsker's 2020 PhD thesis from the Technical University of Denmark (DTU), an *industrial* PhD done jointly with **LEGO System A/S**. It's 253 pages, and it tackles what the literature calls the **LEGO construction problem**: given a 3D shape and a set of available bricks, find a placement of bricks that reproduces the shape while being cheap, stable, and well-built. Here's the distilled version.

## The problem

The workflow it targets has four steps — **Design → Convert to 1×1 bricks → Generate building instructions → Assemble** — with a feedback loop when a construction turns out infeasible (disconnected bricks, collapse, or unfillable geometry). The thesis focuses on the third step, which is where the hard combinatorial + structural decisions live.

The object is **voxelised** (discretised into unit cubes), and three quality objectives define a "good" construction:

- **Cost** — minimise the number of bricks (saves money and, more importantly, assembly time).
- **Structural integrity** — the construction must stay connected and withstand forces.
- **Aesthetics** — quantified via *brick-bonding rules* (systematic staggering of bricks, like real bricklaying), since the shape/colour are already fixed by the input.

Two things the thesis explicitly broadens versus prior work (which mostly dates from Gower et al. 1998 onward): it allows **bricks of multiple heights** (plates, bricks, DUPLO — 3 plates = 1 brick, DUPLO = 2× brick), and it drops the common assumption that a **1×1 brick is always available**.

## Structural integrity (Chapter 2)

This is the heart of the "and such." Four methods are compared — penalty method, finite element method, static limit analysis, and yield-line analysis — with **static limit analysis solved via quadratic programming** identified as the most promising.

**Penalty method** (fast, used by most prior metaheuristics): a weighted sum of soft penalties — perpendicularity of neighbouring bricks, number of connections, uncovered gaps, and horizontal alignment. The alignment penalty for a brick offset $x$ in a layer of length $l$ is roughly

$$\frac{|x - l/2|}{l/2}$$

The thesis criticises these because they don't reflect true physical stability.

**Static limit analysis** models forces flowing between bricks and asks whether a valid distribution exists. For each brick $b$ the model enforces **force balance** and **moment (torque) balance**:

$$\sum_{i\in F_b^-}\vec{F}*i - \sum*{i\in F_b^+}\vec{F}*i = m_b,\vec{g} \qquad \sum*{i\in F_b^-}\vec{L}_i\times\vec{F}*i - \sum*{i\in F_b^+}\vec{L}_i\times\vec{F}_i = \vec{0}$$

plus a **Coulomb friction cone** $F_f \le \mu_s F_s$ and per-connection capacities (friction capacity $T = 0.625,\text{N}$, normal-force capacity $T = 5440,\text{N}$). This is cast as a **max-flow / min-cut** problem:

- *Dead load* (weight only), model (2.19)–(2.25): find the minimal remaining capacity $\hat{C}_M$ across all connections. If $\hat{C}_M < 0$, the structure collapses.
- *Live load* (external forces), model (2.27)–(2.32): maximise the flow of external forces $\sum_{i\in F_e}\vec{F}_i$ subject to $\hat{C}_M \le T_i - |\vec{F}_i|$ to find the **collapse load**.

The **min-cut** of this flow is interpreted as the **yield line** — the minimal set of critical connections whose removal disconnects the construction.

## Optimisation models (Chapters 4–6)

**Chapter 3** surveys existing metaheuristics (constructive, local search, evolutionary, large-neighbourhood search) and argues their weakness is the crude objective function.

**Chapters 4–5 — the 2D MILP.** A set-covering / set-partitioning formulation over feasible brick placements. With binary variables $x_b = 1$ if placement $b$ is used, sets $V$ (voxels) and $B$ (candidate placements), the skeleton is:

$$\min \sum_{b\in B} c_b,x_b \quad\text{s.t.}\quad \sum_{b\in B_v} x_b = 1 ;; \forall v\in V,\quad (\text{static equilibrium / connectivity constraints}),\quad x_b\in{0,1}$$

Each voxel must be covered exactly once; additional constraints tie in static equilibrium across layer links. Because these models have heavy **symmetry**, the thesis adds a **fix-and-optimise matheuristic** and a **cutting-plane algorithm** that adds equilibrium constraints lazily.

**Chapter 6 — the 3D combined model (the main contribution).** The key claim of the thesis is that the **combinatorial part** (where to place bricks) and the **structural part** (how forces distribute) are *mutually dependent* and should be solved together, not separately as the literature had done. The approach layers:

- a fast **constructive heuristic** (decompose into 1D strips, fill strips),
- a **MILP with static-equilibrium constraints**,
- **branching heuristics** that linearise objectives and speed solving (branch on which brick covers a given link),
- an **Adaptive Large Neighbourhood Search (ALNS)** whose *destroy* method uses the static analysis to target critical (near-collapse) regions and whose *repair* method uses a **QP** to redistribute forces and an **LP** to re-optimise the brick layout on the destroyed subset.

To make the QP tractable it uses a **simplified force model** (aggregating variables/constraints), yielding large runtime reductions — fast enough that the authors frame it as an **interactive design tool**.

## Key findings and limitations

- Covering the shape with bricks is *not* the hard part — many feasible coverings usually exist. The difficulty is placing bricks to satisfy **structural integrity and aesthetics**, which are expensive to compute and interdependent.
- Combining combinatorial + structural optimisation into one framework that finds constructions in **static equilibrium** relatively quickly is the headline result.
- **Limitations the author is candid about:** the static limit analysis only considers dead load (weight), not the infinite space of external load cases, so it can produce false negatives; quantifying the brick-bonding/aesthetic rules into one weighted function was unreliable and had no verification method; and the framework mostly assumes **solid** constructions.
- **Open problems:** automatic **hollowing** and inner-scaffolding (lighter, cheaper constructions — drawing on 3D-printing topology-optimisation work), better aesthetic quantification, and richer force-case handling.

One caveat on the equation numbering: I pulled the force-balance, friction, and max-flow equations directly from the pages, but a few constraint labels in the 2D/3D MILP blocks I'm summarising structurally rather than transcribing symbol-for-symbol. If you want, I can pull any specific model out verbatim — e.g. the full 2D formulation (5.2), the 3D formulation (6.3), or the ALNS destroy/repair pseudocode — and lay out every constraint with its exact numbering.