# Slackwright - spec <!-- omit in toc -->

- [Overview](#overview)
- [Problem and solution](#problem-and-solution)
- [Scope](#scope)
- [Architecture](#architecture)
- [Output layout (the on-disk contract)](#output-layout-the-on-disk-contract)
- [CLI surface](#cli-surface)
- [Exit codes (stable contract)](#exit-codes-stable-contract)
- [Slack search query syntax](#slack-search-query-syntax)
- [Authentication details](#authentication-details)
- [Resolver details](#resolver-details)
- [File download details](#file-download-details)
- [Edge cases and limitations](#edge-cases-and-limitations)
- [Test strategy](#test-strategy)
- [Non-goals / deliberate decisions](#non-goals--deliberate-decisions)
- [Open questions](#open-questions)
- [Future work](#future-work)

> Current specification - the single source of truth for what Slackwright is, what it does, and how it does
> it. Kept **self-sufficient**: it reads standalone. Updated through dialogue during planning and
> development. Version of record: **0.3.1** (alpha/beta), Apache 2.0, `github.com/myurasov/Slackwright`. The
> version string is mirrored in `source/pyproject.toml`, `source/src/slackwright/__init__.py`, and this
> header - keep them in sync.

## Overview

Slackwright is a small Python CLI (src layout package `slackwright`) that extracts Slack messages using the
user's own logged-in web session - no bot scopes, no Slack app install. **Login** drives a real Chromium
window through Playwright (so SSO / MFA / SAML work normally); **every subsequent call** (search, lookups,
file downloads) is a plain `httpx` HTTP request that replays the captured cookie jar + `xoxc-` token - no
browser process. It is **read-only by design** (it never writes to Slack) and **OSS, Apache 2.0,
single-author** (Mikhail Yurasov). It is pip-installable (hatchling) and also drivable via the `./slackwright`
wrapper.

## Problem and solution

**Problem.** Official Slack bot apps and most "Slack MCP" integrations are limited by the bot scopes a
workspace admin grants: the bot only sees DMs/MPIMs/channels it is explicitly added to; externally-shared
channels are usually excluded by org policy; per-tenant exclusion lists block whole channels; the result-set
cap is tighter than the web client's. So "fetch every message I sent in the last 30 days" is impossible
through normal bot integrations for most users.

**Solution.** Slackwright drives a real, headed Chromium window through Playwright, lets the user log in to
slack.com normally (SSO, MFA, hardware keys - whatever the org requires), and then uses the resulting
authenticated browser context to call Slack's internal web-search endpoint
`<workspace>/api/search.modules.messages` - the same endpoint the desktop client uses. Anything readable in
slack.com is reachable here, with no bot scopes involved.

## Scope

**In scope:** read-only message extraction from workspaces the user can already log into; search-style queries
(from / to / with / in / before / after / on / during, plus arbitrary keyword text); resolution of Slack IDs
to human names + emails, channel names, etc.; optional download of message attachments; output as a sharded
per-message JSON tree + YAML caches that downstream tools consume without conversion.

**Out of scope:** posting / editing / reactions / status / any write to Slack; bypassing 2FA, captchas, or any
auth challenge; fetching from workspaces the user isn't a member of; real-time streaming / event
subscriptions (this is a search-style extractor, not a bot); re-implementing the Slack desktop client.

## Architecture

A small Python package (`src/slackwright/`, src layout) plus a CLI. Modules are thin and orthogonal so they
unit-test in isolation:

```
src/slackwright/
  __init__.py       public surface + version + __all__
  __main__.py       enables `python -m slackwright`
  paths.py          state-dir resolution (~/.cache/slackwright/ default)
  auth.py           login flows (headed / non-interactive / chrome-cookie) + token persistence +
                    refresh_token_headless + LoginSession + TokenRefreshError
  chrome_cookies.py read + decrypt local Chrome cookies off disk (macOS; via cryptography) and write them
                    into Playwright storage-state shape - powers `login --use-chrome` without launching Chrome
  client.py         Slack web client over plain httpx (browserless); retry/backoff; two-tier token refresh;
                    cookie-jar <-> playwright-state.json round-trip; CostTracker hooks
  cost.py           CostTracker (api calls, bytes, retries, rate-limit time)
  lock.py           StateLock (cross-process flock on the state dir)
  resolver.py       id / email / handle / name -> Slack entity resolution
  search.py         SearchPlan + paginated runner + month chunker + timeout
  files.py          attachment downloader
  archive.py        sharded JSON tree + YAML caches + matches.jsonl + read_index; ARCHIVE_SCHEMA
  report.py         self-contained HTML report renderer (canonical reader of the on-disk layout)
  schema.py         JSON schema of the CLI tree (--schema)
  result.py         Result + ExitCode + structured-error envelope
  progress.py       stderr progress display (--quiet aware)
  cli.py            argparse entry-point dispatching to the modules above

examples/           runnable Python embedding snippets (01_fetch_in_process, 02_resolve_only,
                    03_explain_no_network, 04_render_report)
tests/              pytest suite (see Test strategy)
slackwright         bash helper: dev workflow (install/test/lint/fmt/shell/clean/help) + CLI forwarding
pyproject.toml      hatchling build; [project.scripts] slackwright = "slackwright.cli:main"; pytest + ruff
```

**Dependencies.** Runtime: `playwright>=1.41,<2.0`, `PyYAML>=6.0`, `httpx>=0.27,<1.0`, `cryptography>=41.0`.
Dev: `pytest>=7.4`, `pytest-asyncio>=0.23`, `ruff>=0.4`. Python >= 3.10. Must stay installable on plain pip
across macOS / Linux / Windows with no compilation step.

**Data flow.** (1) `login` opens headed Chromium at the workspace; once `window.boot_data.api_token` appears,
extract it + the cookie jar and persist Playwright storage state (`<state-dir>/playwright-state.json`). (2)
`fetch` opens a long-lived `httpx.Client` whose cookie jar is hydrated from that storage state (no browser);
all API calls are form-encoded POSTs to `<api_url>/<method>` with the `xoxc-` token in the body. Rotated
`Set-Cookie` values are merged into the jar and persisted back to disk on exit. (3) `EntityResolver`
translates `--from/--to/--with/--in` into
Slack IDs/handles; first missing-name lookup triggers a one-time `users.list` / `conversations.list` pull
(cached on disk). (4) `SearchRunner` builds the query (`from:@handle in:#channel after:YYYY-MM-DD ...`),
paginates all results, slices date ranges by month so each chunk stays under Slack's 100x100 cap; cross-chunk
dedup on `(channel_id, ts)`. (5) `ArchiveWriter` writes each match in the canonical layout, appending to
`matches.jsonl` as it goes. (6) with `--with-files`, `FileDownloader` walks every match's `files` array. (7)
the resolver fills in any user/channel IDs seen but not cached, then writes `_users/` + `_channels/` YAMLs.
(8) `ArchiveWriter` writes `_index.yaml` summarising counts + the search plan + truncation warnings.

## Output layout (the on-disk contract)

`--format archive` (default; the only format producing the full sharded tree):

```
<out>/
  messages/YYYY/MM/DD/YYYY-MM-DD-<chan-slug>-<hash8>.json
  _users/<U_id>.yaml
  _channels/<C_id>.yaml
  _files/<F_id>/<safe_filename>      # only with --with-files
  _files/<F_id>/_meta.json
  _index.yaml
  matches.jsonl
  report.html                        # only after `slackwright report`
```

`<hash8>` = first 8 hex of `sha256("<channel_id>:<ts>")`; the full 64-hex digest is the dedup source of
truth (the 8-char prefix only disambiguates same-day same-channel filenames). `YYYY-MM-DD` is the message ts
in the user's local timezone. Alternative formats: `--format jsonl` (only `matches.jsonl`), `--format raw`
(unprocessed Slack response objects under `_raw/`).

**Per-message JSON shape:** every field Slack's search endpoint returns is preserved verbatim, plus two
additions: `channel_id` (re-injected so the `(cid, ts)` key survives without filename context) and `_archive`
(`captured_at`, `direction` in/out, `archive_schema: 2`, `source_tool: slackwright`, `thread_ts`,
`search_plan`). The single-underscore prefix ensures it can never shadow a future Slack-native field.

**YAML caches:** `_users/<id>.yaml` (id, name, real_name, display_name, email, title, team_id, deleted,
is_bot, captured_at); `_channels/<id>.yaml` (id, name, type channel|im|mpim|group, is_private, is_archived,
topic, purpose, user for IMs, members, captured_at). **`_index.yaml`** summarises `schema_version`, `tool`,
`last_updated`, `captured_at`, `format`, `plan`, `query`, `counts`, `cost`, and `extra` (incl.
`search_stats.chunks_completed`, which `--resume` reads to skip finished work).

**Idempotency:** re-running over an existing tree merges cleanly per message (missing -> created; same raw
payload -> noop; changed -> updated). `matches.jsonl` is truncated at the start of every run (per-run ledger,
not append-only).

## CLI surface

```
slackwright [--state-dir DIR] [--json] [-q/--quiet] <subcommand> ...
slackwright --schema     # JSON schema of every subcommand + flag
slackwright --version    # print version and exit

# global options
--state-dir DIR   override ~/.cache/slackwright/ (or $SLACKWRIGHT_STATE_DIR)
--json            emit one JSON Result document on stdout (machine mode)
-q, --quiet       suppress stderr progress (the JSON envelope is unaffected)

# subcommands
login --workspace WORKSPACE [--timeout SECONDS] [--executable-path PATH]
      [--use-chrome] [--chrome-profile DIR] [--profile-directory NAME]
      [--copy-profile | --no-copy-profile]
      [--token xoxc-... --cookie-d xoxd-...] [--user-id ID --user-email E --team-id T]
whoami                                  # print saved login bundle (token redacted)
doctor                                  # probe auth.test; print user + team; carries a cost block
resolve <value> [--kind {auto,user,channel}]
fetch [--from PERSON] [--to PERSON] [--with PERSON] [--involves PERSON] [--in CHANNEL]
      [--query "extra terms"] [--days N | --since YYYY-MM-DD [--until YYYY-MM-DD]]
      [--max N] [--out DIR] [--with-files] [--format {archive,jsonl,raw}]
      [--explain] [--dry-run] [--resume] [--stream-json] [--timeout SECONDS] [--no-refresh] [-v]
describe-archive <path>                 # read an output dir; emit _index.yaml + file counts as JSON
report <path> [--out FILE] [--title TITLE]
```

`fetch` defaults: `--format archive`, `--out ./slackwright-out`, no max, auto-refresh on. `--involves PERSON`
is a shortcut for `--from PERSON` + `--to PERSON` unioned in one run (conflicts with `--from/--to/--with`).
`--explain` (alias `--dry-run`) resolves filters from cache (no network when possible) and emits the rendered
query + chunk schedule as JSON without writing. `--resume` reads `--out`'s `_index.yaml` and skips finished
chunks. `--stream-json` emits one JSON match per line on stdout as matches arrive. `--timeout N` aborts after
N seconds; partial output stays on disk and is resumable (returns `transient_api`). `--no-refresh` disables
the on-`invalid_auth` headless token refresh (see Authentication details).

**PERSON args** (`--from/--to/--with`): a U-id (as-is); an email (via `users.lookupByEmail`); an `@handle`
(matches `name` via cache); a real/display name (case-insensitive, must be unambiguous); literal
`me`/`myself`/`self`. **CHANNEL args** (`--in`): channel name (with/without `#`); a C/D/G-id (as-is).

## Exit codes (stable contract)

`0 ok`, `2 usage`, `3 no_login`, `4 resolution_failed`, `5 transient_api`, `6 permanent_api`, `7 io`,
`130 interrupted`. Numeric values are stable across versions; symbolic names live in
`slackwright.result.ExitCode`; tests compare to the enum, not the int. The full table (with remediation
hints) is reachable via `slackwright --schema`.

## Slack search query syntax

CLI flags map to Slack's standard search operators: `--from -> from:@handle` (or `from:<U-id>` fallback),
`--to -> to:@handle`, `--with -> with:@handle`, `--in -> in:#slug` (or `in:<C-id>`),
`--since DATE -> after:<DATE-1day>` (Slack's `after:` is exclusive), `--until DATE -> before:<DATE+1day>`
(exclusive), `--query "..." -> appended verbatim`. Date inputs accept `YYYY-MM-DD`, `YYYY/MM/DD`, `YYYYMMDD`.

**Pagination cap:** Slack caps any single search at 100 pages x 100 results = 10000. When a date range
overflows, Slackwright slices it into per-month chunks (descending, so recent matches surface first) and
de-dupes across them on `(channel_id, ts)`. If a single month still hits the cap, a warning is recorded in
`_index.yaml`'s `search_stats.truncated_chunks` - the caller should narrow with `--in / --from / --query` and
re-run.

## Authentication details

Three login paths, all producing the same persisted state (`<state-dir>/auth.json` + `playwright-state.json`,
both chmod 600). **Interactive** (default, `login --workspace WORKSPACE`): normalize the workspace
(`acme` -> `https://acme.slack.com`), open a fresh Chromium context there, poll `page.evaluate(...)` every
second for `window.boot_data.api_token` (falling back to `localStorage.localConfig_v2.teams[*].token`), and on
a plausible `xoxc` token capture workspace_url / api_url / api_token / team_id / enterprise_id / user_id /
user_name / user_real_name / user_email / extracted_at into `auth.json` + the Playwright storage state.
**Non-interactive** (`login --token xoxc-... --cookie-d xoxd-... [...]`): skips the headed flow; constructs
the storage state from the `d` cookie (scoped to `.slack.com`, HttpOnly + Secure), validates token starts
`xox<letter>-` and cookie starts `xoxd-`, persists the same files. **Chrome-cookie** (`login --use-chrome`,
or `--chrome-profile DIR [--profile-directory NAME]`): reads + decrypts the user's existing Slack cookies
straight off disk from their system Chrome (macOS only today, via `cryptography`) and writes them into the
Playwright storage-state shape - no Chrome launch, dodging the macOS keychain/headed-window problems.
`--use-chrome` is a shortcut that points `--chrome-profile` + `--executable-path` at the system Chrome
defaults and enables `--copy-profile` (snapshot the profile to a private state-dir copy, skipping caches, so
the user's Chrome can stay open); `--no-copy-profile` uses the profile in place (Chrome must be quit first).

**HTTP transport (browserless):** after login, every API call is a `httpx.Client` form-encoded POST to
`<api_url>/<method>` (the xoxc token in the body as `token=...`). The cookie jar - including the HttpOnly
`d`-family cookies - is hydrated from `playwright-state.json` via `_build_cookie_jar`; rotated `Set-Cookie`
values are auto-merged and persisted back on context exit so long sessions keep their refreshed `d-s`. Adds
`Origin` + `Referer` headers (workspace origin) and a Chromium-shaped `User-Agent` to look like the web
client. Retries: 5-step exponential backoff (5s, 15s, 30s, 60s, 120s) on 429 / 5xx / transport errors;
ratelimited responses (HTTP 429 or `ok:false ratelimited`) honor `Retry-After`. Every call increments the
`CostTracker`.

**Token auto-refresh (two-tier).** On an `invalid_auth`-class error (`invalid_auth`, `not_authed`,
`token_revoked`, `account_inactive`, `token_expired`), `api()` attempts one refresh per call: first
`refresh_token_headless` (a short-lived headless Chromium re-reads `boot_data.api_token` - works when only the
bearer token rotated but cookies are still good); if that raises `TokenRefreshError` (typical for
Enterprise-Grid SAML where the IdP needs a user gesture) and stdin+stderr are TTYs, it falls back to a full
interactive `LoginSession`. Non-TTY shells (cron/CI) surface the original error instead. `fetch --no-refresh`
disables this entirely. On success the on-disk auth + storage state are rewritten and the in-memory jar +
bundle reloaded before the request retries.

**Concurrency:** every state-dir mutation goes through `StateLock` (fcntl/msvcrt advisory lock at
`<state-dir>/.slackwright.lock`); acquire failure returns `io` with `error: lock_timeout`.

## Resolver details

`EntityResolver` classifies an input: Slack user/bot id `^[UWB][A-Z0-9]{6,}$` -> use as-is; channel id
`^[CDG][A-Z0-9]{6,}$` -> as-is; email -> `users.lookupByEmail`; literal `me/myself/self/i` -> bundle.user_id;
leading `@` -> strip and continue; else name lookup. **Name lookup** tries the cached @handle index, then
display-name, then real-name (case-insensitive exact); on miss, a one-time paginated `users.list` (cached),
then re-tries; last resort is case-insensitive substring on real_name + display_name - multiple substring
hits raise `LookupError` with candidates (never silently picks). Channel lookup works the same against
`conversations.list`. **Cache-only mode** (`client=None`): ID-shaped inputs return a stub record with no
network; named inputs raise `LookupError` if uncached - this powers `fetch --explain`. **Caches:**
`<state-dir>/users.json`, `channels.json`, `handle-index.json` (reverse indices + `users_listed` /
`channels_listed` flags).

## File download details

With `--with-files`: walk each match's `files` array and each attachment's nested `files`; skip incomplete
entries (`mode == "tombstone"`, missing id, missing `url_private*`); for each unique file id write
`<out>/_files/<F_id>/<safe_filename>` + `_meta.json`. Idempotent (existing non-empty files skipped,
`_meta.json` always re-written); per-file errors are recorded in stats but don't abort the run.

## Edge cases and limitations

- Slack search hard cap is 10000 results/query - chunking by month helps but doesn't eliminate; narrow with
  `--in / --from / --with` for very high-volume conversations.
- Edited/deleted messages: search returns the latest content Slack exposes; there is no full edit-history API.
- Bot messages usually have no `user` field (only `bot_id`) - the resolver short-circuits with a synthetic
  record to avoid `users.info` lookups.
- IM channel `name` is sometimes the other user's id - the channel-slug helper detects this and falls back to
  `im-<short_id>`.
- Enterprise Grid: `https://<workspace>.enterprise.slack.com` must be passed in full (the short-name shortcut
  assumes the non-Grid form).
- `search.modules.messages` is the web client's endpoint - stable in practice but not part of Slack's public
  API contract; a response-shape change may require an update.

## Test strategy

pytest-driven, runs in a few seconds; baseline **194 tests across 11 files**. Live Playwright tests are NOT
in the default suite (need real credentials). `tests/conftest.py` supplies a `FakeClient` replacing
`SlackWebClient` (canned responses + dynamic handlers; records every `api(method, params)` call). Use
realistic Slack ids (`UALICE00`, `CGENERAL`) - synthetic underscored ids fail the resolver regex. Pure
helpers are unit-tested directly; resolver / search runner / archive writer / file downloader / schema
introspector / HTML renderer / Result + ExitCode / StateLock / CostTracker each have their own file;
`test_integration.py` wires resolver + runner + writer end-to-end; `test_cli.py` exercises only the argparse
layer; `test_lock.py` uses multiprocessing with a sentinel-file handshake. Behavior-changing PRs MUST add or
update tests; new response shapes -> a fixture in `conftest.py` (never live Slack); new exit codes ->
`test_result.py`; new `--json` fields -> a `to_json()` round-trip test.

Test files: `test_archive.py`, `test_auth.py`, `test_cli.py`, `test_cost.py`, `test_files.py`,
`test_integration.py`, `test_lock.py`, `test_report.py`, `test_resolver.py`, `test_result.py`,
`test_schema.py`, `test_search.py`.

## Non-goals / deliberate decisions

- **argparse over click** - stdlib-only, CLI surface small enough.
- **httpx (browserless) for API calls; Playwright only for login** - earlier versions drove every call
  through `page.request` inside a launched Chromium to share the real browser cookie jar; that was robust but
  heavy (a hidden Chromium per fetch just to issue form POSTs). The auth-replay problem is now solved by
  hydrating an `httpx.Cookies` jar from `playwright-state.json` (incl. HttpOnly cookies), so plain httpx
  suffices - Slack accepts the same cookies + token regardless of who holds the socket. Don't reintroduce a
  per-call browser; login (SAML/MFA/SSO) is the only flow that still needs one.
- **sync over async** - the code stays linear; don't async-ify without a measured benefit.
- **No `slackdump` dependency** - single-process Python tool, no compiled-binary deps.

## Open questions

- _(none open)_ The import-time spec/code drift - httpx browserless transport, the `--use-chrome`
  Chrome-cookie login path, and the `httpx` + `cryptography` deps - was reconciled on 2026-06-14 (v0.3.0).
- **Chrome-cookie path is macOS-only.** Linux (libsecret) and Windows (DPAPI) cookie decryption are
  straightforward to add in `chrome_cookies.py` when there's demand.

## Future work

(Rough buckets - refine before picking up.) MCP server (`slackwright mcp serve`) exposing every subcommand as
an MCP tool; idempotency keys (`--run-id <uuid>`) for retry correlation; per-channel `last_seen` incremental
sync (today's `--resume` is chunk-granular); thread expansion (opt-in `--thread-replies` calling
`conversations.replies`); multi-workspace support in one state dir; export adapters (ndjson stream, parquet
writer/reader); Windows + Linux CI smoke-tests (currently macOS-only validated); PyPI release via GitHub
Actions trusted publishing.
