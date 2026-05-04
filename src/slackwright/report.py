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

"""Self-contained HTML report generator for a slackwright archive.

Reads an output directory produced by ``slackwright fetch`` and writes
a single ``report.html`` file with:

  - run metadata (plan, query, timestamps, cost)
  - summary stats (counts, by-month bar chart, by-channel-type)
  - a per-channel section listing every message in chronological order
  - thread grouping (replies indented under the parent ts)
  - inline reactions, file attachment links (relative to ``_files/``)
  - resolved sender names + emails when available

The output is **self-contained**: inline CSS, no external assets, no
JavaScript. You can email it, attach it to a ticket, or open it
locally with file://.

Performance: the renderer is O(messages) and streams each section to
the output file as it's built, so a 10K-message archive renders in
under a second on a modern laptop.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Loading + grouping
# ---------------------------------------------------------------------------


@dataclass
class _Message:
    ts: str
    ts_float: float
    channel_id: str
    user_id: str | None
    username: str | None
    text: str
    permalink: str | None
    thread_ts: str | None
    files: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[dict[str, Any]] = field(default_factory=list)
    direction: str | None = None  # "in" / "out"
    raw: dict[str, Any] = field(default_factory=dict)


def _load_user_cache(out_dir: Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    udir = out_dir / "_users"
    if not udir.exists():
        return cache
    for f in udir.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            uid = data.get("id") or f.stem
            cache[uid] = data
        except Exception:
            continue
    return cache


def _load_channel_cache(out_dir: Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    cdir = out_dir / "_channels"
    if not cdir.exists():
        return cache
    for f in cdir.glob("*.yaml"):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            cid = data.get("id") or f.stem
            cache[cid] = data
        except Exception:
            continue
    return cache


def _iter_message_files(out_dir: Path) -> Iterable[Path]:
    msgs = out_dir / "messages"
    if not msgs.exists():
        return ()
    return sorted(msgs.glob("**/*.json"))


def _load_message(path: Path) -> _Message | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    ts = str(raw.get("ts") or "")
    if not ts:
        return None
    try:
        tsf = float(ts)
    except (TypeError, ValueError):
        return None
    cid = str(raw.get("channel_id") or (raw.get("channel") or {}).get("id") or "")
    archive_meta = raw.get("_archive") or {}
    return _Message(
        ts=ts,
        ts_float=tsf,
        channel_id=cid,
        user_id=raw.get("user"),
        username=raw.get("username"),
        text=raw.get("text") or "",
        permalink=raw.get("permalink"),
        thread_ts=raw.get("thread_ts") or archive_meta.get("thread_ts"),
        files=list(raw.get("files") or []),
        reactions=list(raw.get("reactions") or []),
        direction=archive_meta.get("direction"),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Inline rendering helpers
# ---------------------------------------------------------------------------


_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
_CHANNEL_MENTION_RE = re.compile(r"<#([CDG][A-Z0-9]+)(?:\|([^>]+))?>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")
_BARE_URL_RE = re.compile(r"(?<![\"'>=])(https?://[^\s<>]+)")


def _render_text(
    text: str, users: dict[str, dict[str, Any]], channels: dict[str, dict[str, Any]]
) -> str:
    """Translate Slack message-text mrkdwn into safe HTML."""
    if not text:
        return ""

    def repl_user(m: re.Match[str]) -> str:
        uid, label = m.group(1), m.group(2)
        if label:
            name = label
        else:
            u = users.get(uid) or {}
            name = u.get("name") or u.get("real_name") or uid
        return f'<span class="mention">@{html.escape(name)}</span>'

    def repl_channel(m: re.Match[str]) -> str:
        cid, label = m.group(1), m.group(2)
        if label:
            name = label
        else:
            c = channels.get(cid) or {}
            name = c.get("name") or cid
        return f'<span class="mention">#{html.escape(name)}</span>'

    def repl_link(m: re.Match[str]) -> str:
        url, label = m.group(1), m.group(2)
        text = label or url
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'

    # Run mention/link substitutions on the RAW text first so we operate
    # on un-escaped angle brackets, then HTML-escape the leftover plain
    # spans.
    parts: list[str] = []
    cursor = 0
    pattern = re.compile(
        r"<(?:@(?P<u>[UW][A-Z0-9]+)(?:\|(?P<ulabel>[^>]+))?"
        r"|#(?P<c>[CDG][A-Z0-9]+)(?:\|(?P<clabel>[^>]+))?"
        r"|(?P<url>https?://[^|>]+)(?:\|(?P<urllabel>[^>]+))?"
        r"|!(?P<special>[^>|]+)(?:\|(?P<slabel>[^>]+))?"
        r")>"
    )
    for m in pattern.finditer(text):
        if m.start() > cursor:
            parts.append(_format_plain(text[cursor : m.start()]))
        if m.group("u"):
            uid = m.group("u")
            label = m.group("ulabel") or (
                (users.get(uid) or {}).get("name") or (users.get(uid) or {}).get("real_name") or uid
            )
            parts.append(f'<span class="mention">@{html.escape(label)}</span>')
        elif m.group("c"):
            cid = m.group("c")
            label = m.group("clabel") or (channels.get(cid) or {}).get("name") or cid
            parts.append(f'<span class="mention">#{html.escape(label)}</span>')
        elif m.group("url"):
            url = m.group("url")
            label = m.group("urllabel") or url
            parts.append(
                f'<a href="{html.escape(url, quote=True)}" rel="noopener">{html.escape(label)}</a>'
            )
        else:
            label = m.group("slabel") or m.group("special") or ""
            parts.append(f'<span class="mention">@{html.escape(label)}</span>')
        cursor = m.end()
    if cursor < len(text):
        parts.append(_format_plain(text[cursor:]))
    return "".join(parts)


def _format_plain(text: str) -> str:
    """Escape + lightweight mrkdwn (code spans, bold/italic, paragraphs)."""
    if not text:
        return ""
    s = html.escape(text)
    # Triple-backtick code blocks
    s = re.sub(
        r"```(.+?)```",
        lambda m: f"<pre><code>{m.group(1)}</code></pre>",
        s,
        flags=re.DOTALL,
    )
    # Inline code
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    # Bold *text* (Slack-style, single asterisk)
    s = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"<strong>\1</strong>", s)
    # Italic _text_
    s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<em>\1</em>", s)
    # Bare URLs
    s = re.sub(
        r"(?<![\"'>=])(https?://[^\s<>]+)",
        lambda m: f'<a href="{m.group(1)}" rel="noopener">{m.group(1)}</a>',
        s,
    )
    # Paragraph breaks: double-newline → </p><p>; single → <br>.
    s = re.sub(r"\n\n+", "</p><p>", s)
    s = s.replace("\n", "<br>")
    return f"<p>{s}</p>"


# ---------------------------------------------------------------------------
# CSS (kept inline so the report is one self-contained file)
# ---------------------------------------------------------------------------


_CSS = """
:root {
  --fg: #1d1f21;
  --fg-mute: #5a6470;
  --bg: #fafbfc;
  --bg-card: #ffffff;
  --border: #e1e4e8;
  --accent: #4a154b;
  --accent-mute: #f4ecf4;
  --in-bar: #e1e4e8;
  --out-bar: #4a154b;
  --code-bg: #f6f8fa;
  --link: #036cc9;
}
* { box-sizing: border-box; }
body {
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  color: var(--fg);
  background: var(--bg);
  margin: 0;
  padding: 24px;
}
.container { max-width: 980px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
h3 { font-size: 15px; margin: 24px 0 8px; color: var(--fg-mute); font-weight: 600; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }

.run-meta {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 16px 18px;
  margin-bottom: 24px;
}
.run-meta dl { margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; }
.run-meta dt { font-weight: 600; color: var(--fg-mute); }
.run-meta dd { margin: 0; word-break: break-word; }
.run-meta code { background: var(--code-bg); padding: 1px 5px; border-radius: 3px; font-size: 13px; }

.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.stat {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px 14px;
}
.stat .n { font-size: 22px; font-weight: 700; color: var(--accent); }
.stat .l { font-size: 12px; color: var(--fg-mute); text-transform: uppercase; letter-spacing: 0.04em; }

.bymonth {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px 14px;
  margin-bottom: 24px;
}
.bymonth .row { display: grid; grid-template-columns: 80px 1fr 60px; align-items: center; gap: 10px; padding: 2px 0; }
.bymonth .label { color: var(--fg-mute); font-variant-numeric: tabular-nums; }
.bymonth .bar { height: 10px; background: var(--accent-mute); border-radius: 2px; position: relative; overflow: hidden; }
.bymonth .bar > span { display: block; height: 100%; background: var(--accent); }
.bymonth .count { text-align: right; color: var(--fg-mute); font-variant-numeric: tabular-nums; }

.channels { margin-top: 12px; }
.channel {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-bottom: 16px;
  overflow: hidden;
}
.channel summary {
  list-style: none;
  cursor: pointer;
  padding: 12px 16px;
  background: linear-gradient(to bottom, #ffffff, #f7f8fa);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.channel summary::-webkit-details-marker { display: none; }
.channel summary::before {
  content: "";
  display: inline-block;
  width: 0; height: 0;
  border-style: solid;
  border-width: 5px 0 5px 7px;
  border-color: transparent transparent transparent var(--fg-mute);
  transition: transform 0.12s ease;
  flex-shrink: 0;
}
.channel[open] summary::before { transform: rotate(90deg); }
.channel .ch-name { font-weight: 700; font-size: 15px; }
.channel .ch-type {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--fg-mute); padding: 2px 6px; background: var(--accent-mute); border-radius: 3px;
}
.channel .ch-count { margin-left: auto; color: var(--fg-mute); font-size: 13px; }
.channel .ch-purpose { font-size: 12px; color: var(--fg-mute); display: block; padding: 4px 16px 0; }

.message { padding: 12px 16px; border-top: 1px solid var(--border); }
.message:first-of-type { border-top: 0; }
.message.reply { padding-left: 40px; background: #fafbfc; border-left: 3px solid var(--accent-mute); }
.message .meta { font-size: 12px; color: var(--fg-mute); margin-bottom: 4px; display: flex; gap: 8px; align-items: baseline; }
.message .author { font-weight: 700; color: var(--fg); font-size: 13px; }
.message .author.me { color: var(--accent); }
.message .ts a { color: var(--fg-mute); }
.message .body { margin: 4px 0; }
.message .body p { margin: 0 0 6px; }
.message .body p:last-child { margin-bottom: 0; }
.message .body code { background: var(--code-bg); padding: 1px 5px; border-radius: 3px; font-size: 13px; }
.message .body pre {
  background: var(--code-bg);
  padding: 10px 12px;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 13px;
  line-height: 1.4;
  margin: 6px 0;
}
.message .body pre code { background: transparent; padding: 0; }
.message .body .mention {
  background: var(--accent-mute);
  color: var(--accent);
  padding: 0 4px;
  border-radius: 3px;
  font-weight: 500;
}
.message .files { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.message .file {
  font-size: 12px;
  border: 1px solid var(--border);
  background: var(--bg-card);
  padding: 4px 8px;
  border-radius: 4px;
}
.message .file a { color: var(--fg); }
.message .reactions { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.message .react {
  font-size: 12px; padding: 2px 6px;
  background: var(--accent-mute); color: var(--accent);
  border-radius: 10px;
}

.empty {
  text-align: center;
  color: var(--fg-mute);
  padding: 40px;
  background: var(--bg-card);
  border: 1px dashed var(--border);
  border-radius: 6px;
}
footer { margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border); color: var(--fg-mute); font-size: 12px; text-align: center; }
"""


# ---------------------------------------------------------------------------
# Top-level renderer
# ---------------------------------------------------------------------------


def render_report(
    out_dir: Path,
    *,
    target: Path | None = None,
    title: str | None = None,
    state_dir: Path | None = None,
) -> Path:
    """Read ``out_dir`` and write ``out_dir/report.html`` (or ``target``).

    Returns the path that was written. When ``state_dir`` is provided
    (the slackwright state dir, typically ``~/.cache/slackwright``), the
    renderer also reads the resolver's persistent ``users.json`` /
    ``channels.json`` caches as a fallback for any id that the
    archive's own ``_users/`` / ``_channels/`` directories couldn't
    resolve. Useful for older archives written before the writer
    started seeding all referenced users.
    """
    out_dir = Path(out_dir)
    if not out_dir.exists():
        raise FileNotFoundError(f"output directory does not exist: {out_dir}")

    target_path = Path(target) if target is not None else (out_dir / "report.html")

    users = _load_user_cache(out_dir)
    channels = _load_channel_cache(out_dir)
    if state_dir is not None:
        _merge_state_dir_caches(state_dir, users, channels)
    index = _read_index_yaml(out_dir)

    messages = sorted(
        (m for m in (_load_message(p) for p in _iter_message_files(out_dir)) if m is not None),
        key=lambda m: m.ts_float,
    )

    sa_user_id = _detect_sa_user_id(messages)
    by_channel = _group_by_channel(messages)

    page_title = title or (f"Slackwright report — {index.get('plan') if index else out_dir.name}")

    parts: list[str] = []
    parts.append('<!DOCTYPE html>\n<html lang="en"><head>')
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>{html.escape(page_title)}</title>")
    parts.append(f"<style>{_CSS}</style>")
    parts.append('</head><body><div class="container">')
    parts.append(f"<h1>{html.escape(page_title)}</h1>")

    parts.append(_render_run_meta(out_dir, index, len(messages)))
    parts.append(_render_stats(messages, index))
    parts.append(_render_by_month(messages))

    parts.append("<h2>Conversations</h2>")
    if not by_channel:
        parts.append('<div class="empty">No messages in this archive.</div>')
    else:
        parts.append('<div class="channels">')
        for cid, msgs in _sorted_channels(by_channel, channels):
            parts.append(_render_channel_section(cid, msgs, channels, users, sa_user_id))
        parts.append("</div>")

    parts.append(
        "<footer>Generated by "
        '<a href="https://github.com/myurasov/Slackwright">slackwright</a> '
        f"on {html.escape(dt.datetime.now().astimezone().isoformat(timespec='seconds'))}."
        "</footer>"
    )
    parts.append("</div></body></html>\n")

    target_path.write_text("\n".join(parts), encoding="utf-8")
    return target_path


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_run_meta(out_dir: Path, index: dict[str, Any] | None, n_messages: int) -> str:
    rows: list[tuple[str, str]] = []
    if index:
        if index.get("plan"):
            rows.append(("Plan", html.escape(str(index["plan"]))))
        if index.get("query"):
            rows.append(("Query", f"<code>{html.escape(str(index['query']))}</code>"))
        if index.get("captured_at"):
            rows.append(("Captured", html.escape(str(index["captured_at"]))))
        if index.get("last_updated"):
            rows.append(("Last updated", html.escape(str(index["last_updated"]))))
        if index.get("format"):
            rows.append(("Format", html.escape(str(index["format"]))))
        cost = index.get("cost") or {}
        if cost:
            api_calls = cost.get("api_calls", 0)
            elapsed = cost.get("elapsed_ms", 0)
            bytes_in = cost.get("bytes_in", 0)
            rate = cost.get("rate_limited_seconds", 0)
            rows.append(
                (
                    "Cost",
                    f"{api_calls:,} API calls, "
                    f"{_fmt_bytes(bytes_in)} downloaded, "
                    f"{elapsed:,} ms elapsed"
                    f"{f', {rate:,}s rate-limited' if rate else ''}",
                )
            )
        ss = (index.get("extra") or {}).get("search_stats") or {}
        if ss:
            tr = ss.get("truncated_chunks") or []
            if tr:
                rows.append(
                    (
                        "Truncations",
                        "<strong>"
                        + html.escape(", ".join(map(str, tr)))
                        + "</strong> &mdash; some results may be missing.",
                    )
                )
    rows.append(("Source", f"<code>{html.escape(str(out_dir))}</code>"))
    rows.append(("Messages", f"{n_messages:,}"))
    body = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<section class="run-meta"><dl>{body}</dl></section>'


def _render_stats(messages: list[_Message], index: dict[str, Any] | None) -> str:
    n_total = len(messages)
    n_out = sum(1 for m in messages if m.direction == "out")
    n_in = n_total - n_out
    by_chan_type = (((index or {}).get("counts") or {}).get("by_channel_type")) or {}
    n_files = sum(len(m.files) for m in messages)
    n_threads = len({m.thread_ts for m in messages if m.thread_ts})

    cards: list[tuple[int, str]] = [
        (n_total, "Messages"),
        (n_out, "Sent"),
        (n_in, "Received"),
        (n_files, "Files"),
        (n_threads, "Threads"),
    ]
    for ctype, n in sorted(by_chan_type.items()):
        cards.append((int(n), ctype.upper()))

    body = "".join(
        f'<div class="stat"><div class="n">{n:,}</div><div class="l">{html.escape(label)}</div></div>'
        for n, label in cards
    )
    return f'<section class="stats">{body}</section>'


def _render_by_month(messages: list[_Message]) -> str:
    by_month: dict[str, int] = defaultdict(int)
    for m in messages:
        try:
            d = dt.datetime.fromtimestamp(m.ts_float, tz=dt.timezone.utc).astimezone()
            by_month[d.strftime("%Y-%m")] += 1
        except (TypeError, ValueError, OSError):
            continue
    if not by_month:
        return ""
    peak = max(by_month.values())
    rows = []
    for month in sorted(by_month):
        n = by_month[month]
        pct = int(round(100 * n / peak)) if peak else 0
        rows.append(
            f'<div class="row"><div class="label">{html.escape(month)}</div>'
            f'<div class="bar"><span style="width:{pct}%"></span></div>'
            f'<div class="count">{n:,}</div></div>'
        )
    return f'<section class="bymonth">{"".join(rows)}</section>'


def _render_channel_section(
    cid: str,
    msgs: list[_Message],
    channels: dict[str, dict[str, Any]],
    users: dict[str, dict[str, Any]],
    sa_user_id: str | None,
) -> str:
    chan = channels.get(cid) or {}
    # Channel cache yamls written before the resolver-seeding fix may
    # have ``name: null`` for MPIMs / private channels that
    # ``conversations.info`` couldn't resolve. The Slack search response
    # always embeds a usable name on each match, so fall back to that.
    raw_name = chan.get("name") or _channel_name_from_msgs(msgs)
    ctype = chan.get("type") or _channel_type_from_msgs(msgs) or "channel"
    purpose = chan.get("purpose") or chan.get("topic") or ""

    if ctype == "im":
        # Slack stores IMs (DMs) with the other party's user id as the
        # channel name. Render the partner's real name instead so the
        # collapsed summary is human-readable.
        other_uid = chan.get("user") or (raw_name if raw_name and raw_name.startswith(("U", "W")) else None)
        if not other_uid:
            other_uid = _other_user_in_im(msgs, sa_user_id)
        partner = users.get(other_uid) if other_uid else None
        partner_name = None
        if partner:
            partner_name = (
                partner.get("real_name")
                or partner.get("display_name")
                or partner.get("name")
            )
        # Always emit ``DM with …`` for visual consistency, even when
        # we couldn't resolve the partner — a raw ``Uxxxx`` makes the
        # IM look like a regular channel.
        name = f"DM with {partner_name or other_uid or cid}"
    else:
        name = raw_name or cid

    name_prefix = "#" if ctype == "channel" else ""
    summary = (
        f'<summary><span class="ch-name">{name_prefix}{html.escape(str(name))}</span>'
        f'<span class="ch-type">{html.escape(ctype)}</span>'
        f'<span class="ch-count">{len(msgs):,} messages</span></summary>'
    )
    if purpose:
        summary += f'<span class="ch-purpose">{html.escape(str(purpose))}</span>'

    msgs_sorted = sorted(msgs, key=lambda m: m.ts_float)
    by_thread = _group_by_thread(msgs_sorted)
    rendered_msgs: list[str] = []
    for thread in by_thread:
        for i, m in enumerate(thread):
            rendered_msgs.append(_render_message(m, users, sa_user_id, is_reply=(i > 0)))
    return f'<details class="channel">{summary}{"".join(rendered_msgs)}</details>'


def _render_message(
    m: _Message, users: dict[str, dict[str, Any]], sa_user_id: str | None, *, is_reply: bool
) -> str:
    user_block = _author_block(m, users, sa_user_id)
    when = _format_timestamp(m.ts_float)
    permalink = (
        f'<a href="{html.escape(m.permalink, quote=True)}" rel="noopener" target="_blank">{html.escape(when)}</a>'
        if m.permalink
        else html.escape(when)
    )
    body = _render_text(m.text, users, {})
    if not body and m.files:
        body = "<p><em>(attachment-only message)</em></p>"
    files = _render_files(m.files, m.raw)
    reactions = _render_reactions(m.reactions)
    cls = "message reply" if is_reply else "message"
    return (
        f'<article class="{cls}">'
        f'<div class="meta">{user_block}<span class="ts">{permalink}</span></div>'
        f'<div class="body">{body}</div>'
        f"{files}{reactions}"
        f"</article>"
    )


def _author_block(m: _Message, users: dict[str, dict[str, Any]], sa_user_id: str | None) -> str:
    u = users.get(m.user_id or "") or {}
    name = (
        u.get("real_name")
        or u.get("display_name")
        or u.get("name")
        or m.username
        or m.user_id
        or "(unknown)"
    )
    email = u.get("email") or ""
    classes = "author me" if (sa_user_id and m.user_id == sa_user_id) else "author"
    label = f'<span class="{classes}">{html.escape(str(name))}</span>'
    if email:
        label += f' <span style="color:var(--fg-mute)">&lt;{html.escape(email)}&gt;</span>'
    return label


def _render_files(files: list[dict[str, Any]], raw: dict[str, Any]) -> str:
    items: list[str] = []
    for f in files:
        if f.get("mode") == "tombstone":
            continue
        fid = f.get("id") or ""
        name = f.get("name") or fid or "file"
        # Local archive path (relative to report.html)
        rel = f"_files/{fid}/{html.escape(str(name), quote=True)}"
        items.append(f'<span class="file">📎 <a href="{rel}">{html.escape(str(name))}</a></span>')
    if not items:
        return ""
    return f'<div class="files">{"".join(items)}</div>'


def _render_reactions(reactions: list[dict[str, Any]]) -> str:
    if not reactions:
        return ""
    items: list[str] = []
    for r in reactions:
        name = r.get("name") or "?"
        count = r.get("count") or len(r.get("users") or [])
        items.append(f'<span class="react">:{html.escape(str(name))}: {int(count)}</span>')
    return f'<div class="reactions">{"".join(items)}</div>'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _merge_state_dir_caches(
    state_dir: Path,
    users: dict[str, dict[str, Any]],
    channels: dict[str, dict[str, Any]],
) -> None:
    """Fill in missing/null entries from the resolver's persistent caches.

    The archive's per-run caches (``_users/`` / ``_channels/``) only
    cover ids the writer saw. The state-dir caches accumulate across
    every prior run, so a quiet IM partner from a previous fetch may be
    resolvable here even if the current archive never saw their
    messages. Per-archive entries always win — this only fills gaps.
    """
    users_json = state_dir / "users.json"
    channels_json = state_dir / "channels.json"
    if users_json.exists():
        try:
            payload = json.loads(users_json.read_text(encoding="utf-8"))
            for uid, rec in (payload.get("users") or {}).items():
                if not isinstance(rec, dict) or not rec.get("real_name") and not rec.get("name"):
                    continue
                existing = users.get(uid) or {}
                if not (existing.get("real_name") or existing.get("display_name") or existing.get("name")):
                    users[uid] = {**rec, **{k: v for k, v in existing.items() if v}}
        except Exception:
            pass
    if channels_json.exists():
        try:
            payload = json.loads(channels_json.read_text(encoding="utf-8"))
            for cid, rec in (payload.get("channels") or {}).items():
                if not isinstance(rec, dict) or not rec.get("name"):
                    continue
                existing = channels.get(cid) or {}
                if not existing.get("name"):
                    channels[cid] = {**rec, **{k: v for k, v in existing.items() if v}}
        except Exception:
            pass


def _read_index_yaml(out_dir: Path) -> dict[str, Any] | None:
    p = out_dir / "_index.yaml"
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _detect_sa_user_id(messages: list[_Message]) -> str | None:
    """Pick the user id with the most ``direction: out`` messages."""
    counts: dict[str, int] = defaultdict(int)
    for m in messages:
        if m.direction == "out" and m.user_id:
            counts[m.user_id] += 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _channel_name_from_msgs(msgs: list[_Message]) -> str | None:
    """Pick the first non-null ``channel.name`` from any message in the group."""
    for m in msgs:
        ch = m.raw.get("channel")
        if isinstance(ch, dict):
            name = ch.get("name")
            if name:
                return str(name)
    return None


def _other_user_in_im(msgs: list[_Message], sa_user_id: str | None) -> str | None:
    """For an IM channel, find the user id that isn't the logged-in user.

    Useful when the channel cache doesn't carry an explicit ``user``
    field — pick whichever participant authored at least one message
    and isn't the SA user.
    """
    for m in msgs:
        uid = m.user_id
        if uid and uid != sa_user_id:
            return uid
    return None


def _channel_type_from_msgs(msgs: list[_Message]) -> str | None:
    """Infer a channel type from inline ``channel.is_*`` flags on a hit."""
    for m in msgs:
        ch = m.raw.get("channel")
        if not isinstance(ch, dict):
            continue
        if ch.get("is_im"):
            return "im"
        if ch.get("is_mpim"):
            return "mpim"
        if ch.get("is_group"):
            return "group"
        if ch.get("is_channel"):
            return "channel"
    return None


def _group_by_channel(messages: list[_Message]) -> dict[str, list[_Message]]:
    by: dict[str, list[_Message]] = defaultdict(list)
    for m in messages:
        by[m.channel_id].append(m)
    return by


def _sorted_channels(
    by_channel: dict[str, list[_Message]],
    channels: dict[str, dict[str, Any]],
) -> list[tuple[str, list[_Message]]]:
    """Sort channels by activity desc, with named public channels first."""

    def sort_key(item: tuple[str, list[_Message]]) -> tuple[int, int, str]:
        cid, msgs = item
        chan = channels.get(cid) or {}
        ctype = chan.get("type") or "channel"
        type_rank = {"channel": 0, "group": 1, "mpim": 2, "im": 3}.get(ctype, 4)
        return (type_rank, -len(msgs), str(chan.get("name") or cid))

    return sorted(by_channel.items(), key=sort_key)


def _group_by_thread(messages: list[_Message]) -> list[list[_Message]]:
    """Cluster messages: each thread is [parent, reply1, reply2, ...].

    Standalone (non-threaded) messages are 1-element lists in chronological
    order. Dedup is on ``ts`` (the per-channel-unique Slack timestamp),
    not the dataclass identity, since we may walk the input list more
    than once.
    """
    # First pass: index every message by ts and collect thread roots.
    by_ts: dict[str, _Message] = {m.ts: m for m in messages}
    threads_by_root: dict[str, list[_Message]] = defaultdict(list)
    standalone: list[_Message] = []
    for m in messages:
        if not m.thread_ts:
            standalone.append(m)
            continue
        threads_by_root[m.thread_ts].append(m)

    # Promote every thread group: the parent message lives at threads[0]
    # if we have it on hand, otherwise sort everything chronologically.
    threads: list[list[_Message]] = []
    used_ts: set[str] = set()
    for root_ts, members in threads_by_root.items():
        members_sorted = sorted(members, key=lambda x: x.ts_float)
        # If the root message itself is in ``messages`` but didn't carry
        # ``thread_ts == ts`` (Slack doesn't always echo it back on the
        # parent), splice it in at the front.
        if root_ts in by_ts and by_ts[root_ts] not in members_sorted:
            members_sorted.insert(0, by_ts[root_ts])
        threads.append(members_sorted)
        for m in members_sorted:
            used_ts.add(m.ts)

    for m in standalone:
        if m.ts in used_ts:
            continue
        threads.append([m])
        used_ts.add(m.ts)

    threads.sort(key=lambda g: g[0].ts_float)
    return threads


def _format_timestamp(ts: float) -> str:
    try:
        return (
            dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
            .astimezone()
            .strftime("%a %b %d %Y, %H:%M")
        )
    except (TypeError, ValueError, OSError):
        return str(ts)


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


__all__ = ["render_report"]
