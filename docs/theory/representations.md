# Representations

Four data structures carry everything. Their invariants are what the rest of the
system is allowed to assume.

---

## The three-tier geometry story

This is the structural idea that most explains the codebase's shape.

| Tier | Unit | Answers |
| --- | --- | --- |
| **Target cells** | 1 stud × 1 stud × 1 plate, integer | Which cells does the shape want filled? |
| **Filled cells** | same lattice | Which cells does this part *contribute* to the shape? |
| **Exact LDU boxes** | integer LDU AABBs | Do these two parts physically collide? Do these two connectors mate? |

The tiers are not redundant. A 45° slope **occupies** a 2×2×3 cell block for collision
purposes but only **fills** the cells below its sloped face. A SNOT bracket's occupied
cells are a conservative collision prism, not physical volume.

The rule that follows: **coarse cells are never used as physical connector evidence.**
Contacts are matched in exact integer LDU, which is what keeps half-stud and
half-plate features distinct from ordinary stud-up geometry.

Consequence for the user: generated placement is yaw-only on the coarse lattice, but
imported LDraw geometry is preserved exactly and analyzed capability by capability. No
meaningful pitch, roll, or half offset is silently snapped.

---

## `VoxelGrid`

`src/legolization/grid.py`. A frozen dataclass wrapping `codes: np.ndarray` of dtype
int16, shape `(nx, ny, nlayers)`, where `layer` counts **plate heights**, z-up.

### Sentinels and the colour lattice

| Value | Meaning |
| --- | --- |
| `≥ 0` | An LDraw colour code |
| `EMPTY = -1` | Nothing here |
| `IGNORE = -2` | Filled, but colour-free |

`merge_colour(*codes)` defines the lattice every merge and tiler consults:

$$
\text{merge}(a,b) =
\begin{cases}
\texttt{IGNORE} & a = b = \texttt{IGNORE}\\
c & \text{exactly one of } a,b \text{ is a specific colour } c\\
c & a = b = c\\
\bot & a \neq b,\ \text{both specific}
\end{cases}
$$

`⊥` means *incompatible* — the merge is refused. `IGNORE` is the top element: a
wildcard that any colour absorbs.

This is what makes hollowing cheap. Interior cells are invisible, so forcing them to a
colour would fragment merges on boundaries nobody sees. Marking them wildcards lets a
brick span the shell/interior boundary freely.

### Erosion, twice

| Method | Erosion | Used for |
| --- | --- | --- |
| `interior_mask()` | Isotropic 6-connectivity | Invisibility — which cells can never be seen |
| `core_mask(margin_xy, margin_z)` | **Anisotropic** | Hollowing — a shell of a given physical thickness |

`core_mask` erodes to `min(xy, z)` in common, then extends along each axis separately.
The anisotropy is forced by the lattice: cells are 1 stud × 1 stud × 1 plate, so one
brick of shell is `margin_xy=1, margin_z=3`.

### Invariants

- The array must be 3-D.
- Every non-empty integer code is validated against the LDraw palette on construction.
- `.vox` parsing composes the full MagicaVoxel scene graph (`nTRN`/`nGRP`/`nSHP`) with
  cycle detection and a 512-node depth cap. Missing RGBA falls back to the format's
  documented implicit palette. Ambiguous unplaced models and conflicting overlaps are
  errors, not silent choices.

### Mesh annotations

A grid from a mesh carries `MeshFeatureAnnotations`: `target_studs`, `grid_phase`,
`surface_error`, `constructibility`, `planar_region_count`, `detail_candidates`, and
`local_normals`. The last two are consumed by the SNOT pass, by slope orientation, and
by exact placement's gate on special parts.

---

## `Part`

`src/legolization/catalog.py`. Frozen. Local cells are `(dx, dy, dz)` with `dz` in
plates, the footprint's long axis along `+dx`, and yaw counter-clockwise in 90° steps.

| Field | Role |
| --- | --- |
| `occupied_cells` | Coarse collision/target cells |
| `filled_cells` | Cells contributing to the target shape — defaults to `occupied_cells` |
| `collision_boxes_ldu` | Exact integer-LDU AABBs, synthesized per cell unless declared |
| `top_connectors` / `bottom_connectors` | Stud and anti-stud positions |
| `mount_normal` | Non-`None` marks a **sideways** (SNOT) part |
| `emit_yaw_offset`, `mount_matrices` | Raw LDraw emission rotations |
| `orientations` | Permitted yaws, reduced by symmetry |

