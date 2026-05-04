# dev.memory — slackwright maintainer preferences

The dev.agent reads this file on every turn (after `dev.agent.md`,
before touching code). Each entry is a hard rule unless the maintainer
overrides it in the current conversation.

This is a living document. Append a new bullet under the right section
whenever the maintainer says any of:

- *"always do X"* / *"never do X"*
- *"every time you …, do …"*
- *"I prefer X to Y"*
- *"remember that …"*
- *"in this project we …"*

Keep entries short, grouped, and dated only when the rule depends on a
specific event. Don't reorder existing entries unless asked.

---

## Workflow

- Use `./slackwright install / test / lint / fmt / shell / clean` for
  the dev workflow. Never run `uv`, `pytest`, `ruff`, or `playwright`
  directly — the helper script handles bootstrap, the iCloud `.pth`
  workaround, and Playwright Chromium installation idempotently.
- Forwarded CLI calls (`./slackwright login ...`, `./slackwright fetch
  ...`) auto-bootstrap too. There is no `./slackwright run` — the pass-
  through is implicit.
- Never write to `~/.cache/slackwright/` from tests. Tests must use the
  `state_dir` / `out_dir` fixtures from `tests/conftest.py`.

## Code style

- Python 3.10+ only. `from __future__ import annotations` at the top
  of every module. PEP-604 union syntax (`X | None`, not
  `Optional[X]`).
- Type hints on every public function and dataclass.
- Dataclasses over dicts for any structured value crossing module
  boundaries.
- Comments explain *why*, not *what*. Skip narration comments.
- Apache 2.0 copyright header on every new `.py` file (copy from any
  existing module).

## Tests

- Behavior-changing PRs always add or update tests. No exceptions.
- Use the `FakeClient` from `tests/conftest.py` instead of mocking
  Playwright or hitting the real Slack API.
- Test names are descriptive
  (`test_resolver_falls_back_to_email_lookup_when_handle_not_cached`,
  not `test_resolver_email`).
- Edge cases over happy paths.

## Lint

- `./slackwright lint` must pass clean before any commit.
- Rule set is `E F W I B UP SIM` (see `pyproject.toml`). Don't widen
  it without asking. `E501` (line length) is intentionally disabled.

## Commits

- One logical change per commit. Renames, refactors, behavior changes
  go in separate commits.
- Subject line: imperative present tense, ≤72 characters, no trailing
  period, ASCII only.
- **Never** include AI-attribution trailers (`Co-Authored-By: Claude
  <...>`, `Made-with: Cursor`, `Generated-with: ...`, robot/sparkles
  emoji, references to model names). The maintainer wrote it — strip
  any auto-injected trailer before pushing.
- No customer / employer / private-context references in commit
  messages. This is public OSS.
- Reference issues with `Fixes #N` or `Refs #N` on a final body line
  (after a blank line). Don't fabricate issue numbers.

## Dependencies

- Runtime deps are limited to `playwright` and `PyYAML`. Dev deps to
  `pytest`, `pytest-asyncio`, `ruff`. **Always ask before adding
  another.**
- Stdlib first. Argparse over click. `sync_playwright` over async.

## Slack API

- Treat `search.modules.messages` as load-bearing but unofficial.
  Wrap any new endpoint call in the same retry/backoff machinery as
  `client.py`.
- Never invent endpoint names or response shapes — verify against a
  captured fixture or ask the maintainer to probe via
  `./slackwright resolve` / `doctor`.
- Be conservative with rate. Default to sequential calls; don't
  parallelise without clear benefit.

## On-disk schema

- The output layout (sharded `messages/YYYY/MM/DD/...json`,
  `_users/`, `_channels/`, `_index.yaml`, `matches.jsonl`) is a
  **public contract**. Changing it requires a spec update in the same
  PR and an `archive_schema` bump in `archive.py`.
- The dedup key is `sha256("<channel_id>:<ts>")`. Don't change the
  hashing scheme without coordinating across `archive.py` and any
  downstream readers the maintainer is using.

## Ask-before-acting list

(Mirrors `dev.agent.md` § "When to ask the user". Keep them in sync
when the maintainer adds or removes items here.)

- Adding a new runtime dependency.
- Adding a new top-level subcommand to the CLI.
- Changing the on-disk archive layout.
- Changing the public Python API surface
  (anything imported from `slackwright.<module>`).
- Removing or significantly weakening a test.
- Anything you find yourself wanting to mark with `# TODO: ask`.

## Maintainer preferences (free-form, append-only)

<!-- Add new entries below this line. Newer entries last. -->

- Prefer single-entry helper scripts (`./slackwright`) that handle
  both dev workflow and CLI forwarding, over separate `./dev` +
  `./slackwright` binaries. (May 2026)
- Every CLI subcommand returns a `Result` (see
  `slackwright.result.Result`); `main()` renders it as JSON when
  `--json` is set, human text otherwise. Don't print directly to
  stdout from a handler — populate `data.human` and let the renderer
  decide. (May 2026)
- Exit codes are an enum, not magic ints. New failure modes get a new
  `ExitCode` member with a documented remediation; the table is
  surfaced via `slackwright --schema` so agents can rely on it. (May
  2026)
- Cost / observability lives on `SlackWebClient.cost`. Anything that
  hits the network in a new code path must increment the right counter
  (api_calls, retries, rate_limited_seconds, ...) so the manifest's
  `cost` block stays honest. (May 2026)
- The on-disk archive layout (sharded `messages/YYYY/MM/DD/...json`,
  `_users/`, `_channels/`, `_index.yaml`, `matches.jsonl`,
  `_files/<F_id>/...`) is a public contract. The HTML report renderer
  in `report.py` is the canonical reader of that layout — when you
  change the schema, update the renderer and bump `archive_schema` in
  `archive.py`. (May 2026)
