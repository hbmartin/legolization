# Exit codes and JSON

Everything here exists so the tool can be scripted without parsing prose.

---

## Exit codes

| Code | Name | Meaning |
| ---: | --- | --- |
| 0 | `COMPLETE` | Complete. |
| 1 | `OPERATIONAL_ERROR` | Operational error — **including invalid usage**. |
| 2 | `UNBUILDABLE` | Unbuildable, or failed physics. |
| 3 | `PARTIAL` | Partial or indeterminate outcome. |
| 4 | `EXACT_LIMIT` | Exact-placement limit under the `fail` policy. |
| 130 | `INTERRUPTED` | Interrupted after atomically recording resumable state. |

Two things about this table are unusual and deliberate.

**Invalid usage exits 1, not 2.** Argparse's native exit code 2 is overridden,
because 2 is reserved for the domain meaning "unbuildable". A script can therefore
treat 2 as a *result*, never as a typo.

**Exit 3 is often success.** It means "we finished, but the result is qualified".

---

## What each code means per command

| Command | 0 | 2 | 3 | 4 / 130 |
| --- | --- | --- | --- | --- |
| `build` | Buildable | Not buildable | — | 4 on exact limit; 130 on interrupt |
| `bundle` | Buildable, no override | No candidate buildable | Workers still pending, non-certified audit, missing renderer under `--render required`, or a booklet with missing steps | 4, 130 |
| `bundle --retry-materials` | A rung is buildable | No rung buildable | — | — |
| `bundle --cancel-pending` | Cancellation recorded | — | — | — |
| `analyze` | Connected and feasible | Disconnected or infeasible | Partial, or verdict indeterminate | — |
| `analyze --repair` | Repaired, or repair not needed | Search exhausted with no validated fix | Search timed out | — |
| `validate` | Manifest valid, complete, buildable | Valid but not buildable | Manifest records `partial` | — |
| `input inspect` | Always | — | — | — |
| `model render` | Every view rendered | — | Some views rendered | — |
| `instructions audit` | `certified` | `infeasible` | `findings` | — |
| `catalog infer` | Validated, measured, confident | — | Draft written, gates not all passed | — |
| `catalog validate` | All gates pass | — | Any gate fails | — |
| `corpus generate` / `download` / `verify` | Nothing failed | — | — | 1 if any model failed |
| `corpus collect` | Collection complete | — | — | 1 if incomplete |
| `corpus assemble` | Clean | Failed expectation or HARD regression | — | 1 on operational error |
| `corpus evaluate` | Clean | Failure or regression | — | — |
| `cache`, `parts sync` | Always | — | — | — |

### Exception mapping

| Exception | Code |
| --- | ---: |
| `ExactPlacementLimitError` | 4 |
| `PlacementInfeasibleError` | 2 |
| `KeyboardInterrupt` | 130 |
| Everything else caught | 1 |

Only `KeyboardInterrupt`, `LegolizationError`, `OSError`, and `ValueError` are
caught. Anything else propagates as a traceback — that is intentional, because an
unexpected exception is a bug report, not a user error.

---

## The JSON envelope

Every command accepts `--json`. Under it:

- stdout contains **exactly one** JSON object and nothing else;
- progress and warnings go to stderr;
- **errors still emit the envelope on stdout**, so a failing run is as parseable as a
  succeeding one.

```jsonc
{
  "schema": "legolization.result/v1",
  "version": "0.6.0",
  "command": "bundle",
  "status": "complete",
  "exit_code": 0,
  "artifacts": [
    {"path": "model-legolization/model/model.mpd", "kind": "model", "sha256": "…"}
  ],
  "warnings": [],
  "data": { /* command-specific */ }
}
```

| Field | Notes |
| --- | --- |
| `schema` | Always `legolization.result/v1`. |
| `status` | `complete`, `error`, `unbuildable`, `partial`, `interrupted`. |
| `exit_code` | The same integer the process returns. |
| `artifacts` | `{path, kind, sha256?}`. `kind` is `model`, `manifest`, `bundle-record`, `normalized`, `sidecar`, … |
| `warnings` | Strings. Non-fatal, but read them. |
| `data` | Present when the command has structured results — `build` reports `strategy`, `brick_count`, `mass_g`, `step_count`, `stable`, `buildable`; `input inspect` reports the whole inspection report. |
| `error` | `{type, message, detail?}` on failure, absent otherwise. |

JSON is emitted with `indent=2` and sorted keys, so envelopes diff cleanly.

`status` and `exit_code` always agree — codes 1 and 4 both map to `status: "error"`,
which is the one place they are not one-to-one.

---

## Scripting patterns

Check buildability without parsing anything:

```sh
if legolization bundle model.obj --json > result.json; then
  echo "buildable"
else
  case $? in
    2) echo "not buildable — try --retry-materials" ;;
    3) echo "partial — check warnings" ;;
    4) echo "exact placement hit a limit" ;;
    *) echo "error" ;;
  esac
fi
```

Pull the winning model's path:

```sh
legolization bundle model.obj --json \
  | jq -r '.artifacts[] | select(.kind == "model") | .path'
```

Treat "partial" as acceptable but noisy. Match codes 0 and 3 exactly — a
`-le 3` range test would also wave through 1 (`OPERATIONAL_ERROR`) and 2
(`UNBUILDABLE`):

```sh
legolization bundle model.obj --json > out.json
code=$?
case "$code" in
  0|3) ;;
  *) exit "$code" ;;
esac
jq -r '.warnings[]' out.json
```

Read a verdict out of a bundle after the fact:

```sh
jq -r '.verdicts | "buildable=\(.buildable) winner=\(.winner.strategy)"' \
  model-legolization/bundle.json
```

!!! warning "Do not mix `--json` with human output"

    Under `--json` the runner is the sole writer to stdout, and every command
    suppresses its human-readable printing. Anything unexpected on stdout is a bug
    worth reporting.

---

## Parse failures under `--json`

Even a command-line usage error emits the envelope, so a wrapper never has to
distinguish "the CLI rejected my flags" from "the CLI crashed":

```jsonc
{
  "schema": "legolization.result/v1",
  "command": "bundle",
  "status": "error",
  "exit_code": 1,
  "error": {"type": "CliUsageError", "message": "unrecognized arguments: --qualityy fast"}
}
```

The human-readable `error: …` line still goes to stderr alongside it.
