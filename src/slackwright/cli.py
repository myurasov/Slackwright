# Copyright 2026 Mikhail Yurasov
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""``slackwright`` command-line interface.

Subcommands::

    slackwright login [--workspace WORKSPACE] [--token TOK --cookie-d COOKIE]
    slackwright whoami
    slackwright resolve PERSON_OR_CHANNEL [--kind {auto,user,channel}]
    slackwright fetch  [--from PERSON] [--to PERSON] [--with PERSON]
                       [--in CHANNEL]  [--query "..."]
                       [--days N | --since YYYY-MM-DD [--until YYYY-MM-DD]]
                       [--max N] [--out DIR] [--with-files]
                       [--format {archive,jsonl,raw}]
                       [--explain | --dry-run | --resume | --stream-json]
                       [--timeout SECONDS]
    slackwright describe-archive PATH
    slackwright report PATH [--out FILE]
    slackwright doctor

Top-level flags::

    --json        emit a single JSON Result document on stdout
    -q / --quiet  suppress stderr progress
    --schema      print the machine-readable CLI schema and exit
    --version     print the version and exit

Run ``slackwright <cmd> --help`` for per-command flags. The exit-code
contract is documented in :mod:`slackwright.result`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Any

from . import __version__
from .archive import ArchiveWriter, previously_completed_chunks, read_index
from .auth import (
    AuthBundle,
    LoginSession,
    has_storage_state,
    is_plausible_api_token,
    load_auth,
    login_non_interactive,
    normalize_workspace_url,
)
from .client import SlackWebClient, SlackWebError
from .cost import CostTracker
from .files import FileDownloader
from .lock import LockTimeoutError, StateLock
from .paths import ensure, resolve_state_dir
from .progress import Progress
from .report import render_report
from .resolver import EntityResolver, is_channel_id
from .result import ExitCode, Result
from .schema import describe_parser
from .search import (
    SearchPlan,
    SearchRunner,
    SearchTimeoutError,
    build_query,
    chunk_label,
    days_back,
    month_chunks,
    parse_date,
)

