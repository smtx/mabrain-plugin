# MaBrain for Claude

Give your app a brain, from Claude Code. Add your docs, pages or a whole site; ask it and get answers
that cite their source; fix what it gets wrong; plug it into your own agent with a few lines of code.

> Every GitHub account older than 30 days gets a brain and 5 $ of free credit a month. Asking is free.

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

The first time you upload a file or crawl a site, Claude runs the bundled crawler's `login`: your
browser opens, you sign in with GitHub, and that machine stays signed in. No key to copy. The crawler
(`python3`, no dependencies) sends the pages' literal HTML; nothing goes through a model on the way.
If Claude Code runs sandboxed, allow the site's domain and `api.mabra.in`.

## For your app

Your app uses a read key (`mb_ro_…`) and the API at `https://api.mabra.in`; `/mabrain:connect-agent`
creates it for you and writes it into your project's `.env` without showing it. By default your agent
asks without spending credit (`mode: "search"`). The full contract for
models is at [api.mabra.in/llms.txt](https://api.mabra.in/llms.txt), and ready-to-paste tool
definitions at [api.mabra.in/v1/tools](https://api.mabra.in/v1/tools). `/mabrain:connect-agent` writes
the integration for you.

## License

MIT