`orientations` reduction: four yaws in general, `(0, 90)` for square footprints, `(0,)`
for 1×1. Enumerating symmetric duplicates would multiply candidate counts for nothing.

### The catalog

58 parts, schema 2. Masses in grams come from BrickLink's published "Weight (in
Grams)" field, ingested by `scripts/ingest_bricklink_masses.py`.

| Category | Count | Notes |
| --- | ---: | --- |
| `brick` | 11 | 1×1 through 2×8 |
| `plate` | 11 | |
| `tile` | 4 | |
| `slope` | 5 | Including inverted |
| `snot` | 4 | Sideways-mounting carriers |
| `special` | 20 | Import-only stud-up geometry, excluded from placement |
| `special_snot` | 3 | Import-only sideways geometry, excluded from the SNOT pass |

Catalog stud lengths `(1, 2, 3, 4, 6, 8)` drive the lookahead DP in the constructive
heuristics, and the largest footprint area `A_MAX = 16` (a 2×8) normalizes size terms
in several objectives. `catalog_hash()` — a SHA-256 of the catalog bytes — feeds
template cache keys, manifests, and bundle identity.

A draft part may carry `mass_g = NaN` until an estimate sidecar supplies it, which is
why `Part.has_mass` exists as a separate predicate.

---

## `Layout`

`src/legolization/layout.py`. Mutable. Two structures kept in sync:

```python
bricks: dict[int, PlacedBrick]  # id -> placement
occupancy: dict[Cell, int]  # cell -> brick id
```

The `occupancy` index is the point: it makes collision and adjacency O(1), explicitly
replacing the O(n²) scans of the reference implementations this project started from.

### `add()` invariants

Three checks, all enforced:

1. No cell below ground — `cell[2] >= 0`.
2. No coarse-cell occupancy collision.
3. **No exact-LDU box intersection** against the 27-cell neighbourhood.

Violations raise `CollisionError`. Check 3 is what catches the cases check 2 cannot:
two parts can be coarse-cell-disjoint and still physically overlap in LDU when one of
them is a slope or a sideways mount.

### Deliberate bypasses

| Method | Behaviour | Why |
| --- | --- | --- |
| `subset(ids)` | New layout with a subset of bricks | Prefix analysis during sequencing |
| `translated(dz=)` | **Preserves brick ids** by bypassing `add()` | Subassembly bookkeeping keys on ids; re-adding would renumber them |
| `replace_with(other)` | Wholesale content swap | The guard pattern's revert |

`occupancy_grid(layout)` inverts the whole thing — layout to coloured `VoxelGrid`,
normalized layout, and origin offset. It is the shared substrate under `--retile` and
under repair's refill.

---

## `ConnectionGraph`

`src/legolization/graph.py`. Built from a layout, it is the sole authority on what is
connected to what.

### Contact matching

A socket index maps `(exact LDU point, direction) → brick_id`, built from every
brick's **bottom** physical connectors. For every **top** connector, look up
`(point, −direction)`. A match is a `KnobContact`.

Ground contacts are bottom connectors with `point[2] == 0` **and**
`direction == (0,0,-1)` — only downward anti-studs seat on the baseplate. A sideways
connector at layer zero does not count as grounded, which is correct and easy to get
wrong.

Lateral (SNOT) mates carry their sideways direction in `normal`, and then the
contact's `x`/`y`/`interface_layer` refer to the stud cell itself rather than to a
mating plane.

### Side contacts

`SideContact` aggregates shared vertical faces per (brick pair, axis), recording
`face_count`, `centroid`, the vertical extremes `z_lo`/`z_hi`, and the **transverse
face-edge** extent `t_lo`/`t_hi` at `lo − 0.5` / `hi + 0.5`.

The transverse extent was corrected in v7. Before, a one-cell shared face produced two
duplicate zero-width generators; now it produces two distinct yaw levers, which is
what the torque model needs.

!!! note "Cladding parts are excluded from side contacts"

    Parts with a `mount_normal` are excluded on **both** sides. Their occupied cells
    are a conservative collision prism, not physical volume — counting those faces
    gave a mounted tile a phantom press contact on top of its real lateral stud mate.
    Their only physical connection is the stud.

### Two connectivity semantics

```python
component_count()  # stud connections between bricks only; ground does NOT join
floating_ids()  # ground-merged reachability
topology_metrics()  # both results from one brick-component labeling
```