# Module-level so the entry point and the schema printer share one parser.
_PARSER: argparse.ArgumentParser | None = None


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slackwright",
        description=textwrap.dedent(
            """\
            slackwright — extract Slack messages via your own logged-in web session.

            Bypasses the bot-scope limits and exclusion-list restrictions that
            constrain official Slack apps and MCP integrations: every channel
            you can read in slack.com is reachable here.
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"slackwright {__version__}")
    p.add_argument(
        "--json",
        action="store_true",
        help="emit a single JSON Result document on stdout (machine-readable mode).",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress stderr progress (the JSON envelope is unaffected).",
    )
    p.add_argument(
        "--schema",
        action="store_true",
        help="print the JSON schema describing every subcommand + flag, then exit.",
    )
    p.add_argument(
        "--state-dir",
        help="override the slackwright state dir "
        "(default: ~/.cache/slackwright or $SLACKWRIGHT_STATE_DIR).",
    )
    sub = p.add_subparsers(dest="cmd", metavar="<cmd>")

    # --- login ---
    sp = sub.add_parser(
        "login",
        help="open a headed browser, log in to Slack, and persist the session.",
    )
    sp.add_argument(
        "--workspace",
        help="workspace URL (https://acme.slack.com), short name (acme), or full E-Grid URL.",
    )
    sp.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="seconds to wait for login to complete (default 300).",
    )
    sp.add_argument(
        "--executable-path",
        help="path to a custom Chromium binary (default: Playwright's bundled Chromium).",
    )
    sp.add_argument(
        "--chrome-profile",
        help="reuse this Chrome user-data-dir instead of starting fresh — if you're "
        "already signed into Slack in that profile, login is instant. "
        "macOS default: '~/Library/Application Support/Google/Chrome'.",
    )
    sp.add_argument(
        "--profile-directory",
        help="profile name within --chrome-profile (e.g. 'Default', 'Profile 1'). "
        "Only set if you have multiple Chrome profiles.",
    )
    sp.add_argument(
        "--copy-profile",
        dest="copy_profile",
        action="store_true",
        help="snapshot --chrome-profile to a private copy under the state-dir before "
        "launching, so your normal Chrome can keep running. Skips browser caches "
        "to keep the copy small (~tens of MB).",
    )
    sp.add_argument(
        "--no-copy-profile",
        dest="copy_profile",
        action="store_false",
        help="use --chrome-profile in-place; Chrome must be quit first.",
    )
    sp.set_defaults(copy_profile=None)
    sp.add_argument(
        "--use-chrome",
        action="store_true",
        help="shortcut: --chrome-profile + --executable-path set to the system Chrome's "
        "defaults (currently macOS only) and --copy-profile enabled so Chrome can "
        "stay open.",
    )
    sp.add_argument(
        "--token",
        dest="api_token",
        help="non-interactive: pre-extracted xoxc-... web token. "
        "Use with --cookie-d to skip the headed browser flow.",
    )
    sp.add_argument(
        "--cookie-d",
        dest="cookie_d",
        help="non-interactive: pre-extracted xoxd-... `d` cookie. "
        "Use with --token to skip the headed browser flow.",
    )
    sp.add_argument("--user-id", help="optional user-id metadata for non-interactive login.")
    sp.add_argument("--user-email", help="optional user-email metadata for non-interactive login.")
    sp.add_argument("--team-id", help="optional team-id metadata for non-interactive login.")

    # --- whoami ---
    sub.add_parser("whoami", help="print the logged-in user info.")

    # --- doctor ---
    sub.add_parser("doctor", help="run health checks against the saved login.")

    # --- resolve ---
    sp = sub.add_parser(
        "resolve",
        help="show what a person/channel argument resolves to (debugging).",
    )
    sp.add_argument("value", help="name, email, handle, channel name, or Slack id.")
    sp.add_argument(
        "--kind",
        choices=["auto", "user", "channel"],
        default="auto",
        help="force interpretation as user or channel (default: auto-detect).",
    )
    # --- fetch ---
    sp = sub.add_parser(
        "fetch",
        help="fetch messages matching the search filters.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            Fetch messages matching the supplied filters.

            All --from / --to / --with / --in args accept any of:
              * a Slack id (U… for users, C… / D… / G… for channels)
              * an email address (resolved via users.lookupByEmail)
              * a Slack handle (resolved via users.list cache)
              * a real / display name (case-insensitive, must be unambiguous)
              * the literal `me` (the logged-in user)

            Date range can be one of:
              * --days N            messages from the last N days (default: no range)
              * --since YYYY-MM-DD  inclusive lower bound
              * --until YYYY-MM-DD  inclusive upper bound (with or without --since)

            Output format:
              * archive (default) — sharded per-message JSON tree plus
                                    _users/_channels YAML caches, _index.yaml,
                                    and a flat matches.jsonl ledger.
              * jsonl             — flat ledger only (matches.jsonl).
              * raw               — raw Slack response objects under _raw/ (forensic).

            Inspection / control modes:
              * --explain        resolve filters + print plan/query/chunks JSON, no fetch
              * --dry-run        same as --explain (kept for backward compat)
              * --resume         skip chunks completed by a prior run at --out
              * --stream-json    emit one JSON match per line on stdout as they arrive
              * --timeout N      abort the fetch after N seconds (best-effort)
            """
        ),
    )
    sp.add_argument(
        "--from", dest="from_user", help="message author (name / email / handle / U-id / `me`)."
    )
    sp.add_argument(
        "--to",
        dest="to_user",
        help="message recipient (DMs only) — same input flexibility as --from.",
    )
    sp.add_argument(
        "--with",
        dest="with_user",
        help="DMs/MPIMs with this user — same input flexibility as --from.",
    )
    sp.add_argument(
        "--involves",
        dest="involves",
        help="shortcut for --from PERSON + --to PERSON unioned in one run "
        "(messages this person sent or received). Conflicts with --from/--to/--with.",
    )
    sp.add_argument(
        "--in", dest="in_channel", help="restrict to a channel (name with or without #, or C-id)."
    )
    sp.add_argument(
        "--query",
        dest="extra_query",
        help="extra raw search terms appended verbatim (any Slack search syntax).",
    )
    sp.add_argument(
        "--days",
        type=int,
        help="messages from the last N days (mutually exclusive with --since/--until).",
    )
    sp.add_argument("--since", help="inclusive lower bound, YYYY-MM-DD.")
    sp.add_argument("--until", help="inclusive upper bound, YYYY-MM-DD (default: today).")
    sp.add_argument(
        "--max",
        dest="max_results",
        type=int,
        help="hard cap on number of matches written (default: no cap).",
    )
    sp.add_argument(
        "--out", default="./slackwright-out", help="output directory (default: ./slackwright-out)."
    )
    sp.add_argument(
        "--with-files", action="store_true", help="also download attached files into <out>/_files/."
    )
    sp.add_argument(
        "--format",
        choices=["archive", "jsonl", "raw"],
        default="archive",
        help="output format (default: archive).",
    )
    sp.add_argument("-v", "--verbose", action="store_true", help="extra logging on stderr.")
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve filters and print the search query, but don't fetch.",
    )
    sp.add_argument(
        "--explain",
        action="store_true",
        help="emit the resolved plan + chunk schedule + query as JSON, no fetch.",
    )
    sp.add_argument(
        "--resume",
        action="store_true",
        help="re-use --out: skip chunks already marked complete in its _index.yaml.",
    )
    sp.add_argument(
        "--stream-json",
        action="store_true",
        help="emit one JSON match per line on stdout as they arrive.",
    )
    sp.add_argument(
        "--timeout", type=int, default=None, help="abort the fetch after N seconds (best-effort)."
    )
    sp.add_argument(
        "--no-refresh",
        dest="auto_refresh",
        action="store_false",
        help="don't auto-refresh the xoxc token via headless browser on invalid_auth.",
    )
    sp.set_defaults(auto_refresh=True)

    # --- describe-archive ---
    sp = sub.add_parser(
        "describe-archive",
        help="introspect an existing slackwright output dir; emit the same JSON shape as `fetch --json`.",
    )
    sp.add_argument("path", help="path to a slackwright output directory.")

    # --- report ---
    sp = sub.add_parser(
        "report",
        help="generate a self-contained HTML report from a slackwright output dir.",
    )
    sp.add_argument("path", help="path to a slackwright output directory.")
    sp.add_argument(
        "--out",
        dest="report_out",
        help="path to the HTML file to write (default: <path>/report.html).",
    )
    sp.add_argument(
        "--title", help="optional title for the report (default: derived from the run plan)."
    )

    return p


# ---------------------------------------------------------------------------
# Subcommand handlers — each returns a Result
# ---------------------------------------------------------------------------


