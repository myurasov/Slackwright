# Engineer instructions - Slackwright <!-- omit in toc -->

- [Identity](#identity)
- [Build / run / test](#build--run--test)
- [Code style](#code-style)
- [CLI envelope + exit codes (public contract)](#cli-envelope--exit-codes-public-contract)
- [On-disk archive schema (public contract)](#on-disk-archive-schema-public-contract)
- [Cost / observability discipline](#cost--observability-discipline)
- [Concurrency](#concurrency)
- [Slack API discipline](#slack-api-discipline)
- [Test discipline](#test-discipline)
- [Lint](#lint)
- [Dependencies](#dependencies)
- [Commits + publication](#commits--publication)
- [Workflow for non-trivial changes](#workflow-for-non-trivial-changes)
- [When to ask vs just-do-it](#when-to-ask-vs-just-do-it)

Editable, project-specific notes on how to develop this project. Rewrite freely to keep the best version
(not append-only). The commit and safety policies live in `engineer.agent.md`.

**Shareable layer.** This file sits in `ai/` alongside `engineer.agent.md` and `spec.md` - the portable,
shareable "how to develop this project" layer. Keep it free of anything environment-specific or sensitive:
**no** hostnames, IPs, workspace names, internal/corporate URLs, tokens, or cookies. Those live in the
private `ai/memory/` layer (`resources.md`, `credentials.md`, `context.md`).

## Identity

Maintain and extend **Slackwright** - a small (~17 modules, src layout) Slack message extractor in Python.
**Login** drives a real Chromium window through Playwright so the user signs in to slack.com normally
(SSO / MFA / hardware keys); **every subsequent call** to Slack's internal web-search endpoint
(`search.modules.messages`) and friends is a plain `httpx` request replaying the captured cookie jar +
`xoxc-` token - no browser process, no bot scopes. It is **OSS, Apache 2.0, single-author** (Mikhail
Yurasov), `github.com/myurasov/Slackwright`. Treat it as a polished public artifact. **Read-only by design** -
it never posts, edits, reacts, or writes to Slack; never add code that does.

## Build / run / test

Run everything through the `./slackwright` wrapper from `source/`. **Never** bootstrap the venv manually and
never run `uv` / `pytest` / `ruff` / `playwright` directly - the wrapper handles bootstrap, the iCloud `.pth`
workaround, and Playwright Chromium installation idempotently.

```
./slackwright install         # ensure venv + deps + Playwright Chromium
./slackwright test [args...]  # pytest (use -q; baseline ~194 tests across 11 files, runs in seconds)
./slackwright lint            # ruff check
./slackwright fmt             # ruff check --fix + ruff format
./slackwright shell           # subshell with venv activated
./slackwright clean           # remove .venv + caches
./slackwright help            # help text
```

Reserved dev-workflow verbs: `install / test / lint / fmt / shell / clean / help`. Anything else forwards to
the Python CLI (`./slackwright login | fetch | resolve | whoami | doctor | describe-archive | report`, plus
global flags `--schema | --json | -q`). There is **no** `make`, `tox`, or `pre-commit`; new workflow verbs
are added as reserved cases in the `./slackwright` script, not as parallel tools. The wrapper's `show_help()`
prints a fixed line-range of its own header - if you reorder that comment block, update the line numbers.

Unlike some sibling repos, Slackwright **is** pip-installable (hatchling build backend, `[project.scripts]
slackwright = "slackwright.cli:main"`, package at `src/slackwright`). Keep it cleanly installable on plain
pip across macOS / Linux / Windows with no compilation step.

## Code style

- **Python 3.10+ only.** `from __future__ import annotations` atop every module. PEP-604 unions
  (`X | None`, `X | Y`) - no `typing.Optional` / `Union`.
- **Type hints everywhere** - public functions and dataclasses fully typed; internal helpers too unless
  truly noisy.
- **Dataclasses over dicts** for any structured value with more than two fields or crossing module
  boundaries.
- **Small modules, one responsibility, cap ~600 lines.** Propose a new module (in chat) before exceeding it
  or before adding a file that doesn't fit an existing module. No `workspace/`-style scratch dirs - everything
  lives under the project root.
- **Comments explain *why*, not *what*.** Skip narration comments (the maintainer is opinionated here).
- **Apache 2.0 copyright header on every new `.py`** (copy from any existing module).
- argparse over click; sync over async; **httpx (browserless) for API calls, Playwright only for login** -
  the cookie jar (incl. HttpOnly `d`-family cookies) is hydrated from `playwright-state.json` via
  `client._build_cookie_jar`. Don't reintroduce a per-call browser. See `spec.md` non-goals before changing
  any of these.

## CLI envelope + exit codes (public contract)

- Every subcommand handler returns a `slackwright.result.Result`; `main()` renders it as human text (default)
  or **exactly one** JSON document on stdout (`--json`). **Never print to stdout from a handler** - populate
  `data["human"]` and let the renderer decide. Agents parse `--json` output with `json.load(stdout)`.
- The `--json` envelope top-level keys (`ok` / `command` / `exit_code` / `exit_code_name` / `error` /
  `message` / `remediation` / `data`) are a public contract - don't reorder or rename them.
- Exit codes are a stable enum (`slackwright.result.ExitCode`): `0 ok`, `2 usage`, `3 no_login`,
  `4 resolution_failed`, `5 transient_api`, `6 permanent_api`, `7 io`, `130 interrupted`. Numeric values stay
  stable across versions; tests compare to the enum, not the int. New failure modes get a new member + a
  remediation hint in `result.py`'s `_REMEDIATION` table, surfaced via `slackwright --schema`.
- `error` values are lowercase snake_case identifiers (`no_login`, `resolution_failed`, `rate_limited`) -
  reuse the closest existing one; don't invent lightly.

## On-disk archive schema (public contract)

The `--format archive` output layout - sharded `messages/YYYY/MM/DD/<date>-<chan-slug>-<hash8>.json`,
`_users/<id>.yaml`, `_channels/<id>.yaml`, `_files/<F_id>/...`, `_index.yaml`, `matches.jsonl` - is a public
contract. The dedup key is `sha256("<channel_id>:<ts>")` (8-char prefix only disambiguates filenames; the
full digest is the dedup source of truth) - never change the hashing scheme without coordinating across
`archive.py` and every downstream reader. Schema-changing PRs **must** bump `ARCHIVE_SCHEMA` in `archive.py`
**and** update `report.py` (the HTML renderer is the canonical reader of the layout), in the same commit. Per
the spec: `matches.jsonl` is truncated at the start of every run (per-run ledger, not append-only); re-runs
merge cleanly (created / noop / updated per message).

## Cost / observability discipline

Every network call goes through `SlackWebClient.api()` / `download_file()`, which update the client's
`CostTracker`. New code paths that bypass these must update the right counters themselves (`record_api_call`,
`record_retry`, `record_rate_limit_sleep`, ...). The `cost` block surfaces in both `_index.yaml` and the
`--json` envelope's `data.cost` - agents budget against it, so keep it honest.

## Concurrency

Every state-dir mutation (login, resolver cache writes, fetch) goes through `StateLock` - an advisory
fcntl/msvcrt file lock at `<state-dir>/.slackwright.lock`. New code paths that touch the state dir must wrap
their mutating section in `with StateLock(state_dir).acquire(timeout=...):` so concurrent invocations
serialise rather than race-corrupt the caches. Lock-acquire failure returns `ExitCode.IO` with
`error: lock_timeout`.

## Slack API discipline

- Treat `search.modules.messages` as load-bearing but **unofficial** (not part of Slack's public API
  contract). Wrap any new endpoint call in the same retry/backoff machinery as `client.py` (5-step
  exponential backoff on 429 / 5xx / transport errors; honor `Retry-After`).
- **Never invent endpoint names or response shapes** - verify against a captured fixture or ask the user to
  probe via `./slackwright resolve --json` / `doctor --json`.
- Be conservative with rate: default to sequential calls; don't parallelise without a clear, measured
  benefit (the web client itself is sequential for search).

## Test discipline

- **Behavior-changing PRs always add or update tests.** No exceptions - if you can't articulate a test, the
  change isn't ready. Baseline is ~194 tests across 11 files; keep it green (`./slackwright test -q`).
- Use the `FakeClient` from `tests/conftest.py` for anything that would otherwise need live Slack (it serves
  canned `users.list` / `conversations.list` / `auth.test` and records every `api(method, params)` call).
  Register new shapes via `fake_client.register(...)` / `register_handler(...)`. Never depend on real Slack
  in the default suite; live Playwright tests are not in it (they need real credentials).
- Use **realistic Slack ids** in tests (`UALICE00`, `CGENERAL`) - the resolver's regex rejects ids with
  underscores.
- Never write to `~/.cache/slackwright/` from tests - use the `state_dir` / `out_dir` fixtures from
  `conftest.py`.
- Test names are descriptive (`test_resolver_falls_back_to_email_lookup_when_handle_not_cached`); edge cases
  over happy paths.
- New exit codes -> extend `test_result.py`'s `TestExitCode`. New `--json` envelope fields -> add a
  `to_json()` round-trip test.

## Lint

`./slackwright lint` must pass clean before any commit. Rule set is `E F W I B UP SIM` (see `pyproject.toml`);
`E501` (line length) is intentionally disabled, line-length 100. Don't widen the rule set without asking.

## Dependencies

- Runtime: `playwright>=1.41,<2.0`, `PyYAML>=6.0`, `httpx>=0.27,<1.0`, `cryptography>=41.0`.
- Dev: `pytest>=7.4`, `pytest-asyncio>=0.23`, `ruff>=0.4`.
- **Stdlib first. Always ask before adding any new runtime or dev dep.** (`httpx` drives the browserless
  transport in `client.py`; `cryptography` decrypts Chrome cookies in `chrome_cookies.py` for the
  `--use-chrome` login path - both are load-bearing, not incidental.)

## Commits + publication

Full commit policy is embedded in `engineer.agent.md`. Slackwright-specific: imperative present-tense subject
<= 72 chars, no trailing period, ASCII only; no AI-attribution trailers; no customer / employer /
private-context references (public OSS); reference issues with `Fixes #N` / `Refs #N`. One logical change per
commit (renames / refactors / behavior changes separate). No DCO sign-off required. Canonical home is
`github.com/myurasov/Slackwright`; force-pushes to `main` need explicit approval. The version string lives in
`pyproject.toml [project].version`, `src/slackwright/__init__.py`, and `ai/spec.md`'s header - keep in sync.

## Workflow for non-trivial changes

For anything beyond a one-line typo fix: (1) state the user-visible intent and which spec section it touches;
(2) check `ai/spec.md`, updating it *first* in the same PR if behavior changes; (3) write the failing test;
(4) implement the smallest change that passes (avoid speculative generalization); (5) run
`./slackwright fmt && ./slackwright lint && ./slackwright test` - all green; (6) re-read the diff for
narration comments / missing types / dishonest docstrings; (7) commit per the discipline.

## When to ask vs just-do-it

**Ask** before: a new runtime/dev dep; a new top-level CLI subcommand; changing the on-disk archive layout
(bump `ARCHIVE_SCHEMA` same PR); changing the `--json` envelope shape or adding/repurposing an exit-code
value; changing the public Python API surface (`slackwright.__all__` - new exports fine, renames/removals
need approval); removing or weakening a test; anything you'd mark `# TODO: ask`. When in doubt, ask - a
one-line clarification beats a wrong commit.

**Just do it** (no need to ask): renaming a private helper; adding internal type hints; splitting a function
for readability with no behavior change; tightening a test or adding edge-case coverage; fixing a lint
warning; updating a docstring.

Default working style (carried by Solaris): terse responses; tables when comparing options; lead with an
explicit recommendation; give the bare command first, then variants.