`topology_metrics()` labels the brick-only graph once with
`scipy.sparse.csgraph.connected_components`. A component floats exactly when none of
its bricks has a ground contact, so ground reachability follows from that same
labeling without merging ground into the component count. The immutable graph caches
the count-only result, component labels, and full topology summary separately.
`component_count()` therefore avoids materializing labels and grounding metrics when
only the count is needed; `brick_components()`, `floating_ids()`, and
`topology_metrics()` reuse cached labels once they exist. A later label request after a
count-only request may run the sparse component routine again. Conflating the two
semantics remains the classic bug: with ground merged into the count, two disconnected
towers on one baseplate look like one model.

---

## Units and exact geometry

`src/legolization/ldraw_units.py`:

| Constant | Value | Meaning |
| --- | ---: | --- |
| `STUD_LDU` | 20.0 | LDU per horizontal stud pitch |
| `PLATE_LDU` | 8.0 | LDU per vertical plate height |
| `GRID_TOLERANCE_LDU` | 0.2 | Max positional transform noise per axis on import |
| `ROTATION_TOLERANCE` | 1e-2 | Max dimensionless matrix-entry noise for yaw decoding |

`src/legolization/physical.py` turns cells into geometry:

```text
box_for_cell(x, y, z):
    x → [20x − 10, 20x + 10)      stud-centred horizontally
    z → [8z, 8(z+1))              plate-bottom-anchored vertically
```

```text
physical_connector(c):
    point = (20·x + 10·dx,
             20·y + 10·dy,
             8·z + (8 if dz > 0 else 0) + (4 if dz == 0 else 0))
```

That last term is load-bearing: a **lateral** connector (`dz == 0`) sits at the
*half-plate* height. That single offset is what keeps SNOT mates from ever being
confused with vertical ones in the socket index.

`LduBox` is a half-open integer AABB that rejects degeneracy on construction; its
`intersects` requires **positive volume** overlap on all three axes, so parts that
merely touch do not count as colliding.

### Three ratios that look alike and are not

| Ratio | Value | Meaning |
| --- | ---: | --- |
| Geometric plates per stud | 2.5 | `STUD_LDU / PLATE_LDU` = 20/8 |
| Mesh pre-stretch | 2.5 | The same geometric ratio, applied before voxelization |
| **Physics** plates per stud | 2.4375 | `KNOB_PITCH_M / PLATE_HEIGHT_M` = 7.8 mm / 3.2 mm |

The physics ratio differs because the RBE uses StableLego's *measured* knob pitch and
plate height in metres, not the idealized LDU ratio. Using 2.5 in the physics lever
arms would be a small but systematic error.

---

## Colour quantization

`src/legolization/color.py`. The palette is introspected from the generated LDraw
colour library and restricted to **opaque solid** colours — no metallic, chrome,
glitter, or transparency, and excluding the LDraw pseudo-codes 16 and 24.

Quantization is Euclidean distance in **Oklab with the chroma axes over-weighted**, so
desaturated inputs land on desaturated bricks instead of drifting toward a nearby
saturated colour.

Optional Floyd–Steinberg dithering runs per horizontal slice with taps
`(0,1,7/16)`, `(1,−1,3/16)`, `(1,0,5/16)`, `(1,1,1/16)`, and diffuses error **only
into filled cells of the same slice** — so gradients dissolve without bleeding across
empty space or between layers.

---

## Repeated components

`repetition.repeated_components(grid)` finds 6-connected voxel components equal up to
**yaw, translation, and colour relabelling**. Canonicalization takes the lexicographic
minimum over the four yaws of the normalized cell tuple with colours relabelled by
first-appearance order; the component's `signature` is the SHA-256 of that canonical
tuple.

`templates.instantiate_repeated_templates` derives one placement from the first
instance and applies it to the rest, with a content-addressed persistent cache keyed by
`(component_signature, catalog_hash, configuration_hash, physics_profile,
algorithm_version)`.

Cache statuses are deliberately fine-grained:

| Status | Meaning |
| --- | --- |
| `hit` | Reused from cache |
| `derived` | Computed this run |
| `write` | Computed and stored |
| `recovered` | Cached payload was corrupt; quarantined and recomputed |
| `local_miss` | Structurally valid but collides *here* — says nothing about reuse elsewhere |
| `skipped` | Not attempted |
| `rejected_stability` | Applied, then reverted by the guard |

`local_miss` exists so that a placement failing at one instance does not poison the
cache entry for every other model that could use it.