def _cmd_login(args: argparse.Namespace) -> Result:
    state_dir = ensure(resolve_state_dir(args.state_dir))

    if args.api_token or args.cookie_d:
        # Non-interactive path
        if not (args.api_token and args.cookie_d):
            return Result.failure(
                "login",
                ExitCode.USAGE,
                "usage",
                "--token and --cookie-d must be supplied together for non-interactive login.",
            )
        if not args.workspace:
            return Result.failure(
                "login",
                ExitCode.USAGE,
                "usage",
                "--workspace is required for non-interactive login.",
            )
        try:
            with StateLock(state_dir).acquire(timeout=30):
                bundle = login_non_interactive(
                    workspace_url=args.workspace,
                    api_token=args.api_token,
                    cookie_d=args.cookie_d,
                    state_dir=state_dir,
                    user_id=args.user_id,
                    user_email=args.user_email,
                    team_id=args.team_id,
                )
        except LockTimeoutError as e:
            return Result.failure("login", ExitCode.IO, "lock_timeout", str(e))
        except ValueError as e:
            return Result.failure("login", ExitCode.USAGE, "usage", str(e))
        except Exception as e:
            return Result.failure(
                "login",
                ExitCode.PERMANENT_API,
                "permanent_api",
                f"non-interactive login failed: {e}",
            )
        return Result.success(
            "login",
            data={
                "mode": "non_interactive",
                "workspace_url": bundle.workspace_url,
                "user_id": bundle.user_id,
                "user_email": bundle.user_email,
                "state_dir": str(state_dir),
                "human": (
                    f"login OK (non-interactive) — {bundle.workspace_url} (state-dir: {state_dir})"
                ),
            },
        )

    if not args.workspace:
        return Result.failure(
            "login",
            ExitCode.USAGE,
            "usage",
            "--workspace is required for the headed login flow.",
        )

    workspace_url = normalize_workspace_url(args.workspace)
    try:
        chrome_profile, executable_path, prime_storage_state = _resolve_chrome_profile_args(
            args, state_dir=state_dir
        )
    except (FileNotFoundError, ValueError) as e:
        return Result.failure("login", ExitCode.USAGE, "usage", str(e))
    if prime_storage_state:
        sys.stderr.write(
            f"[slackwright] login: opening {workspace_url} with cookies pre-loaded "
            f"from your Chrome profile. If your Slack session is still alive there, "
            f"the workspace loads instantly with no SSO re-prompt.\n"
        )
    elif chrome_profile:
        sys.stderr.write(
            f"[slackwright] login: opening {workspace_url} via Chrome profile "
            f"{chrome_profile}. If you're already signed in there, the workspace "
            f"will load directly with no further steps.\n"
        )
    else:
        sys.stderr.write(
            f"[slackwright] login: opening {workspace_url} in a headed browser. "
            f"Sign in there; I'll detect the session automatically.\n"
        )
    try:
        with (
            StateLock(state_dir).acquire(timeout=30),
            LoginSession(
                workspace_url=workspace_url,
                state_dir=state_dir,
                executable_path=executable_path,
                chrome_profile=chrome_profile,
                profile_directory=args.profile_directory,
                prime_storage_state=prime_storage_state,
            ) as s,
        ):
            bundle = s.run_interactive(timeout_s=args.timeout)
    except TimeoutError as e:
        return Result.failure("login", ExitCode.PERMANENT_API, "login_timeout", str(e))
    except LockTimeoutError as e:
        return Result.failure("login", ExitCode.IO, "lock_timeout", str(e))
    except Exception as e:
        return Result.failure(
            "login",
            ExitCode.PERMANENT_API,
            "login_failed",
            f"login failed: {e}",
        )

    if not is_plausible_api_token(bundle.api_token):
        return Result.failure(
            "login",
            ExitCode.PERMANENT_API,
            "implausible_token",
            f"extracted token does not look like a Slack web token "
            f"({bundle.api_token[:6]!r}…). Login probably partial; try again.",
        )

    return Result.success(
        "login",
        data={
            "mode": "interactive",
            "workspace_url": bundle.workspace_url,
            "user_id": bundle.user_id,
            "user_name": bundle.user_name,
            "user_real_name": bundle.user_real_name,
            "user_email": bundle.user_email,
            "team_id": bundle.team_id,
            "enterprise_id": bundle.enterprise_id,
            "state_dir": str(state_dir),
            "human": (
                f"login OK\n"
                f"  user:        {bundle.user_real_name or bundle.user_name or bundle.user_id}\n"
                f"  email:       {bundle.user_email or '<unknown>'}\n"
                f"  workspace:   {bundle.workspace_url}\n"
                f"  state-dir:   {state_dir}"
            ),
        },
    )


def _cmd_whoami(args: argparse.Namespace) -> Result:
    state_dir = resolve_state_dir(args.state_dir)
    bundle = _load_or_complain(state_dir)
    if isinstance(bundle, Result):
        return bundle
    data = _redact(bundle.to_json())
    data["human"] = json.dumps(data, indent=2, sort_keys=True)
    return Result.success("whoami", data=data)


