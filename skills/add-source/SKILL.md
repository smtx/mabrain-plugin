---
name: add-source
description: Add material to the user's MaBrain brain - a local file, one web page, or a whole site or section (crawled on this machine, previewed before anything is uploaded). Use when the user wants the brain to learn from a document, a URL or a website, or to re-crawl a site for changes.
argument-hint: "[file path | URL | site URL]"
---

# Add a source to the brain

The brain learns only from what is uploaded. MaBrain's server never downloads URLs: pages are
fetched here, on the user's machine, and only their content is sent. Answer the user in their
language.

Pick the path by what the user gave (`$ARGUMENTS`, or ask):

| Input | Path |
|---|---|
| A local file (md, txt, html, pdf, docx; up to 50 MB) | A. Upload the file |
| One web page | B. One page |
| A site, a section (`/guides/`), a sitemap, or "re-crawl" | C. Crawl |
| Pasted text, or a short note from the user | Use the MaBrain `add_source` tool with `content`, `title` and, if there is one, `source_url` |

If the user has more than one brain, call `list_brains` first and pass `brain` (the slug) everywhere.

## Before A or C: sign in once on this machine

Uploads from the shell use the crawler that ships with this plugin, signed in to MaBrain once per
machine (no key to copy). Check `python3 --version` first (3.10+), then check the session:

```bash
test -f ~/.mabrain/credentials.json && echo "signed in" || echo "not signed in"
```

- Not signed in: run `python3 "${CLAUDE_SKILL_DIR}/scripts/mabrain-crawl.py" login`. It opens the
  browser; the person signs in with GitHub and allows access. Tell them that is all it asks.
- An agent or CI without a browser uses a key in `MABRAIN_API_KEY` instead (the crawler uses it when
  set). Never print, echo or write a key, and never ask anyone to paste one into the chat.
- No Bash here (Claude Desktop, claude.ai): only B and `content` are available; say so if the user
  asked for a crawl or a file upload.

## A. Upload a file

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/mabrain-crawl.py" upload-file '<absolute path>' --brain <slug> [--source-url '<url>'] [--title '<title>']
```

Quote the path with single quotes (if it contains one, ask the user to rename the file or copy it).
The answer has `job_id`; follow it with `get_job` (step D). `add_source` with `path` returns the same
command with every value already quoted, if you prefer to take it from there (replace
`mabrain-crawl.py` with the path above).

## B. One page

1. WebFetch the URL with this prompt, literally: "Return the full text of the page word for word,
   with its headings, lists and tables. Do not summarize, shorten or add anything."
2. Check it is the whole page (not a summary, not cut). If it looks truncated, use C with
   `--urls-file` for that one URL instead: the crawler sends the literal HTML.
3. Call `add_source` with `content` (the text, at most 100 KB), `title` (the page title) and
   `source_url` (the URL). Then step D.

## C. Crawl a site or a section

The crawler ships with this plugin (Python 3.10+, standard library only). Check `python3 --version`
first. It never sends a page through a model. It keeps each page's content and leaves out what
repeats on every page (menu, header, footer, sidebars, forms), so the credit goes to knowledge;
`--full-page` uploads whole pages instead. Privacy, terms, cookie and account pages are skipped
(`--include-legal` keeps them).

1. Preview (downloads, uploads nothing):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/mabrain-crawl.py" preview --brain <slug> --url '<url>' [--prefix /guides/] [--max-pages 50]
   ```

   Exact pages instead of discovery: `--urls-file pages.txt` (one URL per line, same site).
   Quote the URL with single quotes; if it contains a single quote, write it to a file and use
   `--urls-file`.
2. Show the user the preview from the JSON it prints, in plain words: how many pages (`counts`:
   new, unchanged, failed), a few of the pages in `to_upload` (URLs: show them as page names, not raw links), how many legal pages were left out
   (`skipped`), and `estimate.approx_cost_usd` (what the upload will roughly cost) next to the credit
   left this month (`list_brains` shows it as `credit.remaining_usd`). If `estimate.rate_source` is
   `default`, say the cost is approximate (the server's price could not be read). Keep `run_id`. Ask
   for a go-ahead and do not upload without it. If the cost is above the credit left, suggest a
   smaller part (`--prefix`, `--max-pages` or `--urls-file`).
3. Upload exactly what was previewed, in the background (it can take long; one page at a time):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/mabrain-crawl.py" upload --run <run_id>
   ```

   Run it with `run_in_background`. Tell the user how long it should take (about half a minute per
   page) and that they can ask "how is it going?" at any time. Exit codes: 0 done; 2 some pages failed (listed with the reason);
   3 stopped (credit exhausted, or a job still running): run the same `upload --run` again later, it
   continues where it stopped and never sends a page twice.
4. Progress at any time: `python3 "${CLAUDE_SKILL_DIR}/scripts/mabrain-crawl.py" status --run <run_id>`.
   Answer from its `progress`: pages done of the total, what they have cost so far
   (`approx_spent_usd`) and the minutes left (`eta_minutes`). The background output also has one
   line per page, `[done/total] state url (~cost so far, about N min left)`.

Sandboxed Claude Code: the site's domain and `api.mabra.in` must be in the sandbox's allowed
domains; if a request is blocked, tell the user which domain to allow.

Pages that changed since the last crawl are uploaded as new documents and the old version stays;
unchanged pages are skipped at no cost.

## D. Follow the job

`get_job` with the `job_id`: `queued` or `running` → check again in a minute; `completed` → say how
many facts were created; `partial` → say which part failed (`detail`); `failed` → show `detail`;
`unknown` → the engine has not confirmed the extraction yet: each `get_job` tries to reconcile it, which
can take a few checks while the engine is unavailable (at most 30 min). Do not upload the same source
until it is no longer `unknown`.

Errors: `busy` (another of the user's sources is still being extracted) → wait for it and retry;
`duplicate` → this content is already in the brain, nothing to do; `credit_exhausted` → the hint
says what it would cost and what is left: suggest a smaller part or waiting for next month's credit.
Asking the brain never spends credit.

When it completes, offer the `evaluate` skill to check that the brain now covers the questions this
source was meant to answer.
