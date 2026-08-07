# BrickSim: A Physics-Based Simulator for Manipulating Interlocking Brick Assemblies

Source PDF: `BrickSim A Physics-Based Simulator for Manipulating Interlocking Brick Assemblies.pdf`

arXiv:2603.16853v1 [cs.RO] 17 Mar 2026. Haowei Wen, Ruixuan Liu, Weiyi
Piao, Siyu Li, and Changliu Liu — Robotics Institute, Carnegie Mellon
University. Code: https://github.com/intelligent-control-lab/BrickSim

Text-only conversion (figures omitted; the repo's pdf-to-markdown tool
needs undeclared dependencies — see docs/pr-19-review.md).

## Abstract

Interlocking brick assemblies provide a standardized yet challenging
testbed for contact-rich and long-horizon robotic manipulation, but
existing rigid-body simulators do not faithfully capture snap-fit
mechanics. We present BRICKSIM, the first real-time physics-based
simulator for interlocking brick assemblies. BRICKSIM introduces a
compact force-based mechanics model for snap-fit connections and solves
the resulting internal force distribution using a structured convex
quadratic program. Combined with a hybrid architecture that delegates
rigid-body dynamics to the underlying physics engine while handling
snap-fit mechanics separately, BRICKSIM enables real-time,
high-fidelity simulation of assembly, disassembly, and structural
collapse. On 150 real-world assemblies, BRICKSIM achieves 100% accuracy
in static stability prediction with an average solve time of 5 ms. In
dynamic drop tests, it also faithfully reproduces real-world structural
collapse, precisely mirroring both the occurrence of breakage and the
specific breakage locations. Built on Isaac Sim, BRICKSIM further
supports seamless integration with a wide variety of robots and
existing pipelines.

## I. Introduction

Brick assembly provides a compelling and widely adopted testbed for
studying contact-rich manipulation, long-horizon planning, and physical
reasoning. While the individual bricks are standardized, their
combinatorial compositions create highly diverse assembly tasks.
Physics simulators are essential for studying such tasks at scale.
Although existing simulators excel at modeling rigid-body dynamics,
they often fail to accurately capture interlocking contacts arising
from micro-elastic deformation and friction, such as the snap-fit
connections between bricks. These limitations call for a simulator that
preserves stable assemblies, reproduces collapse and breakage under
disturbances, runs in real time, and integrates easily with modern
robotic manipulation pipelines.

Contributions: (1) BRICKSIM, the first real-time physics-based
simulator for interlocking brick assemblies supporting physically
realistic assembly, disassembly, and structural collapse, integrated
with Isaac Sim; (2) a hybrid simulation architecture handling snap-fit
mechanics separately from rigid-body dynamics; (3) a compact
force-based mechanics model for snap-fit connections yielding a
structured sparse convex quadratic program for internal force
distribution; (4) validation across static and dynamic scenarios: 100%
accuracy in static stability prediction on 150 real-world assemblies,
faithful reproduction of collapse and breakage patterns in drop tests.

## II. Related Work

Comparison axes (Table I): snap-fit mechanics, temporal dynamics,
efficient runtime, robot integration. Rigid-body simulators (Gazebo,
MuJoCo, Isaac Sim) lack snap-fit mechanics — physically stable brick
structures collapse spontaneously in them. Force-based stability
analysis (Legolization/Luo; StableLego) has snap-fit mechanics but no
temporal dynamics, and explicitly parameterizes loads using
fine-grained local force variables at contact points; the resulting
problem size is large, especially for multi-stud connections, making
real-time analysis challenging. BrickFEM (finite elements, Abaqus) is
accurate but computationally expensive (hundreds of seconds per
structure).

## III. Overview

BRICKSIM augments the Isaac Sim backbone with three modules: the Brick
Topology Graph (BTG), the Assembly Monitor (ASM), and the Breakage
Detector (BRD). The BTG stores snap-fit connectivity, maintains
consistent relative poses within each connected component, and
synchronizes rigid constraints and collision filtering with PhysX. The
ASM monitors contact reports and creates new connections when valid
stud-hole engagements are detected. The BRD evaluates the assembly
under external loads, solves for the internal force distribution, and
removes overloaded connections.

## IV. Brick Topology Graph

All bricks form an undirected graph G = (V, E); edges represent
kinematic connectivity; each connected component is a rigidly connected
assembly with fixed relative poses (rigid transform per edge; all paths
between two bricks must yield the same relative pose). Each brick
exposes stud/hole interfaces on a local grid (unit length L_U = 8 mm);
a snap-fit connection between a stud interface and a hole interface is
parameterized by a discrete tuple (o, psi): planar grid offset in
stud-grid units and relative yaw quantized to multiples of pi/2. Free
yaw rotation of 1x1 connections is not modeled.

Integration with the physics engine: each brick is a rigid body with a
cuboid collision proxy — stud and hole geometry is NOT modeled in
collision. Rigid constraints fix relative poses within a component and
intra-component collision checking is disabled. Extra rigid constraints
between non-adjacent bricks (sampled from a random regular graph of
fixed degree d, fixed seed) create force-propagation shortcuts that
reduce the constraint-graph diameter — physics engines struggle to
propagate forces through long chains of connected bodies.

## V. Assembly Monitor

Per step: (1) enumerate candidate brick pairs from contact reports; (2)
check geometric criteria — snap the relative pose to the nearest
discrete (o, psi) and accept within tolerances (vertical distance to
the pre-engagement height H_S = 1.7 mm, tilt, yaw error, planar error,
positive stud/hole overlap); (3) force criteria — require the
compressive force along the stud axis (impulse / dt projected on the
interface normal) to exceed a threshold F_asm; (4) commit accepted
connections to the BTG and snap bricks to the exact discrete transform.

