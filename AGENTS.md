# AGENTS.md — slackwright

This file is the **universal entry-point for AI-enabled IDEs** (Cursor,
Claude Code, OpenAI Codex, GitHub Copilot, and any other tool that
respects the [`AGENTS.md`](https://agents.md/) convention). Read it on
every turn; it is intentionally short and points at the canonical
sources.

## What slackwright is

A small (~3 kLoC) browser-driven Slack message extractor written in
Python on top of Playwright. Apache 2.0, single-author OSS. Uses the
user's own logged-in Slack web session (via Playwright) to call
Slack's internal `search.modules.messages` endpoint, bypassing the
bot-scope and exclusion-list limits imposed on Slack apps and MCP
integrations.

User-facing surface:

```bash
./slackwright login --workspace https://acme.slack.com   # one-time
./slackwright fetch  --from me --days 14                 # search + extract
./slackwright whoami | resolve | doctor                  # auxiliaries
./slackwright install | test | lint | fmt | shell | clean  # dev workflow
```

## Read in order, on every turn

1. **[`ai/dev.agent.md`](ai/dev.agent.md)** — the actual rules: who
   you are, how the maintainer wants the project built, the commit and
   test discipline, when to ask vs. just-do-it. **This is your
   primary instruction file.**
2. **[`ai/dev.memory.md`](ai/dev.memory.md)** — accumulated maintainer
   preferences (workflow shortcuts, gotchas, conventions). Treat each
   entry as a hard rule unless overridden in the current turn.
3. **[`ai/spec.txt`](ai/spec.txt)** — canonical specification of what
   slackwright does (architecture, on-disk layout, CLI surface, edge
   cases). Consult before adding, changing, or removing behavior.

`ai/dev.agent.md` itself opens with the "read in this exact order"
list, so the chain is self-reinforcing — start there.

## Bootstrap and run

Use the `./slackwright` script for everything. Never bootstrap the
venv manually:

```bash
./slackwright install        # ensure venv + deps + Chromium (idempotent)
./slackwright test           # pytest
./slackwright lint           # ruff check
./slackwright fmt            # ruff check --fix
./slackwright fetch ...      # forwarded to the Python CLI
```

Reserved dev-workflow names: `install / test / lint / fmt / shell /
clean / help`. Anything else is forwarded to the slackwright Python
CLI as-is.

## IDE-specific notes

- **Cursor** picks up `AGENTS.md` automatically (and any
  `.cursor/rules/*.mdc` files, none of which are present here).
- **Claude Code** picks up `CLAUDE.md` if present; absent that, it
  reads `AGENTS.md`. Slackwright ships only this file — both work.
- **OpenAI Codex / Codex CLI** reads `AGENTS.md` per the published
  spec.
- **GitHub Copilot** reads `.github/copilot-instructions.md` if
  present; for slackwright the canonical instructions live here, so
  link or import this file when configuring Copilot for the repo.

If you add another IDE-specific shim later, keep it as a thin
forwarder to `ai/dev.agent.md` rather than duplicating content.
