# `judge-aesthetics`

**Compare two builds by eye — with the verdict written down.**

A development tool, not a model-building one. Every judgement it records
calibrates the beauty objective, so the numbers eventually agree with your
eyes.

---

## Say something like

> Which of these two looks better built?
>
> Compare the beauty and bond outputs for the heart model.
>
> Did that weight change actually make the mushroom prettier?
>
> Judge the pending pairs and show me the ones you can't call.

---

## What it does

1. **Renders both models** under identical cameras (front, iso, top) via
   `scripts/render_pairs.py`, with the presentation order randomized and
   recorded.
2. **Judges each pair** against a fixed rubric — silhouette, global
   symmetry, colour coherence, seam bond, detail legibility — from the
   images first, labels second.
3. **Records the verdict** in the tracked preference log
   (`references/aesthetic-preferences/pairs.jsonl`) through a validated
   write path.
4. **Escalates what it cannot call**: close calls go to you, live when you
   are present, otherwise onto a self-contained HTML review page
   (`scripts/review_pairs.py`) you can answer in batch.

The accumulated log feeds `scripts/fit_preference_weights.py`, which turns
judgements into *recommended* objective weights — never auto-applied.

---

## Result vocabulary

| Verdict | Meaning |
| --- | --- |
| **a** / **b** | That model wins (the letter names the model, never the screen side) |
| **tie** | A real verdict: neither looks better |
| **low confidence** | An escalation — the pair is waiting for your call |

---

## Where it hands off

- **Single-model inspection instead of comparison** →
  [`render-ldraw`](render-ldraw.md)
- **The winner still needs its numbers checked** →
  [`eval-corpus`](eval-corpus.md)

---

## The full methodology

This page covers the skill. The paired-comparison protocol — fixed cameras,
randomized presentation, forced choice with honest abstention — is §4b of the
[self-evaluation playbook](../../guides/self-evaluation-playbook.md), and the
standing calibration verdicts are in the
[beauty-term validation report](../../reports/aesthetics-validation.md).
