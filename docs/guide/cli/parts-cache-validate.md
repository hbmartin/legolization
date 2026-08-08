# `parts`, `cache`, `validate`

Three small commands that support the rest.

---

## `parts sync`

Install or update the managed official LDraw parts library.

```
legolization parts sync [--force] [--json]
```

| Flag | Effect |
| --- | --- |
| `--force` | Re-download even when the weekly freshness check says the library is current. |
| `--json` | Single envelope on stdout. |

```sh
legolization parts sync
```

What it does:

- installs or updates the official library into **platform user-data storage** — no
  admin privileges, nothing written to system directories;
- validates the download before replacing anything;
- checks weekly for updates thereafter;
- **continues silently offline** while a valid existing library remains usable.

The library provides part geometry for rendering and for resolving imported LDraw
models. Without it, rendering degrades and `analyze` will tell you to run this
command.

Override the location with `$LDRAWDIR`, or move the whole user-data root with
`$LEGOLIZATION_DATA_HOME` — see [Rendering and parts](../rendering-and-parts.md).

Always exits 0 when the operation completed, including the offline-but-usable case.

---

## `cache`

Inspect or clear the persistent architectural-template cache.

```
legolization cache [--config PATH] [--set KEY=VALUE] OPERATION ...
legolization cache inspect [--json]
legolization cache clear (--key KEY | --all) [--json]
```

!!! danger "Configuration options come *before* the operation"

    `cache` is the one command that registers `--config`/`--set` on the group parser:

    ```sh
    legolization cache --config project.toml inspect    # correct
    legolization cache inspect --config project.toml    # error
    ```

### What is cached

When a model contains repeated components — four identical turrets, eight identical
windows — the placement derived for the first one is reused for the rest. The cache
key is content-addressed: the component's canonical signature (invariant under yaw,
translation, and colour relabelling), the catalog hash, the configuration hash, and
the physics profile. Nothing about paths or timestamps enters it.

That means a cache hit is always safe: a different catalog, a different config, or a
different physics profile produces a different key.

### `cache inspect`

```console
$ legolization cache inspect
[{"key": "…", "payload_sha256": "…", "size_bytes": 4096}, …]
```

### `cache clear`

Exactly one of `--key` or `--all` is required — there is no default.

```sh
legolization cache clear --key 3f9a...
legolization cache clear --all
```

The cache root is `cache.path` if configured, otherwise platform user-data storage.
Disable caching entirely with `--set cache.enabled=false`.

Both operations always exit 0 on success.

---

## `validate`

Validate an assembly manifest.

```
legolization validate [--against PATH] [--json] manifest
```

| Flag | Effect |
| --- | --- |
| `manifest` | The `legolization.assembly-manifest` JSON to check. |
| `--against PATH` | Also compare the manifest against a model file. |
| `--json` | Single envelope on stdout. |

```sh
legolization validate model.manifest.json
legolization validate model.manifest.json --against model.ldr
```

Manifests are the canonical, timestamp-free record of a build or analysis: hashes,
algorithms, exact LDU poses, normalized contacts, capability results, stability
evidence, action relations, instructions, BOM data, artifacts, and cache provenance.
Because they contain no wall-clock time, identical inputs produce identical
manifests — which is what makes `validate --against` a meaningful reproducibility
check.

| Code | Meaning |
| ---: | --- |
| 0 | Valid, and the recorded status is complete and buildable. |
| 1 | Validation error, or the manifest records `status: error`. |
| 2 | Valid, but the manifest records a non-buildable result. |
| 3 | The manifest records `status: partial`. |
