---
name: add-source
description: Add material to the user's Mabrain brain - a local file, one web page, or a whole site or section (crawled on this machine, previewed before anything is uploaded). Use when the user wants the brain to learn from a document, a URL or a website, or to re-crawl a site for changes.
argument-hint: "[file path | URL | site URL]"
---

# Add a source to the brain

The brain learns only from what is uploaded. Mabrain's server never downloads URLs: pages are
fetched here, on the user's machine, and only their content is sent. Answer the user in their
language.

Pick the path by what the user gave (`$ARGUMENTS`, or ask):

| Input | Path |
|---|---|
| A local file (md, txt, html, pdf, docx; up to 50 MB) | A. Upload the file |
| One web page | B. One page |
| A site, a section (`/guides/`), a sitemap, or "re-crawl" | C. Crawl |
| Pasted text, or a short note from the user | Use the Mabrain `add_source` tool with `content`, `title` and, if there is one, `source_url` |

If the user has more than one brain, call `list_brains` first and pass `brain` (the slug) everywhere.

## Before A or C: the ingest key

Uploads from the shell use an ingest key (`mb_in_…`) in the environment variable `MABRAIN_API_KEY`.
Check it without printing it:

```bash
[ -n "$MABRAIN_API_KEY" ] && echo "key: set (${MABRAIN_API_KEY:0:6}…)" || echo "key: missing"
```

- Never print, echo or write the full key, and never ask the user to paste it into the chat.
- Missing: tell the user to ask their Mabrain operator for an ingest key, add
  `export MABRAIN_API_KEY=mb_in_…` to their shell profile (`~/.zshrc`) and restart Claude Code (an
  `export` in another terminal does not reach a session that is already open). Until then, a file
  under 100 KB can still go through `add_source` with `content` (read it and pass the text).
- No Bash here (Claude Desktop, claude.ai): only B and `content` are available; say so if the user
  asked for a crawl.

## A. Upload a file

Call `add_source` with `path` (absolute) and, if the file came from the web, `source_url`. It returns
a `curl` command with every value already quoted: run it as given with Bash, without editing it.
The answer has `job_id`; follow it with `get_job` (step D).

## B. One page

1. WebFetch the URL with this prompt, literally: "Return the full text of the page word for word,
   with its headings, lists and tables. Do not summarize, shorten or add anything."
2. Check it is the whole page (not a summary, not cut). If it looks truncated, use C with
   `--urls-file` for that one URL instead: the crawler sends the literal HTML.
3. Call `add_source` with `content` (the text, at most 100 KB), `title` (the page title) and
   `source_url` (the URL). Then step D.

## C. Crawl a site or a section

The crawler ships with this plugin (Python 3.10+, standard library only). Check `python3 --version`
first. It never sends a page through a model: it downloads the literal HTML and uploads it.

1. Preview (downloads, uploads nothing):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/mabrain-crawl.py" preview --brain <slug> --url '<url>' [--prefix /guides/] [--max-pages 50]
   ```

   Exact pages instead of discovery: `--urls-file pages.txt` (one URL per line, same site).
   Quote the URL with single quotes; if it contains a single quote, write it to a file and use
   `--urls-file`.
2. Show the user the preview from the JSON it prints: `counts` (new, unchanged, failed), a sample of
   `to_upload`, and `estimate` (size of the visible text). Keep `run_id`. Ask for a go-ahead and do
   not upload without it.
3. Upload exactly what was previewed, in the background (it can take long; one page at a time):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/mabrain-crawl.py" upload --run <run_id>
   ```

   Run it with `run_in_background`. Exit codes: 0 done; 2 some pages failed (listed with the reason);
   3 stopped (credit exhausted, or a job still running): run the same `upload --run` again later, it
   continues where it stopped and never sends a page twice.
4. Progress at any time: `python3 "${CLAUDE_SKILL_DIR}/scripts/mabrain-crawl.py" status --run <run_id>`.

Sandboxed Claude Code: the site's domain and `api.mabrain.dev` must be in the sandbox's allowed
domains; if a request is blocked, tell the user which domain to allow.

Pages that changed since the last crawl are uploaded as new documents and the old version stays;
unchanged pages are skipped at no cost.

## D. Follow the job

`get_job` with the `job_id`: `queued` or `running` → check again in a minute; `completed` → say how
many facts were created; `partial` → say which part failed (`detail`); `failed` → show `detail`;
`unknown` → the extraction's answer was lost and it may be running: wait, and do not upload the
same source again until it clears.

Errors: `busy` (another of the user's sources is still being extracted) → wait for it and retry;
`duplicate` → this content is already in the brain, nothing to do.

When it completes, offer the `evaluate` skill to check that the brain now covers the questions this
source was meant to answer.