def _cmd_resolve(args: argparse.Namespace) -> Result:
    state_dir = ensure(resolve_state_dir(args.state_dir))
    bundle = _load_or_complain(state_dir)
    if isinstance(bundle, Result):
        return bundle

    cost = CostTracker()
    try:
        with (
            StateLock(state_dir).acquire(timeout=60),
            SlackWebClient.open(bundle, state_dir=state_dir, cost=cost) as client,
        ):
            resolver = EntityResolver(client, state_dir=state_dir)
            kind = args.kind
            try:
                if kind == "channel" or (
                    kind == "auto" and (args.value.startswith("#") or is_channel_id(args.value))
                ):
                    rc = resolver.resolve_channel(args.value)
                    resolver.save_caches()
                    cost.finalise()
                    rec = rc.record.to_json()
                    return Result.success(
                        "resolve",
                        data={
                            "kind": "channel",
                            "record": rec,
                            "cost": cost.to_json(),
                            "human": json.dumps(rec, indent=2, sort_keys=True),
                        },
                    )
                ru = resolver.resolve_user(args.value)
                resolver.save_caches()
                cost.finalise()
                rec = ru.record.to_json()
                return Result.success(
                    "resolve",
                    data={
                        "kind": "user",
                        "record": rec,
                        "cost": cost.to_json(),
                        "human": json.dumps(rec, indent=2, sort_keys=True),
                    },
                )
            except (LookupError, ValueError) as e:
                cost.finalise()
                return Result.failure(
                    "resolve",
                    ExitCode.RESOLUTION_FAILED,
                    "resolution_failed",
                    str(e),
                    data={"cost": cost.to_json()},
                )
    except LockTimeoutError as e:
        return Result.failure("resolve", ExitCode.IO, "lock_timeout", str(e))
    except SlackWebError as e:
        cost.finalise()
        return Result.failure(
            "resolve",
            _classify_slack_error(e),
            e.error or "permanent_api",
            f"Slack API error during resolve: {e}",
            data={"cost": cost.to_json()},
        )


def _cmd_doctor(args: argparse.Namespace) -> Result:
    state_dir = resolve_state_dir(args.state_dir)
    bundle = _load_or_complain(state_dir)
    if isinstance(bundle, Result):
        return bundle
    if not has_storage_state(state_dir):
        return Result.failure(
            "doctor",
            ExitCode.NO_LOGIN,
            "no_login",
            "Playwright storage state missing. Run `slackwright login` again.",
        )
    cost = CostTracker()
    try:
        with SlackWebClient.open(bundle, state_dir=state_dir, cost=cost) as client:
            r = client.health()
    except SlackWebError as e:
        cost.finalise()
        return Result.failure(
            "doctor",
            _classify_slack_error(e),
            e.error or "permanent_api",
            f"auth.test failed: {e}",
            data={"cost": cost.to_json()},
        )
    cost.finalise()
    return Result.success(
        "doctor",
        data={
            "auth_test": r,
            "user_id": r.get("user"),
            "team_id": r.get("team"),
            "cost": cost.to_json(),
            "human": (
                f"OK — auth.test → user={r.get('user')}, team={r.get('team')}, "
                f"team_url={r.get('url')}"
            ),
        },
    )


def _cmd_describe_archive(args: argparse.Namespace) -> Result:
    out = Path(args.path).expanduser().resolve()
    if not out.exists():
        return Result.failure(
            "describe-archive",
            ExitCode.IO,
            "io",
            f"path does not exist: {out}",
        )
    idx = read_index(out)
    if idx is None:
        return Result.failure(
            "describe-archive",
            ExitCode.IO,
            "io",
            f"no _index.yaml found at {out} — is this a slackwright output directory?",
        )
    n_messages = (
        sum(1 for _ in (out / "messages").rglob("*.json")) if (out / "messages").exists() else 0
    )
    n_users = sum(1 for _ in (out / "_users").glob("*.yaml")) if (out / "_users").exists() else 0
    n_channels = (
        sum(1 for _ in (out / "_channels").glob("*.yaml")) if (out / "_channels").exists() else 0
    )
    n_files = sum(1 for _ in (out / "_files").iterdir()) if (out / "_files").exists() else 0
    data = {
        "path": str(out),
        "index": idx,
        "files_on_disk": {
            "messages": n_messages,
            "users": n_users,
            "channels": n_channels,
            "files": n_files,
        },
        "human": (
            f"archive at {out}\n"
            f"  messages on disk: {n_messages:,}\n"
            f"  users cached:     {n_users:,}\n"
            f"  channels cached:  {n_channels:,}\n"
            f"  attachments:      {n_files:,}\n"
            f"  plan:             {idx.get('plan')!r}\n"
            f"  query:            {idx.get('query')!r}\n"
            f"  last updated:     {idx.get('last_updated')}\n"
        ),
    }
    return Result.success("describe-archive", data=data)


def _cmd_report(args: argparse.Namespace) -> Result:
    out = Path(args.path).expanduser().resolve()
    if not out.exists():
        return Result.failure("report", ExitCode.IO, "io", f"path does not exist: {out}")
    target = Path(args.report_out).expanduser().resolve() if args.report_out else None
    state_dir = resolve_state_dir(args.state_dir)
    try:
        written = render_report(
            out,
            target=target,
            title=args.title,
            state_dir=state_dir if state_dir.exists() else None,
        )
    except FileNotFoundError as e:
        return Result.failure("report", ExitCode.IO, "io", str(e))
    return Result.success(
        "report",
        data={
            "input": str(out),
            "report_path": str(written),
            "size_bytes": written.stat().st_size,
            "human": f"wrote {written} ({written.stat().st_size:,} bytes)",
        },
    )


