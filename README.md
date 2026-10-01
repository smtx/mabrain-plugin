# MaBrain for Claude

Give your app a brain, from Claude Code. Add your docs, pages or a whole site; ask it and get answers
that cite their source; fix what it gets wrong; plug it into your own agent with a few lines of code.

> MaBrain is in early access: sign-in works for GitHub accounts that have an invite.

## Install (Claude Code)

```bash
claude plugin marketplace add smtx/mabrain-plugin
```

```bash
claude plugin install mabrain@mabrain
```

Restart Claude Code, run `/mcp`, choose the `mabrain` server and sign in with GitHub. Then just ask
Claude, for example: "add https://example.com/docs/ to my brain" or "what does my brain say about
refunds?".

**Claude Desktop and Cowork:** Customize → Plugins → Add → Add marketplace → `smtx/mabrain-plugin`,
install **MaBrain**, and connect it in the plugin's **Connectors** tab.

## What you get

| Skill | Use it to |
|---|---|
| `/mabrain:add-source` | Add a file, a page, or a whole site (crawled on your machine, previewed before anything is sent) |
| `/mabrain:evaluate` | Check which of your questions the brain answers |
| `/mabrain:gaps` | See what it could not answer, and fill it in |
| `/mabrain:curate` | Review and fix facts, one confirmed change at a time |
| `/mabrain:connect-agent` | Wire the brain into your own app or agent (Python, TypeScript, MCP) |

Every answer comes with its source and a certainty from 0 to 1. When the brain does not know, it says
so instead of guessing, and remembers the question so you can fill the gap.

## Uploading files and crawling sites

Uploads from the terminal use an ingest key (`mb_in_…`). Put it in your shell profile and restart
Claude Code:

```bash
echo 'export MABRAIN_API_KEY=mb_in_...' >> ~/.zshrc
```

Without it you can still add pasted text and single pages. The crawler (`python3`, no dependencies)
sends the pages' literal HTML; nothing goes through a model on the way. If Claude Code runs
sandboxed, allow the site's domain and `api.mabra.in`.

## For your app

Your app uses a read key (`mb_ro_…`) and the API at `https://api.mabra.in`. The full contract for
models is at [api.mabra.in/llms.txt](https://api.mabra.in/llms.txt), and ready-to-paste tool
definitions at [api.mabra.in/v1/tools](https://api.mabra.in/v1/tools). `/mabrain:connect-agent` writes
the integration for you.

## License

MIT
