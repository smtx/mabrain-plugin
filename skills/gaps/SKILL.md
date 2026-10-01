---
name: gaps
description: Review the questions the user's Mabrain brain could not answer (gaps) and close them - answer one with expert knowledge, add a source that covers it, or dismiss it. Use when the user asks what the brain does not know, what their agents could not answer, or wants to fill holes in the brain.
argument-hint: "[gap id]"
---

# Work through the brain's gaps

A gap is a question someone (the user, Claude, or one of their agents) asked and the brain could not
answer from its material. Closing gaps is how the brain improves. Answer in the user's language.

## 1. List

`list_gaps` (with `brain` if the user has several; `state_filter` such as `open` to narrow).
Group them by theme and show, per gap: the question, when it was asked, and its state. Suggest which
to tackle first (repeated themes, or the ones closest to what the brain is for).

## 2. Close one

For the gap the user picks (or `$ARGUMENTS`), offer the three ways:

| Way | When | How |
|---|---|---|
| Answer it | The user (or their team) knows the answer | Write the answer with the user, read it back, and on their yes call `answer_gap` (`gap_id`, `content`, `completeness`: `complete`, or `partial` if more is needed) |
| Add a source | A document or page covers it | Use the `add-source` skill; when its job completes, run `ask` with the gap's question to confirm it is now covered |
| Dismiss | Out of scope, a duplicate, or nonsense | Ask the reason and call `dismiss_gap` (`gap_id`, `reason`) |

## Rules

- The answer must be the user's knowledge, not yours: do not fill a gap with general knowledge
  unless the user explicitly says it is correct for them. Write it as short, self-contained
  statements (one fact per sentence), with names and numbers explicit.
- An answer given by a signed-in person counts as their expert endorsement; one given with an agent
  key does not. Say which applies if the user asks why a fact is "verified" or not.
- Answering needs the curation role; listing works with any role. If `answer_gap` is missing, say the
  operator grants it.
- Errors carry a `code` and a `hint`: show the hint. `conflict` on a gap means its state changed
  (someone answered or dismissed it); list again.