def _cmd_fetch(args: argparse.Namespace) -> Result:
    state_dir = ensure(resolve_state_dir(args.state_dir))
    bundle = _load_or_complain(state_dir)
    if isinstance(bundle, Result):
        return bundle

    if args.days is not None and (args.since or args.until):
        return Result.failure(
            "fetch", ExitCode.USAGE, "usage", "--days is mutually exclusive with --since/--until"
        )

    if args.involves and (args.from_user or args.to_user or args.with_user):
        return Result.failure(
            "fetch",
            ExitCode.USAGE,
            "usage",
            "--involves cannot be combined with --from/--to/--with — "
            "it's a shortcut that runs both --from and --to internally.",
        )

    date_from: dt.date | None = None
    date_to: dt.date | None = None
    if args.days is not None:
        date_from = days_back(args.days)
        date_to = dt.date.today()
    if args.since:
        date_from = parse_date(args.since)
    if args.until:
        date_to = parse_date(args.until)
    if args.since and args.until is None:
        date_to = dt.date.today()

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    progress = Progress(label="slackwright", verbose=args.verbose)
    if args.quiet or args.json or args.stream_json:
        progress.disable()

    cost = CostTracker()
    explain_only = bool(args.explain or args.dry_run)
    stream = bool(args.stream_json)

    deadline = None
    if args.timeout is not None:
        import time as _time

        deadline = _time.monotonic() + max(1, int(args.timeout))

    # Build the list of from/to phases up-front; --involves expands to
    # two, everything else is a single phase. Used by both the
    # explain-only path and the live fetch.
    phases: list[tuple[str | None, str | None]]
    if args.involves:
        phases = [(args.involves, None), (None, args.involves)]
    else:
        phases = [(args.from_user, args.to_user)]

    # ---- explain-only fast path: try cache-only resolution first; only
    # spin up Playwright if a name actually needs a network lookup.
    if explain_only:
        explained: list[dict[str, Any]] = []
        human_lines: list[str] = []
        for phase_from, phase_to in phases:
            try:
                plan = _resolve_plan_explain(
                    args=args,
                    state_dir=state_dir,
                    bundle=bundle,
                    cost=cost,
                    date_from=date_from,
                    date_to=date_to,
                    phase_from=phase_from,
                    phase_to=phase_to,
                )
            except (LookupError, ValueError) as e:
                cost.finalise()
                return Result.failure(
                    "fetch",
                    ExitCode.RESOLUTION_FAILED,
                    "resolution_failed",
                    str(e),
                    data={"cost": cost.to_json()},
                )
            except SlackWebError as e:
                cost.finalise()
                return Result.failure(
                    "fetch",
                    _classify_slack_error(e),
                    e.error or "permanent_api",
                    f"Slack API error during resolve: {e}",
                    data={"cost": cost.to_json()},
                )
            plan_summary = plan.display()
            rendered_query = build_query(plan)
            chunks = _plan_chunks(plan)
            if not args.json:
                sys.stderr.write(f"[slackwright] plan: {plan_summary}\n")
                sys.stderr.write(f"[slackwright] query: {rendered_query!r}\n")
            explained.append(
                {
                    "plan": plan_summary,
                    "query": rendered_query,
                    "chunks": chunks,
                    "expected_chunk_count": len(chunks),
                }
            )
            human_lines.append(
                f"plan:  {plan_summary}\n"
                f"query: {rendered_query}\n"
                f"chunks ({len(chunks)}):\n  " + "\n  ".join(c["label"] for c in chunks)
            )
        cost.finalise()
        # Single-phase callers see the legacy top-level shape; --involves
        # surfaces a ``phases`` list. Both are exposed so JSON consumers
        # can branch cleanly.
        latest = explained[-1]
        return Result.success(
            "fetch",
            data={
                "explained": True,
                "plan": latest["plan"],
                "query": latest["query"],
                "chunks": latest["chunks"],
                "expected_chunk_count": latest["expected_chunk_count"],
                "phases": explained if len(explained) > 1 else None,
                "cost": cost.to_json(),
                "human": "\n\n".join(human_lines),
            },
        )

    try:
        with SlackWebClient.open(
            bundle,
            state_dir=state_dir,
            cost=cost,
            auto_refresh=args.auto_refresh,
        ) as client:
            resolver = EntityResolver(client, state_dir=state_dir)
            try:
                with StateLock(state_dir).acquire(timeout=60):
                    phase_results: list[Result] = []
                    for idx, (phase_from, phase_to) in enumerate(phases, start=1):
                        if args.involves and not args.json:
                            sys.stderr.write(
                                f"[slackwright] --involves phase {idx}/{len(phases)} "
                                f"({'from' if phase_from else 'to'}=@{args.involves})\n"
                            )
                        try:
                            plan = SearchPlan(
                                from_user=resolver.resolve_user(phase_from) if phase_from else None,
                                to_user=resolver.resolve_user(phase_to) if phase_to else None,
                                with_user=resolver.resolve_user(args.with_user)
                                if args.with_user
                                else None,
                                in_channel=resolver.resolve_channel(args.in_channel)
                                if args.in_channel
                                else None,
                                extra_query=args.extra_query,
                                date_from=date_from,
                                date_to=date_to,
                                max_results=args.max_results,
                            )
                        except (LookupError, ValueError) as e:
                            cost.finalise()
                            return Result.failure(
                                "fetch",
                                ExitCode.RESOLUTION_FAILED,
                                "resolution_failed",
                                str(e),
                                data={"cost": cost.to_json()},
                            )

                        plan_summary = plan.display()
                        rendered_query = build_query(plan)

                        if not args.json:
                            sys.stderr.write(f"[slackwright] plan: {plan_summary}\n")
                            sys.stderr.write(f"[slackwright] query: {rendered_query!r}\n")

                        skip_chunks: set[str] = set()
                        if args.resume:
                            skip_chunks = previously_completed_chunks(out, query=rendered_query)
                            if skip_chunks and not args.json:
                                sys.stderr.write(
                                    f"[slackwright] resume: skipping "
                                    f"{len(skip_chunks)} previously-completed chunks under {out}\n"
                                )

                        result = _fetch_run(
                            client=client,
                            resolver=resolver,
                            plan=plan,
                            plan_summary=plan_summary,
                            rendered_query=rendered_query,
                            out=out,
                            bundle=bundle,
                            cost=cost,
                            progress=progress,
                            args=args,
                            skip_chunks=skip_chunks,
                            stream=stream,
                            deadline=deadline,
                        )
                        phase_results.append(result)
                        # Bail on the first failed phase so we don't pile
                        # follow-up work on top of a half-broken run.
                        if not result.ok:
                            return result

                    return _merge_phase_results(phase_results, involves=bool(args.involves))
            except LockTimeoutError as e:
                cost.finalise()
                return Result.failure("fetch", ExitCode.IO, "lock_timeout", str(e))
    except SlackWebError as e:
        cost.finalise()
        return Result.failure(
            "fetch",
            _classify_slack_error(e),
            e.error or "permanent_api",
            f"Slack API error: {e}",
            data={"cost": cost.to_json()},
        )


