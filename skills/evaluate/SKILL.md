---
name: evaluate
description: Check which of the user's questions their Mabrain brain covers - a regression test of up to 25 questions, run after adding sources or curating. Use when the user wants to know if the brain can answer their questions, to test coverage, or to compare before and after a change.
argument-hint: "[questions file]"
---

# Evaluate the brain against the user's questions

Answer in the user's language.

## Questions

Use, in this order:
1. A file the user names (`$ARGUMENTS`): one question per line, or a JSON list.
2. `.mabrain/questions.txt` in the current project, if it exists (the user's regression set).
3. Ask the user for their questions (they know what their agents must answer). Offer to save them
   to `.mabrain/questions.txt` so the next run reuses them.

Between 1 and 25 non-blank questions per call; split larger sets into several calls.

## Run

Call `evaluate` with `questions` (and `brain` if the user has several). It records no gaps and changes
nothing. It may take a while for 25 questions (they run four at a time): say so before starting.

## Report

- The summary first (for example "8/10 cubiertas").
- A table: question, status (`covered`, `gap`, or `indeterminate`), and for covered ones the top
  fact with its certainty and source.
- For each gap: what kind of source would cover it, and offer the `add-source` or `gaps` skill.
- `indeterminate` means the brain could not decide (for example the service was slow): run that question
  again with `ask`.

If a previous run exists in `.mabrain/evaluations/`, compare: which questions changed status. A
question that was covered and is now a gap is a regression: point it out first. Save this run as
`.mabrain/evaluations/<date>-<time>.json` (the `evaluate` result as returned) when Bash or file
writing is available.

Covered means: the brain found the question in its material (no gap) with at least one fact of
certainty 0.40 or more.