## VI. Breakage Detector

### A. Constraint model

Two load-bearing constraint families inside a connected component:

- **Contacts C** (touching surfaces, compression only): the contact
  manifold Omega_c is the convex polygonal overlap between two
  surfaces; unilateral frictionless normal forces lambda_v >= 0 at the
  vertices of Omega_c. Wrenches follow from the contact normal and
  vertex positions; Newton's third law pairs the two bricks.
- **Connections K** (snap-fit, carry tension/shear/torsion through
  frictional interlocking): a connection includes an array of studs;
  each stud has 3 or 4 contact points with the hole (as in
  Legolization/StableLego). **The forces at different contact points
  are not independent but are coupled by the micro-displacement between
  the two bricks. Exploiting this coupling reduces each connection to a
  fixed small set of decision variables.** With orthonormal connection
  frame (u, v, n), the traction components along n, u, v are three
  affine fields evaluated at each contact point f at (u_f, v_f):

      p_n(f) = phi_f^T alpha_k,  p_u(f) = phi_f^T beta_k,
      p_v(f) = phi_f^T gamma_k,  phi_f = [1, u_f, v_f]^T

  with alpha_k, beta_k, gamma_k in R^3 the unknowns — nine
  coefficients per connection. Per-point axial (F_a, tension-only,
  F_a >= 0), radial (F_r), and tangential (F_t) components are linear
  in the coefficients. Friction pyramid per point:

      |F_t| + F_a <= mu (F_r + F_0)

  with preload F_0 from snap-fit micro-elastic deformation; typical
  values mu = 0.2 and mu F_0 = 0.7 N (sourced to Legolization).

### B. Solving force distribution

Equilibrium: net internal wrench b_i per brick from the engine's
velocity change minus external impulses over dt. Stacking unknowns into
x gives A x = b per connected component; A depends only on topology and
geometry. Non-negativity and friction constraints at the vertices of
the contact and connection boundaries give G x >= 0, H x <= 1.

Robust lexicographic relaxation in three convex sparse QPs:

1. Project b onto the feasible subspace:
   b* = argmin_y ||y - b||^2 s.t. exists x: A x = y, G x >= 0.
2. Minimum friction-feasibility relaxation:
   v* = argmin_v ||v||^2 s.t. exists x: A x = b*, G x >= 0,
   H x <= 1 + S v, v >= 0 (v is per-connection; S maps to friction
   rows).
3. Minimize the elastic-energy surrogate U = 1/2 x^T Q x
   (per-point w_a F_a^2 + w_r F_r^2 + w_t F_t^2, weights 1.0) under
   the relaxed constraints.

All cost/constraint matrices remain constant while the topology is
unchanged; only right-hand sides update per step. This suits OSQP,
which caches matrix factorizations and warm-starts from the previous
solution, allowing real-time operation.

### C. Breakage criterion

Per-connection utilization u_k = max_f (|F_t| + F_a) / (mu (F_r +
F_0)); u_k > 1 indicates an overloaded connection. Break the smallest
number of overloaded connections containing the most offending one
whose removal disconnects the component; applying this per step mimics
progressive real-world breakage.

## VII. Results

Machine: i9-12900H CPU, 64 GB RAM, RTX 3080 Ti Mobile. Defaults:
eps_z = 1.0 mm, eps_tilt = 5 deg, eps_psi = 5 deg, eps_xy = 2.0 mm,
F_asm = 1.0 N, w_a = w_r = w_t = 1.0, d = 4.

### A. Static stability (Table II)

Evaluation set: 150 assemblies randomly sampled from StableText2Brick,
up to 30 bricks; ground truth established by physically building each
assembly and observing whether it collapses without support.

| | BrickFEM | StableLego | BrickSim |
|---|---|---|---|
| Solvable count | 98 | 150 | 150 |
| Solvability | 65.3% | 100% | 100% |
| False-stable | 22 | 0 | 0 |
| False-unstable | 0 | 3 | 0 |
| Physical accuracy | 77.6% | 98% | 100% |
| Avg. solve time (s) | 193.9 | 0.027 | 0.005 |

StableLego is conservative — all its errors are false-unstable, from
overestimating internal stresses on certain bricks. BrickSim's solve
times are 1-10 ms, an order of magnitude below StableLego's 10-100 ms
and four orders below BrickFEM.

### B. Dynamic drop tests

Assemblies released from ~15 bricks height onto a rigid table: the
bookshelf stays intact in reality and in BrickSim; the bench and guitar
split in two — BrickSim reproduces the collapse and mirrors the
specific breakage locations. End-to-end frame times stay within the
16.7 ms 60-FPS budget for assemblies up to 30 bricks.

### C. Robotic demonstration

Franka single-arm stacking onto a baseplate and bimanual in-hand
assembly, via BrickSim's Python API and Isaac Sim's motion planning.

## VIII. Discussion and Limitations

Frame time grows with structural complexity; the current version
handles assemblies with fewer than 50 bricks in real time. Supported
parts are those with fixed snap-fit connections (bricks, plates,
slopes); functional parts (gears, wheels) are future work.

## Key references

- [8] Luo et al., "Legolization: Optimizing LEGO designs," TOG 2015.
- [9] Pletz & Drvoderic, "BrickFEM," engrXiv 2023, 10.31224/2898.
- [10] Liu et al., "StableLego: Stability analysis of block stacking
  assembly," RA-L 2024.
- [11] Pun et al., "Generating physically stable and buildable brick
  structures from text," ICCV 2025 (StableText2Brick dataset).
- [27] Stellato et al., "OSQP: An operator splitting solver for
  quadratic programs," Math. Prog. Comp. 2020.
