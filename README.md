# Mabrain plugin

Manage your Mabrain brain from Claude Code, Claude Desktop and Cowork.

- **Connector**: the Mabrain MCP server (`https://api.mabrain.dev/mcp`), signed in with GitHub.
- **Skills**: `/mabrain:add-source` (files, pages, whole sites), `/mabrain:curate` (review queue),
  `/mabrain:gaps` (what the brain could not answer), `/mabrain:evaluate` (your regression
  questions), `/mabrain:connect-agent` (integrate your own agents).
- **Crawler**: `skills/add-source/scripts/mabrain-crawl.py` (Python 3.10+, standard library). It runs
  on your machine; page content never goes through a model.

## Install

Claude Code:

```bash
claude plugin marketplace add smtx/mabrain-plugin
```

```bash
claude plugin install mabrain@mabrain
```

Then run `/mcp`, pick the plugin's `mabrain` server and sign in with GitHub. Your GitHub account must be
registered by your Mabrain operator.

Claude Desktop and Cowork: **Customize → Plugins → Add → Add marketplace**, enter
`smtx/mabrain-plugin`, install **Mabrain**, then connect it in the plugin's **Connectors** tab and
sign in with GitHub.

The repository is private: your GitHub account needs read access (your operator grants it).

## Uploading files and crawling sites (Claude Code)

Uploads from the shell use an ingest key from your operator. Add it to your shell profile and
restart Claude Code:

```bash
echo 'export MABRAIN_API_KEY=mb_in_...' >> ~/.zshrc
```

Without it you can still add pasted text and single pages. If Claude Code runs sandboxed, allow the
site's domain and `api.mabrain.dev`.

## An agent or CI instead of a person

Signing in is for people (their curations count as expert endorsements). An agent uses a key and
its own MCP entry instead of this plugin's connector:

```bash
claude mcp add --transport http --scope user mabrain-agent https://api.mabrain.dev/mcp --header 'Authorization: Bearer ${MABRAIN_API_KEY}'
```

The single quotes keep the key out of Claude Code's configuration.
