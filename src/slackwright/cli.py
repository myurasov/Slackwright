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

    slackwright login [--workspace URL]
    slackwright whoami
    slackwright resolve PERSON_OR_CHANNEL [--kind {auto,user,channel}]
    slackwright fetch  [--from PERSON] [--to PERSON] [--with PERSON]
                      [--in CHANNEL]  [--query "..."]
                      [--days N | --since YYYY-MM-DD [--until YYYY-MM-DD]]
                      [--max N] [--out DIR] [--with-files]
                      [--headed | --headless]
                      [--format {archive,jsonl,raw}]

Run ``slackwright <cmd> --help`` for per-command flags.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import textwrap
import traceback
from pathlib import Path

from . import __version__
from .archive import ArchiveWriter
from .auth import (
    AuthBundle,
    LoginSession,
    has_storage_state,
    is_plausible_api_token,
    load_auth,
    normalize_workspace_url,
)
from .client import SlackWebClient, SlackWebError
from .files import FileDownloader
from .paths import ensure, resolve_state_dir
from .progress import Progress
from .resolver import EntityResolver, is_channel_id
from .search import SearchPlan, SearchRunner, build_query, days_back, parse_date

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
        "--state-dir",
        help="override the slackwright state dir "
             "(default: ~/.cache/slackwright or $SLACKWRIGHT_STATE_DIR).",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<cmd>")

    # --- login ---
    sp = sub.add_parser(
        "login",
        help="open a headed browser, log in to Slack, and persist the session.",
    )
    sp.add_argument(
        "--workspace",
        required=True,
        help="workspace URL (https://acme.slack.com), short name (acme), or full E-Grid URL.",
    )
    sp.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait for login to complete (default 300).")
    sp.add_argument("--executable-path",
                    help="path to a custom Chromium binary (default: Playwright's bundled Chromium).")

    # --- whoami ---
    sub.add_parser("whoami", help="print the logged-in user info.")

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
    sp.add_argument("--headed", action="store_true",
                    help="run resolver browser headed (default headless).")

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
            """
        ),
    )
    sp.add_argument("--from", dest="from_user",
                    help="message author (name / email / handle / U-id / `me`).")
    sp.add_argument("--to", dest="to_user",
                    help="message recipient (DMs only) — same input flexibility as --from.")
    sp.add_argument("--with", dest="with_user",
                    help="DMs/MPIMs with this user — same input flexibility as --from.")
    sp.add_argument("--in", dest="in_channel",
                    help="restrict to a channel (name with or without #, or C-id).")
    sp.add_argument("--query", dest="extra_query",
                    help="extra raw search terms appended verbatim (any Slack search syntax).")
    sp.add_argument("--days", type=int,
                    help="messages from the last N days (mutually exclusive with --since/--until).")
    sp.add_argument("--since", help="inclusive lower bound, YYYY-MM-DD.")
    sp.add_argument("--until", help="inclusive upper bound, YYYY-MM-DD (default: today).")
    sp.add_argument("--max", dest="max_results", type=int,
                    help="hard cap on number of matches written (default: no cap).")
    sp.add_argument("--out", default="./slackwright-out",
                    help="output directory (default: ./slackwright-out).")
    sp.add_argument("--with-files", action="store_true",
                    help="also download attached files into <out>/_files/.")
    bg = sp.add_mutually_exclusive_group()
    bg.add_argument("--headless", dest="headed", action="store_false",
                    help="run browser headless (default).")
    bg.add_argument("--headed", dest="headed", action="store_true",
                    help="run browser visible (useful for debugging, optional).")
    sp.set_defaults(headed=False)
    sp.add_argument("--format", choices=["archive", "jsonl", "raw"], default="archive",
                    help="output format (default: archive).")
    sp.add_argument("--executable-path",
                    help="path to a custom Chromium binary (default: Playwright's bundled Chromium).")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="extra logging on stderr.")
    sp.add_argument("--dry-run", action="store_true",
                    help="resolve filters and print the search query, but don't fetch.")

    # --- doctor ---
    sub.add_parser("doctor", help="run health checks against the saved login.")

    return p


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_login(args: argparse.Namespace) -> int:
    state_dir = ensure(resolve_state_dir(args.state_dir))
    workspace_url = normalize_workspace_url(args.workspace)
    sys.stderr.write(
        f"[slackwright] login: opening {workspace_url} in a headed browser. "
        f"Sign in there; I'll detect the session automatically.\n"
    )
    try:
        with LoginSession(
            workspace_url=workspace_url,
            state_dir=state_dir,
            executable_path=args.executable_path,
        ) as s:
            bundle = s.run_interactive(timeout_s=args.timeout)
    except TimeoutError as e:
        sys.stderr.write(f"[slackwright] {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"[slackwright] login failed: {e}\n")
        if args_global_verbose():
            traceback.print_exc()
        return 1

    if not is_plausible_api_token(bundle.api_token):
        sys.stderr.write(
            f"[slackwright] WARN: extracted token does not look like a Slack web token "
            f"({bundle.api_token[:6]!r}…). Login probably partial; try again.\n"
        )
        return 1
    sys.stderr.write(
        f"[slackwright] login OK\n"
        f"  user:        {bundle.user_real_name or bundle.user_name or bundle.user_id}\n"
        f"  email:       {bundle.user_email or '<unknown>'}\n"
        f"  workspace:   {bundle.workspace_url}\n"
        f"  state-dir:   {state_dir}\n"
    )
    return 0


def _cmd_whoami(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    try:
        bundle = load_auth(state_dir)
    except FileNotFoundError as e:
        sys.stderr.write(f"[slackwright] {e}\n")
        return 2
    print(json.dumps(_redact(bundle.to_json()), indent=2, sort_keys=True))
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    state_dir = ensure(resolve_state_dir(args.state_dir))
    bundle = _load_or_complain(state_dir)
    if bundle is None:
        return 2

    headed = bool(getattr(args, "headed", False))
    with SlackWebClient.open(bundle, state_dir=state_dir, headed=headed) as client:
        resolver = EntityResolver(client, state_dir=state_dir)
        kind = args.kind
        try:
            if kind == "channel" or (kind == "auto" and (args.value.startswith("#") or is_channel_id(args.value))):
                rc = resolver.resolve_channel(args.value)
                resolver.save_caches()
                print(json.dumps(rc.record.to_json(), indent=2, sort_keys=True))
                return 0
            ru = resolver.resolve_user(args.value)
            resolver.save_caches()
            print(json.dumps(ru.record.to_json(), indent=2, sort_keys=True))
            return 0
        except (LookupError, ValueError) as e:
            sys.stderr.write(f"[slackwright] resolve: {e}\n")
            return 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    bundle = _load_or_complain(state_dir)
    if bundle is None:
        return 2
    if not has_storage_state(state_dir):
        sys.stderr.write(
            "[slackwright] FAIL: Playwright storage state missing. Run `slackwright login` again.\n"
        )
        return 1
    if not is_plausible_api_token(bundle.api_token):
        sys.stderr.write(
            "[slackwright] WARN: persisted token does not look like a Slack web token.\n"
        )
    sys.stderr.write("[slackwright] doctor: probing auth.test (headless)…\n")
    try:
        with SlackWebClient.open(bundle, state_dir=state_dir, headed=False) as client:
            r = client.health()
    except SlackWebError as e:
        sys.stderr.write(f"[slackwright] FAIL: {e}\n")
        return 1
    sys.stderr.write(f"[slackwright] OK — auth.test → user={r.get('user')}, team={r.get('team')}\n")
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    state_dir = ensure(resolve_state_dir(args.state_dir))
    bundle = _load_or_complain(state_dir)
    if bundle is None:
        return 2

    if args.days is not None and (args.since or args.until):
        sys.stderr.write("[slackwright] --days is mutually exclusive with --since/--until\n")
        return 2

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

    with SlackWebClient.open(bundle, state_dir=state_dir, headed=args.headed) as client:
        resolver = EntityResolver(client, state_dir=state_dir)
        try:
            plan = SearchPlan(
                from_user=resolver.resolve_user(args.from_user) if args.from_user else None,
                to_user=resolver.resolve_user(args.to_user) if args.to_user else None,
                with_user=resolver.resolve_user(args.with_user) if args.with_user else None,
                in_channel=resolver.resolve_channel(args.in_channel) if args.in_channel else None,
                extra_query=args.extra_query,
                date_from=date_from,
                date_to=date_to,
                max_results=args.max_results,
            )
        except (LookupError, ValueError) as e:
            sys.stderr.write(f"[slackwright] resolve: {e}\n")
            return 1

        plan_summary = plan.display()
        rendered_query = build_query(plan)
        sys.stderr.write(f"[slackwright] plan: {plan_summary}\n")
        sys.stderr.write(f"[slackwright] query: {rendered_query!r}\n")

        if args.dry_run:
            sys.stderr.write("[slackwright] --dry-run: not fetching.\n")
            resolver.save_caches()
            return 0

        progress.start()
        runner = SearchRunner(client, resolver, on_progress=progress.note)
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
                progress.tick(matches=1)
        except SlackWebError as e:
            progress.stop()
            sys.stderr.write(f"[slackwright] ERROR: {e}\n")
            if args.verbose:
                traceback.print_exc()
            return 1

        progress.stop()

        # Resolve every user / channel id we saw so the output _users
        # / _channels caches are complete (file downloads happen after
        # this so files already know channel context).
        sys.stderr.write(
            f"[slackwright] resolving {len(writer.stats.user_ids_seen)} users / "
            f"{len(writer.stats.channel_ids_seen)} channels for output cache…\n"
        )
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
            sys.stderr.write("[slackwright] downloading attachments…\n")
            downloader = FileDownloader(client, out, on_progress=progress.note)
            downloaded_stats = downloader.download_for_messages(all_messages)
            progress.tick(files=downloaded_stats.downloaded)

        idx_path = writer.write_index(
            plan_summary=plan_summary,
            search_query=rendered_query,
            extra={
                "users_written": users_written,
                "channels_written": channels_written,
                "files": (
                    {
                        "attempted": downloaded_stats.attempted,
                        "downloaded": downloaded_stats.downloaded,
                        "skipped": downloaded_stats.skipped,
                        "errors": downloaded_stats.errors,
                        "bytes_total": downloaded_stats.bytes_total,
                    }
                    if downloaded_stats is not None
                    else None
                ),
                "search_stats": {
                    "pages_fetched": runner.stats.pages_fetched,
                    "matches_total": runner.stats.matches_total,
                    "matches_unique": runner.stats.matches_unique,
                    "chunks": runner.stats.chunks,
                    "truncated_chunks": runner.stats.truncated_chunks,
                },
            },
        )

    sys.stderr.write(
        f"[slackwright] wrote {writer.stats.created} new / "
        f"{writer.stats.updated} updated messages to {out}\n"
        f"  users:    {users_written}\n"
        f"  channels: {channels_written}\n"
        f"  index:    {idx_path}\n"
    )
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_or_complain(state_dir: Path) -> AuthBundle | None:
    try:
        return load_auth(state_dir)
    except FileNotFoundError as e:
        sys.stderr.write(f"[slackwright] {e}\n")
        return None


def _redact(d: dict[str, object]) -> dict[str, object]:
    out = dict(d)
    if isinstance(out.get("api_token"), str):
        t = out["api_token"]
        out["api_token"] = f"{t[:8]}…<redacted>"
    return out


# Sentinel for "global verbose" — argparse stuffs everything in args, but
# we want a no-op default for code paths that are evaluated before args
# is built (the login error handler, mainly). The function is called only
# inside the CLI.
def args_global_verbose() -> bool:
    return "-v" in sys.argv or "--verbose" in sys.argv


# Re-import for typing convenience in _cmd_fetch (must be after definitions).
from typing import Any  # noqa: E402

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handler = {
        "login": _cmd_login,
        "whoami": _cmd_whoami,
        "resolve": _cmd_resolve,
        "fetch": _cmd_fetch,
        "doctor": _cmd_doctor,
    }.get(args.cmd)
    if handler is None:
        parser.error(f"unknown command {args.cmd!r}")
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:
        sys.stderr.write("\n[slackwright] interrupted\n")
        return 130