def _resolve_chrome_profile_args(
    args: argparse.Namespace, *, state_dir: Path
) -> tuple[str | None, str | None, Path | None]:
    """Resolve ``--chrome-profile`` / ``--use-chrome`` / ``--executable-path``.

    Returns ``(chrome_profile, executable_path, prime_storage_state)``.

    For ``--use-chrome`` we no longer launch the user's real Chrome via
    Playwright at all — that path is plagued by macOS keychain /
    persistent-context fights (cookies don't decrypt with mock
    keychain, Chrome won't open a window without it). Instead we
    decrypt the user's Chrome cookies straight off disk into a
    Playwright storage-state JSON; the regular bundled-Chromium login
    path then loads that into a fresh context so Slack sees the user
    as already-authenticated. ``chrome_profile`` is only honoured for
    advanced users who pass ``--no-copy-profile`` and accept the
    persistent-context limitations.
    """
    chrome_profile = args.chrome_profile
    executable_path = args.executable_path
    use_chrome = getattr(args, "use_chrome", False)
    if use_chrome and not chrome_profile:
        # macOS only for now — easy to extend per-platform later.
        chrome_profile = str(Path.home() / "Library/Application Support/Google/Chrome")

    if not chrome_profile:
        return None, executable_path, None

    if not args.profile_directory:
        args.profile_directory = "Default"

    # Default behaviour for ``--use-chrome`` and ``--chrome-profile``
    # alike: extract cookies into a Playwright storage-state JSON
    # ("snapshot mode"). Power users who want the legacy persistent-
    # context launch (driving real Chrome under Playwright with the
    # profile in-place) opt in via ``--no-copy-profile``.
    copy_profile = args.copy_profile
    if copy_profile is None:
        copy_profile = True

    if copy_profile:
        from .chrome_cookies import extract_chrome_cookies, write_storage_state

        profile_path = Path(chrome_profile).expanduser()
        try:
            cookies = extract_chrome_cookies(
                profile_path,
                args.profile_directory,
                domain_filter=None,
            )
        except (FileNotFoundError, NotImplementedError) as e:
            raise ValueError(str(e)) from e
        except RuntimeError as e:
            raise ValueError(
                f"could not read Chrome cookies: {e}\n"
                f"If macOS prompted to allow keychain access, click "
                f"'Always Allow' and retry."
            ) from e
        prime = state_dir / "chrome-cookies.json"
        write_storage_state(cookies, prime)
        sys.stderr.write(
            f"[slackwright] login: extracted {len(cookies)} cookies from Chrome "
            f"profile {profile_path}/{args.profile_directory}.\n"
        )
        return None, executable_path, prime

    # ``--no-copy-profile`` keeps the legacy persistent-context path,
    # warts and all.
    profile_path = Path(chrome_profile).expanduser()
    lock = profile_path / "SingletonLock"
    if lock.exists() or lock.is_symlink():
        raise ValueError(
            f"Chrome is currently running and holds an exclusive lock on "
            f"{profile_path}. Either:\n"
            f"  - quit Chrome and retry the same command, or\n"
            f"  - drop --no-copy-profile (we'll extract cookies from the "
            f"profile on disk so Chrome can keep running)."
        )
    return chrome_profile, executable_path, None


def _merge_phase_results(results: list[Result], *, involves: bool) -> Result:
    """Combine per-phase ``_fetch_run`` outcomes into a single ``Result``.

    Single-phase callers see the original Result verbatim. ``--involves``
    callers get the latest run's full ``data`` (which already reflects
    the on-disk union — matches.jsonl is append-and-dedup, _index.yaml
    accumulates runs[]) plus a top-level ``phases`` array with each
    phase's plan/query/counts and a summed ``total_counts``.
    """
    if not results:
        return Result.failure("fetch", ExitCode.IO, "io", "no fetch phases ran")
    if not involves or len(results) == 1:
        return results[-1]

    latest = results[-1]
    base = dict(latest.data or {})
    phase_blobs: list[dict[str, Any]] = []
    totals = {"created": 0, "updated": 0, "noop": 0, "dropped": 0}
    for r in results:
        d = r.data or {}
        c = d.get("counts") or {}
        for k in totals:
            v = c.get(k)
            if isinstance(v, int):
                totals[k] += v
        phase_blobs.append(
            {
                "plan": d.get("plan"),
                "query": d.get("query"),
                "counts": c,
                "search_stats": d.get("search_stats"),
            }
        )

    base["phases"] = phase_blobs
    base["total_counts"] = totals
    base["human"] = (
        f"--involves: {len(results)} phases, "
        f"total {totals['created']} new / {totals['updated']} updated "
        f"messages\n" + (latest.data.get("human", "") if latest.data else "")
    )
    return Result.success("fetch", data=base)


