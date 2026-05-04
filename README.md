# slackwright

> Browser-driven Slack message extractor that uses **your own logged-in
> Slack web session** to bypass the bot-scope and exclusion-list limits
> imposed on official Slack apps and MCP integrations.

`slackwright` is a small CLI built on top of [Playwright](https://playwright.dev/)
and Slack's internal web-search endpoint. It works against any workspace
you can already log into — including Enterprise Grid orgs — and reaches
**every channel, DM, and MPIM you can read in the Slack UI**, not just
the subset a bot or MCP token is granted.

Output is a clean message archive (one JSON per message, plus YAML caches
for users and channels) suitable for indexing, replaying, or feeding into
downstream tooling. File attachments are downloaded on request, and the
search query syntax accepts every standard Slack operator (`from:`,
`to:`, `with:`, `in:`, `before:`, `after:`, `during:`, free-text).

## Why?

The official Slack `search.messages` API behind bot apps (and Slack MCP
integrations like NVIDIA's MaaS Slack server) typically:

- only sees DMs / MPIMs / channels the bot is explicitly added to,
- excludes externally-shared channels by org policy,
- enforces per-tenant exclusion lists at the API gateway,
- caps result fan-out tighter than the web client.

`slackwright` sidesteps all of that by being you. It launches a real
Chromium window, you log in once with whatever flow your org requires
(SSO, MFA, hardware keys, …), and from then on it drives the same web
session the desktop client uses — so anything you can read in slack.com
is reachable here.

## Install

`slackwright` is pure Python (≥3.10). It depends on Playwright (which
brings its own Chromium) and PyYAML.

The repo ships a single helper script that handles bootstrap (venv,
dependencies, and the Playwright Chromium download) on first use:

```bash
git clone https://github.com/myurasov/Slackwright.git slackwright
cd slackwright
./slackwright install
```

`./slackwright install` requires [`uv`](https://docs.astral.sh/uv/) on
your `PATH` (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`).
The first run downloads ~150 MB of Chromium into Playwright's cache;
subsequent calls skip the bootstrap unless `pyproject.toml` changed.

If you'd rather install slackwright system-wide via pip:

```bash
pip install -e .[dev]
playwright install chromium
slackwright login --workspace https://acme.slack.com
```

## Quick start

Every CLI invocation works either as `./slackwright <args>` (auto-
bootstraps in the project venv) or as `slackwright <args>` (when
installed via pip):

```bash
# 1. One-time login (opens a real Chrome window — sign in normally).
./slackwright login --workspace https://acme.slack.com

# 2. Fetch every message you sent in the last 14 days.
./slackwright fetch --from me --days 14 --out ./out

# 3. Fetch every DM with Bob, with attachments.
./slackwright fetch --with bob.builder --with-files --out ./bob

# 4. Search a specific channel for keyword text.
./slackwright fetch --in '#engineering' --query 'rollback OR incident' \
                    --since 2026-04-01 --until 2026-04-30 --out ./incidents

# 5. Cross-pollinate: messages from Carla to Alice, in the last 30 days.
./slackwright fetch --from carla@example.com --to me --days 30 --out ./carla-to-me
```

Run `./slackwright --help` and `./slackwright <subcmd> --help` for the
full flag list.

## Person and channel arguments

Every `--from`, `--to`, `--with` and `--in` argument accepts whatever
form is most convenient for you:

| Form                      | Example                | Notes                                |
|---------------------------|------------------------|--------------------------------------|
| Slack ID                  | `U06HYSK2P2L`          | Used as-is, no resolution.           |
| Email                     | `alice@example.com`    | Resolved via `users.lookupByEmail`.  |
| `@`-handle                | `bob.builder`          | Matches the Slack `name` field.      |
| Real or display name      | `Alice Engineer`       | Case-insensitive; must be unique.    |
| `me` / `myself` / `self`  | `me`                   | The logged-in user.                  |
| Channel name              | `engineering` or `#engineering` | Public/private channel.    |
| Channel/DM/MPIM ID        | `C07SC7AFW7Q` etc.     | Used as-is.                          |

The first time `slackwright` needs to resolve a name it issues a single
paginated `users.list` (or `conversations.list`) call and caches the
result under `~/.cache/slackwright/`. Subsequent runs reuse the cache.

If a name is ambiguous (multiple users share a substring) the tool fails
loudly with the candidates listed — it never silently picks one.

## Date filtering

```bash
--days N                 # last N days (today → today − N), inclusive
--since YYYY-MM-DD       # inclusive lower bound
--until YYYY-MM-DD       # inclusive upper bound (default: today)
```

`--days` and `--since/--until` are mutually exclusive. Both forms accept
`YYYY-MM-DD`, `YYYY/MM/DD`, or `YYYYMMDD`.

Slack's search caps any single query at 10 000 results (100 results × 100
pages). For ranges that overflow the cap, slackwright slices the window
into per-month chunks and de-duplicates across them. Truncations are
warned about in `_index.yaml` so you know to re-run with a narrower
window.

## Output layout

Default `--format archive` (drop-in compatible with Slack-style archives):

```
<out>/
├── messages/2026/04/25/2026-04-25-engineering-1a2b3c4d.json
├── messages/2026/04/25/2026-04-25-im-34n8j8p6-aa11bb22.json
├── _users/U06HYSK2P2L.yaml
├── _channels/C07SC7AFW7Q.yaml
├── _files/F09ABCD/screenshot.png        # only when --with-files
├── _files/F09ABCD/_meta.json
├── _index.yaml                          # run summary + counts
└── matches.jsonl                        # one row per match (slim ledger)
```

Per-message JSON files contain the **raw Slack search match** plus an
`_archive` sidecar (`captured_at`, `direction`, `archive_schema`,
`source_tool`, `thread_ts`, `search_plan`). YAML caches under
`_users/` and `_channels/` resolve every Slack ID encountered to the
human-readable name, real-name, email, and channel topic/purpose.

Other formats:

- `--format jsonl` — only `matches.jsonl` (slim ledger, one match per
  line). Useful for grepping or feeding a downstream pipeline.
- `--format raw` — raw Slack response objects under `_raw/`, no
  post-processing. Useful for forensic inspection of the API.

## Headless vs headed

By default `slackwright fetch` runs Chromium **headless** — the browser
window stays hidden, the script just streams progress to stderr.

Use `--headed` if you want to watch the scrape happen (debugging) or if
your org's auth path occasionally requires an interactive prompt that a
headless browser can't satisfy.

`slackwright login` is **always** headed — you need to type things into
the login form yourself.

## Other commands

```bash
slackwright whoami            # show the logged-in user info (sanity check)
slackwright doctor            # call auth.test against the saved session
slackwright resolve alice     # show what an arg resolves to (debugging)
slackwright resolve '#general' --kind channel
```

## Privacy and data location

- All credentials (cookies + xoxc token) live under
  `~/.cache/slackwright/` (override via `--state-dir` or
  `$SLACKWRIGHT_STATE_DIR`). `auth.json` is mode 0600.
- Output files are whatever the user-supplied `--out` directory holds.
  No data is sent anywhere except to Slack.
- `slackwright` keeps no telemetry, makes no third-party network calls,
  and does not phone home on launch.

## Limitations

- **Slack's search cap** (10 000 results / query) applies. The chunker
  slices by month; if a single month exceeds the cap, narrow the query
  with `--in` / `--from` / `--query` and re-run.
- **Edited / deleted messages** appear with the latest content Slack
  returns. Slack does not expose a full edit history through search.
- **Rate limits**: Slack tolerates a few hundred search calls per minute
  from a normal user session. Backoff is automatic but a multi-thousand
  message fetch will take minutes, not seconds.
- **Unofficial endpoint**: `search.modules.messages` is the same endpoint
  the web client uses, so it's stable in practice — but it's not part
  of Slack's public API contract. If Slack changes the response shape
  someday, slackwright may need an update.

## Status

`slackwright` is alpha-quality and used in production by its author
(@myurasov) for personal Slack archive needs. The output schema is
stable and intentionally compatible with common Slack-archive layouts
(per-message JSON keyed by `(channel_id, ts)`, YAML user/channel
caches), so it round-trips cleanly through downstream tooling. Public
API is not yet frozen — minor refactors expected before 1.0.

## Development

Common tasks all go through the same `./slackwright` helper:

```bash
./slackwright install        # bootstrap venv + deps + Chromium
./slackwright test           # pytest
./slackwright lint           # ruff check
./slackwright fmt            # ruff check --fix
./slackwright shell          # subshell with the venv activated
./slackwright clean          # remove .venv + caches
```

Reserved dev-workflow names: `install / test / lint / fmt / shell /
clean / help`. Anything else is forwarded to the slackwright Python CLI.

If you (or your AI coding assistant) plan to make changes, read the
project's agent instructions first:

- [`AGENTS.md`](./AGENTS.md) — universal entry-point for AI-enabled
  IDEs (Cursor, Claude Code, OpenAI Codex, Copilot, etc.).
- [`ai/dev.agent.md`](./ai/dev.agent.md) — the maintainer's rules for
  evolving slackwright (code style, commit discipline, test policy).
- [`ai/spec.txt`](./ai/spec.txt) — canonical specification of what
  slackwright does (architecture, on-disk layout, CLI surface).
- [`ai/dev.memory.md`](./ai/dev.memory.md) — accumulated maintainer
  preferences. Append new entries here when conventions change.

## License

Apache 2.0, see [LICENSE](./LICENSE).
