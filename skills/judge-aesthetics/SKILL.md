---
name: judge-aesthetics
description: >-
  Render two brick models under identical fixed views, judge which one
  looks better against a fixed rubric, record the forced-choice verdict
  in the project's tracked preference log, and escalate any pair you
  cannot call confidently to the human. Use when comparing strategy or
  seed variants visually, checking whether an aesthetics change actually
  looks better, or growing the paired-preference dataset that calibrates
  the beauty objective.
license: GPL-3.0-or-later
---

# Judge rendered pairs and grow the preference log

The beauty objective is calibrated from real judgements, not intuition:
every verdict recorded here feeds the Bradley-Terry weight fitting
(`scripts/fit_preference_weights.py`). One pair judged carefully is worth
more than ten judged sloppily — when in doubt, escalate; the human's
answer becomes training signal too.

## Requirements

This skill works inside a checkout of the legolization repository — the
pair scripts (`scripts/render_pairs.py`, `scripts/review_pairs.py`) and
the tracked log (`references/aesthetic-preferences/pairs.jsonl`) live in
the repo, not in the installed package. It also needs a working renderer;
if rendering fails with no renderer found, set one up with the
`render-ldraw` skill first (its setup script probes and installs). A PNG
on disk is the only render success signal — never trust exit codes.

## Conversation contract

- Say up front which pairs you are judging and why, and what the verdicts
  will be used for; then render, judge, record, and summarize.
- Judge images before consulting labels: form each verdict from the two
  renders alone, then map sides back to models. Never let a strategy
  name, seed, or file path tip a close call.
- Never judge a pair whose manifest `status` is not `rendered` — a
  missing view is a camera asymmetry, not evidence.
- Escalate rather than guess: a pair where the views disagree or the call
  is close gets `confidence: "low"` and goes to the human — live when the
  human is present, into the review queue otherwise.

## Workflow

1. Render the pairs with fixed views and recorded presentation order:

   ```sh
   uv run python scripts/render_pairs.py --pair A.ldr B.ldr [--pair C D …]
   ```

   The manifest path prints to stdout. Bundle directories are accepted
   for either side (resolved via `bundle.json`).

2. Read the manifest; for each pair with `status: "rendered"`, Read every
   image of both sides in the manifest's `presentation_order`, one view
   at a time (front against front, iso against iso, top against top).

3. Apply the rubric per view, in this order, before any overall call:
   silhouette fidelity (does it read as the intended shape), global
   mirror symmetry (one plane for the whole model), colour coherence
   (blocked regions beat speckle), seam bonding (running bond beats
   stacked seams), and detail legibility (features survive at brick
   scale).

4. Decide: `winner` is `"a"`, `"b"`, or `"tie"` — the letter names the
   MODEL (`model_a`/`model_b` by sha256), never the screen side.
   `confidence` is `"high"` only when the views agree and at least two
   rubric items separate the pair; otherwise `"low"`.

5. Record every verdict through the validated write path, copying `id`,
   `sha256_*`, and `presentation_order` from the manifest verbatim:

   ```sh
   uv run python scripts/review_pairs.py --record '<verdict JSON>'
   ```

   The row schema is documented in
   `references/aesthetic-preferences/README.md`.

6. Escalate the low-confidence pairs. With the human present, show both
   images and ask for their pick; record their answer as
   `judge: "human", confidence: "high"`. Otherwise leave your low-
   confidence rows in place and offer the batch page:

   ```sh
   uv run python scripts/review_pairs.py --manifest <manifest.json>
   uv run python scripts/review_pairs.py --manifest <manifest.json> --merge "<id>=a <id>=tie …"
   ```

## Presenting results

Summarize as a table — pair id, winner, confidence, and the rubric items
that decided it — followed by the escalations awaiting the human and the
review-page path if one was built. Both scripts exit `0` on success and
`1` when nothing could be rendered or a row failed validation; a
validation failure means the verdict JSON is malformed, not that the
judgement was wrong.

## Advanced controls (only on request)

- `--views front,iso,top`, `--size N`, `--seed N`, `--out DIR` on
  `render_pairs.py` control the camera set-up; keep them identical across
  pairs that will be compared in one sitting.
- `--log PATH` on `review_pairs.py` writes to an alternate log (tests
  use this; the calibration log is the default path).