def _resolve_plan_explain(
    *,
    args: argparse.Namespace,
    state_dir: Path,
    bundle: AuthBundle,
    cost: CostTracker,
    date_from: dt.date | None,
    date_to: dt.date | None,
    phase_from: str | None = None,
    phase_to: str | None = None,
) -> SearchPlan:
    """Resolve --from / --to / --with / --in for the explain path.

    First tries cache-only (no network). If any name needs a network
    call the resolver doesn't know about yet, opens a live HTTP client
    to finish the resolution. This keeps `--explain` zero-cost when
    every referenced entity is already cached, and still correct
    otherwise.

    ``phase_from`` / ``phase_to`` override ``args.from_user`` /
    ``args.to_user`` for ``--involves`` expansion (one explain per
    phase). Default ``None`` means use the args verbatim.
    """
    eff_from = phase_from if (phase_from is not None or phase_to is not None) else args.from_user
    eff_to = phase_to if (phase_from is not None or phase_to is not None) else args.to_user
    cache_resolver = EntityResolver(client=None, state_dir=state_dir)

    def _try_resolve(cache_only: EntityResolver) -> SearchPlan:
        return SearchPlan(
            from_user=cache_only.resolve_user(eff_from) if eff_from else None,
            to_user=cache_only.resolve_user(eff_to) if eff_to else None,
            with_user=cache_only.resolve_user(args.with_user) if args.with_user else None,
            in_channel=cache_only.resolve_channel(args.in_channel) if args.in_channel else None,
            extra_query=args.extra_query,
            date_from=date_from,
            date_to=date_to,
            max_results=args.max_results,
        )

    try:
        return _try_resolve(cache_resolver)
    except LookupError:
        pass

    # Cache miss — fall back to a live (headless) resolver. This is the
    # only way --explain can return a complete plan when names aren't yet
    # in the on-disk cache.
    with SlackWebClient.open(
        bundle,
        state_dir=state_dir,
        cost=cost,
    ) as client:
        live_resolver = EntityResolver(client, state_dir=state_dir)
        plan = _try_resolve(live_resolver)
        live_resolver.save_caches()
        return plan


def _fetch_run(
    *,
    client: SlackWebClient,
    resolver: EntityResolver,
    plan: SearchPlan,
    plan_summary: str,
    rendered_query: str,
    out: Path,
    bundle: AuthBundle,
    cost: CostTracker,
    progress: Progress,
    args: argparse.Namespace,
    skip_chunks: set[str],
    stream: bool,
    deadline: float | None,
) -> Result:
    progress.start()
    runner = SearchRunner(
        client,
        resolver,
        on_progress=progress.note,
        skip_chunks=skip_chunks,
        deadline=deadline,
    )
    writer = ArchiveWriter(
        out,
        resolver=resolver,
        sa_user_id=bundle.user_id,
        format=args.format,
        plan_summary=plan_summary,
    )
    all_messages: list[dict[str, Any]] = []
    try:
        for msg in runner.iter_matches(plan):
            writer.write_match(msg)
            all_messages.append(msg)
            if stream:
                json.dump(msg, sys.stdout, ensure_ascii=False)
                sys.stdout.write("\n")
                sys.stdout.flush()
            progress.tick(matches=1)
    except SearchTimeoutError as e:
        progress.stop()
        cost.finalise()
        return Result.failure(
            "fetch",
            ExitCode.TRANSIENT_API,
            "fetch_timeout",
            str(e),
            data=_finalise_run(
                writer=writer,
                resolver=resolver,
                runner=runner,
                args=args,
                client=client,
                all_messages=all_messages,
                plan_summary=plan_summary,
                rendered_query=rendered_query,
                out=out,
                cost=cost,
            ),
        )

    progress.stop()
    data = _finalise_run(
        writer=writer,
        resolver=resolver,
        runner=runner,
        args=args,
        client=client,
        all_messages=all_messages,
        plan_summary=plan_summary,
        rendered_query=rendered_query,
        out=out,
        cost=cost,
    )
    return Result.success("fetch", data=data)


