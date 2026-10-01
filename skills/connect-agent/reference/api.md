# Mabrain API for agents

Base URL: `https://api.mabrain.dev`. Every call: `Authorization: Bearer <key>`. Keys by role:
`mb_ro_` read (ask, list gaps), `mb_in_` ingest (read + add knowledge and sources), `mb_cu_` curate
(read + approve, reject, delete, answer gaps). Give an agent the lowest role it needs.

Errors: `{"error": {"code": "...", "hint": "..."}}`. Show or log the `hint`; it says what to do.
Every write must send `Idempotency-Key`: create it before the first attempt and reuse it, with the
identical request, on retries (same key and body within 24 h returns the stored answer; another
body is 422). A timeout or 5xx can come after the write was applied, so a retry without the key can
write twice. `409 idempotency_in_progress`: wait and retry; `409 idempotency_outcome_unknown`: read
the current state before doing anything else.

## The three verbs

| Verb | Call | Role |
|---|---|---|
| Ask | `POST /v1/brains/{brain}/ask` `{"question": "..."}` → `facts[]`, `context`, `gap`, `coverage`, `gap_id` | ro |
| Ingest | `POST /v1/brains/{brain}/knowledge` `{"content": "...", "tentative": false}` | in |
| | `POST /v1/brains/{brain}/sources` (multipart: `file`, optional `source_url`, `title`) → `job_id`; then `GET /v1/brains/{brain}/jobs/{job_id}` | in |
| Curate | `GET /v1/brains/{brain}/review`; `POST …/facts/{id}/approve` `{"reason"?}`; `POST …/facts/{id}/reject` `{"reason"}`; `DELETE …/facts/{id}/curations/{curation_id}`; `DELETE …/facts/{id}?reason=` | cu |
| Gaps | `GET /v1/brains/{brain}/gaps?state=open` (ro); `POST …/gaps/{id}/answer` `{"content", "completeness"}`, `POST …/gaps/{id}/dismiss` `{"reason"}` (cu) | |
| Other | `GET /v1/brains`, `GET /v1/brains/{brain}`, `GET /v1/brains/{brain}/facts/{id}`, `POST /v1/brains/{brain}/evaluate` `{"questions": [...]}` | ro |

`{brain}` is the brain's slug (from `GET /v1/brains`).

## Using `ask` in an agent

Put `context` in the model's prompt (it is ready to paste: numbered `[fuente n]` blocks with source
and certainty) and tell the model to answer only from it and cite `[fuente n]`. When `gap` is true,
the brain does not cover the question: the agent should say so (the gap is already recorded for the
team) instead of answering from general knowledge.

Each fact: `id`, `content`, `certainty` (0-1), `level`, `source`, `source_url`, `locator`, `passage`.

## Tool definitions

`tools-anthropic.json` (Claude Messages API `tools`) and `tools-openai.json` (OpenAI `tools`) define
every verb: read (`ask_brain`, `list_gaps`, `get_fact`), ingest (`add_knowledge`) and curate
(`review_queue`, `approve_fact`, `reject_fact`, `revoke_curation`, `delete_fact`, `answer_gap`,
`dismiss_gap`). Give the model only the tools its key's role allows (`role` in `routes.json`).

`routes.json` maps each tool to its HTTP call: fill `{brain}` from your configuration and each name in
`path_params` from the tool input; the remaining input goes in the JSON body (`other_params: body`)
or the query string (`query`). Files are not a tool: upload them with `POST …/sources`.
