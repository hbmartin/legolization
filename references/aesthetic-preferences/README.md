# Aesthetic preference log

`pairs.jsonl` is this project's own forced-choice comparison dataset: one JSON
object per line, one judged render pair per object. It is the raw material the
Bradley-Terry weight fitting (`scripts/fit_preference_weights.py`) consumes,
adopting the methodology of Dev, *Modeling Aesthetic Preferences in 3D Shapes*
(arXiv:2505.12373) on our own domain — that paper's dataset is unreleased (see
`scripts/datasets.toml`, `shape-aesthetics-pairs`), so the pairs here are
judged by this project instead: Claude first, a human wherever Claude is not
confident. The log is small and precious, so unlike every other evaluation
output it is **committed**; the renders it refers to are not
(`eval/preferences` is gitignored). The models named in committed rows are
repo-relative and live in `models/`, so the preference fit remains reproducible
from a fresh checkout.

## Row schema

```json
{
  "id": "20260809T120000Z-003",
  "model_a": "references/aesthetic-preferences/models/heart-optimized.mpd",
  "model_b": "references/aesthetic-preferences/models/heart-beauty.mpd",
  "sha256_a": "…",
  "sha256_b": "…",
  "views": ["front", "iso", "top"],
  "winner": "a",
  "judge": "claude",
  "confidence": "high",
  "presentation_order": "ba",
  "notes": "coherent colour blocking, cleaner silhouette",
  "recorded": "2026-08-09T12:34:56Z"
}
```

## Conventions (pinned by `tests/test_preference_pairs.py`)

- **`winner` names the model, never the screen position.** `"a"` always means
  `model_a` (identified by `sha256_a`), regardless of `presentation_order`.
  `presentation_order` records what the judge actually saw (`"ba"` = model_b
  was presented on the left/first) so position bias stays measurable.
- Models are identified by content (`sha256_*` of the resolved model file).
  Committed rows use repo-relative, tracked paths; an unavailable or
  hash-mismatched model is rejected before the row is appended. Alternate logs
  outside this tracked directory remain available for tests and dry runs.
- `judge` is `claude` or `human`; `confidence` is `high` or `low`. A `claude`
  row with `confidence: "low"` is an open escalation — the pair appears in the
  next `scripts/review_pairs.py` page until a `human` row for the same `id`
  lands, without exposing the provisional Claude choice or rationale. Rows are
  append-only: a human verdict never rewrites the Claude row, it supersedes it
  (the fit prefers `human` rows, then `high`-confidence `claude` rows).
- Ties are real verdicts (`"tie"`), distinct from abstention: an abstaining
  judge writes no row and escalates instead.

## Workflow

Rendering pairs, judging them, and escalating ambiguity is the
`judge-aesthetics` skill (`skills/judge-aesthetics/`); batch human review is
`scripts/review_pairs.py`; the paired-comparison protocol (fixed views,
randomized presentation, forced choice + abstain) is documented in
`docs/guides/self-evaluation-playbook.md`.