def _finalise_run(
    *,
    writer: ArchiveWriter,
    resolver: EntityResolver,
    runner: SearchRunner,
    args: argparse.Namespace,
    client: SlackWebClient,
    all_messages: list[dict[str, Any]],
    plan_summary: str,
    rendered_query: str,
    out: Path,
    cost: CostTracker,
) -> dict[str, Any]:
    sys.stderr.write(
        f"[slackwright] resolving {len(writer.stats.user_ids_seen)} users / "
        f"{len(writer.stats.channel_ids_seen)} channels for output cache…\n"
    ) if not (args.quiet or args.json or args.stream_json) else None
    try:
        resolver.resolve_users_in(writer.stats.user_ids_seen)
    except SlackWebError as e:
        sys.stderr.write(f"[slackwright] WARN: user resolution incomplete: {e}\n")
    try:
        resolver.resolve_channels_in(writer.stats.channel_ids_seen)
    except SlackWebError as e:
        sys.stderr.write(f"[slackwright] WARN: channel resolution incomplete: {e}\n")
    resolver.save_caches()
    users_written = writer.write_users_cache(resolver)
    channels_written = writer.write_channels_cache(resolver)

    downloaded_stats = None
    if args.with_files:
        if not (args.quiet or args.json or args.stream_json):
            sys.stderr.write("[slackwright] downloading attachments…\n")
        downloader = FileDownloader(client, out, on_progress=lambda _s: None)
        downloaded_stats = downloader.download_for_messages(all_messages)

    cost.finalise()
    cost_block = cost.to_json()

    extra = {
        "users_written": users_written,
        "channels_written": channels_written,
        "search_stats": {
            "pages_fetched": runner.stats.pages_fetched,
            "matches_total": runner.stats.matches_total,
            "matches_unique": runner.stats.matches_unique,
            "chunks": runner.stats.chunks,
            "chunks_completed": list(runner.stats.chunks_completed),
            "chunks_skipped": list(runner.stats.chunks_skipped),
            "truncated_chunks": runner.stats.truncated_chunks,
        },
    }
    if downloaded_stats is not None:
        extra["files"] = {
            "attempted": downloaded_stats.attempted,
            "downloaded": downloaded_stats.downloaded,
            "skipped": downloaded_stats.skipped,
            "errors": downloaded_stats.errors,
            "bytes_total": downloaded_stats.bytes_total,
        }

    idx_path = writer.write_index(
        plan_summary=plan_summary,
        search_query=rendered_query,
        extra=extra,
        cost=cost_block,
    )

    return {
        "out_dir": str(out),
        "index_path": str(idx_path),
        "plan": plan_summary,
        "query": rendered_query,
        "counts": {
            "created": writer.stats.created,
            "updated": writer.stats.updated,
            "noop": writer.stats.noop,
            "dropped": writer.stats.dropped,
            "users_written": users_written,
            "channels_written": channels_written,
        },
        "search_stats": extra["search_stats"],
        "files": extra.get("files"),
        "cost": cost_block,
        "human": (
            f"wrote {writer.stats.created} new / {writer.stats.updated} updated "
            f"messages to {out}"
            + (f" ({writer.stats.dropped} dropped)" if writer.stats.dropped else "")
            + f"\n  users:    {users_written}\n"
            f"  channels: {channels_written}\n"
            f"  index:    {idx_path}"
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_chunks(plan: SearchPlan) -> list[dict[str, Any]]:
    """Pre-compute the chunk schedule for `--explain` output."""
    if plan.date_from is None and plan.date_to is None:
        return [
            {
                "label": chunk_label(None, None),
                "after": None,
                "before": None,
                "query": build_query(plan),
            }
        ]
    a = plan.date_from or dt.date(2010, 1, 1)
    b = plan.date_to or dt.date.today()
    out: list[dict[str, Any]] = []
    for c0, c1 in month_chunks(a, b):
        out.append(
            {
                "label": chunk_label(c0, c1),
                "after": c0.isoformat(),
                "before": c1.isoformat(),
                "query": build_query(plan, after=c0, before=c1),
            }
        )
    return out


def _load_or_complain(state_dir: Path) -> AuthBundle | Result:
    try:
        return load_auth(state_dir)
    except FileNotFoundError as e:
        return Result.failure("whoami", ExitCode.NO_LOGIN, "no_login", str(e))


def _redact(d: dict[str, object]) -> dict[str, object]:
    out = dict(d)
    if isinstance(out.get("api_token"), str):
        t = out["api_token"]
        out["api_token"] = f"{t[:8]}…<redacted>"
    return out


def _classify_slack_error(err: SlackWebError) -> ExitCode:
    msg = (err.error or str(err)).lower()
    if any(
        k in msg
        for k in (
            "ratelimited",
            "rate_limit",
            "transport",
            "timeout",
            "service_unavailable",
            "internal_error",
        )
    ):
        return ExitCode.TRANSIENT_API
    return ExitCode.PERMANENT_API


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_HANDLERS = {
    "login": _cmd_login,
    "whoami": _cmd_whoami,
    "resolve": _cmd_resolve,
    "fetch": _cmd_fetch,
    "doctor": _cmd_doctor,
    "describe-archive": _cmd_describe_archive,
    "report": _cmd_report,
}


def _emit_result(result: Result, *, as_json: bool) -> int:
    if as_json:
        result.render_json()
    else:
        result.render_human()
    return int(result.exit_code)


def main(argv: list[str] | None = None) -> int:
    global _PARSER
    parser = _build_parser()
    _PARSER = parser
    args = parser.parse_args(argv)

    if args.schema:
        schema = describe_parser(parser)
        json.dump(schema, sys.stdout, indent=2, sort_keys=False, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    if args.cmd is None:
        if args.json:
            return _emit_result(
                Result.failure("(none)", ExitCode.USAGE, "usage", "no subcommand supplied"),
                as_json=True,
            )
        parser.print_help()
        return int(ExitCode.USAGE)

    handler = _HANDLERS.get(args.cmd)
    if handler is None:
        parser.error(f"unknown command {args.cmd!r}")
        return int(ExitCode.USAGE)

    try:
        result = handler(args)
    except KeyboardInterrupt:
        return _emit_result(
            Result.failure(args.cmd, ExitCode.INTERRUPTED, "interrupted", "interrupted by user"),
            as_json=args.json,
        )
    except FileNotFoundError as e:
        return _emit_result(
            Result.failure(args.cmd, ExitCode.NO_LOGIN, "no_login", str(e)),
            as_json=args.json,
        )
    except OSError as e:
        return _emit_result(
            Result.failure(args.cmd, ExitCode.IO, "io", str(e)),
            as_json=args.json,
        )
    except Exception as e:
        if args.cmd == "fetch" and getattr(args, "verbose", False):
            traceback.print_exc()
        return _emit_result(
            Result.failure(args.cmd, ExitCode.PERMANENT_API, "unknown", f"unhandled error: {e}"),
            as_json=args.json,
        )
    return _emit_result(result, as_json=args.json)
