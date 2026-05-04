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

"""Search query builder + paginated fetch via Slack's web search API.

The web client uses ``search.modules.messages`` (the same endpoint that
backs the search box in the desktop / web app). It accepts the standard
Slack search operators (``from:``, ``to:``, ``with:``, ``in:``,
``before:``, ``after:``, ``on:``, ``during:``) plus arbitrary keyword text.

Pagination: Slack caps responses at 100 results per page and 100 pages
(10K results) per query. For ranges that overflow the cap we slice the
date window into smaller pieces and merge — same strategy the existing
fetch-slack tool uses.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime as dt
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

from .client import SlackWebClient, SlackWebError
from .resolver import EntityResolver, ResolvedChannel, ResolvedUser

SEARCH_PER_PAGE = 100
SEARCH_MAX_PAGE = 100
SEARCH_MAX_RESULTS = SEARCH_PER_PAGE * SEARCH_MAX_PAGE  # 10_000


class SearchTimeoutError(RuntimeError):
    """Raised when ``SearchRunner`` exceeds its configured ``--timeout``."""


def chunk_label(after: dt.date | None, before: dt.date | None) -> str:
    """Stable, human-readable id for one (after, before) chunk.

    Used both in stderr progress lines and in
    ``_index.yaml.search_stats.chunks_completed`` so ``--resume`` can
    pick up where a prior run left off.
    """
    if after is None and before is None:
        return "(all time)"
    a = after.isoformat() if after else "*"
    b = before.isoformat() if before else "*"
    return f"{a}..{b}"


# ---------------------------------------------------------------------------
# Plan dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SearchPlan:
    """Everything ``SearchRunner`` needs to know about one fetch.

    The plan is **fully resolved** — all ``--from`` / ``--in`` etc. inputs
    have been turned into ``ResolvedUser`` / ``ResolvedChannel``. The
    runner only needs to format and execute.

    ``date_from`` / ``date_to`` are inclusive ``date`` objects in the
    user's local timezone. The runner converts to Slack's exclusive
    ``after:`` / ``before:`` bounds at query time.

    ``extra_query`` is appended verbatim — useful for ``has:link``,
    ``hasmy:thumbsup``, raw keyword text, etc.
    """

    from_user: ResolvedUser | None = None
    to_user: ResolvedUser | None = None
    with_user: ResolvedUser | None = None
    in_channel: ResolvedChannel | None = None
    extra_query: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    max_results: int | None = None
    sort: str = "timestamp"
    sort_dir: str = "desc"

    def display(self) -> str:
        """Human-readable summary for stderr / log lines."""
        bits: list[str] = []
        if self.from_user:
            bits.append(f"from={self.from_user.handle or self.from_user.id}")
        if self.to_user:
            bits.append(f"to={self.to_user.handle or self.to_user.id}")
        if self.with_user:
            bits.append(f"with={self.with_user.handle or self.with_user.id}")
        if self.in_channel:
            bits.append(f"in=#{self.in_channel.slug or self.in_channel.id}")
        if self.extra_query:
            bits.append(f"query={self.extra_query!r}")
        if self.date_from or self.date_to:
            bits.append(
                f"dates={self.date_from.isoformat() if self.date_from else '*'}..{self.date_to.isoformat() if self.date_to else '*'}"
            )
        return " ".join(bits) or "(no filters)"


# ---------------------------------------------------------------------------
# Query builder (pure)
# ---------------------------------------------------------------------------


def _user_token(u: ResolvedUser) -> str:
    """Pick the most reliable Slack-search token for the user.

    Order of preference:
      1. ``@handle``         — least ambiguous, supported on every workspace
      2. fall back to the U… id directly (Slack accepts it bare)
    """
    if u.handle:
        return f"@{u.handle}"
    return u.id


def _channel_token(c: ResolvedChannel) -> str:
    if c.slug and c.record.type == "channel":
        return f"#{c.slug}"
    if c.slug and c.record.type in {"mpim", "group"}:
        # Multi-party / private group DMs: the slug already encodes membership.
        return f"#{c.slug}"
    if c.record.type == "im" and c.slug:
        # IM channel name is usually just the other user's id; fall through.
        return f"#{c.slug}"
    # Bare id form is accepted by `in:` too in the web client.
    return c.id


def build_query(plan: SearchPlan, *, after: dt.date | None = None, before: dt.date | None = None) -> str:
    """Render a SearchPlan to a Slack search query string.

    The ``after`` / ``before`` overrides are used by the chunker — the
    plan's full date range is split into per-month windows, and each
    window calls ``build_query`` with its own narrow bounds.
    """
    parts: list[str] = []
    if plan.from_user:
        parts.append(f"from:{_user_token(plan.from_user)}")
    if plan.to_user:
        parts.append(f"to:{_user_token(plan.to_user)}")
    if plan.with_user:
        parts.append(f"with:{_user_token(plan.with_user)}")
    if plan.in_channel:
        parts.append(f"in:{_channel_token(plan.in_channel)}")
    a = after if after is not None else plan.date_from
    b = before if before is not None else plan.date_to
    if a is not None:
        # Slack `after:` is exclusive, so push back one day to make it inclusive.
        parts.append(f"after:{(a - dt.timedelta(days=1)).isoformat()}")
    if b is not None:
        # Slack `before:` is exclusive, so push forward one day.
        parts.append(f"before:{(b + dt.timedelta(days=1)).isoformat()}")
    if plan.extra_query:
        parts.append(plan.extra_query.strip())
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------------


def parse_date(value: str) -> dt.date:
    """Permissive ISO-date parse: ``2026-04-25`` or ``2026/04/25`` or
    ``20260425``. Empty / None raises.
    """
    if not value:
        raise ValueError("date is empty")
    s = value.strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        s = f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return dt.date.fromisoformat(s)


def days_back(n: int, *, today: dt.date | None = None) -> dt.date:
    """``today - n`` (UTC by default)."""
    base = today or dt.date.today()
    return base - dt.timedelta(days=int(n))


def month_chunks(date_from: dt.date, date_to: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Split a date range into ``[(month_start, month_end), …]`` inclusive
    windows, descending (newest first) so the user sees recent matches earlier."""
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    out: list[tuple[dt.date, dt.date]] = []
    y, m = date_to.year, date_to.month
    end_y, end_m = date_from.year, date_from.month
    while (y, m) >= (end_y, end_m):
        first = dt.date(y, m, 1)
        last = dt.date(y, m, calendar.monthrange(y, m)[1])
        a = max(first, date_from)
        b = min(last, date_to)
        if a <= b:
            out.append((a, b))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


# ---------------------------------------------------------------------------
# Search runner
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SearchStats:
    pages_fetched: int = 0
    matches_total: int = 0
    matches_unique: int = 0
    chunks: int = 0
    chunks_completed: list[str] = dataclasses.field(default_factory=list)
    chunks_skipped: list[str] = dataclasses.field(default_factory=list)
    truncated_chunks: list[str] = dataclasses.field(default_factory=list)


class SearchRunner:
    """Execute a :class:`SearchPlan` against a live :class:`SlackWebClient`.

    Use ``iter_matches()`` to stream results to a writer (memory-friendly),
    or ``run()`` to materialise everything into a list (convenience).

    The runner deduplicates on ``(channel_id, ts)`` so overlap between
    chunks doesn't surface duplicate messages.
    """

    def __init__(
        self,
        client: SlackWebClient,
        resolver: EntityResolver,
        *,
        on_progress: Callable[[str], None] | None = None,
        skip_chunks: set[str] | None = None,
        deadline: float | None = None,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._on_progress = on_progress or (lambda s: sys.stderr.write(f"[slackwright] {s}\n"))
        self._skip_chunks = skip_chunks or set()
        self._deadline = deadline
        self.stats = SearchStats()

    # --- public ---

    def iter_matches(self, plan: SearchPlan) -> Iterator[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        chunks = self._chunks_for(plan)
        self.stats.chunks = len(chunks)
        emitted = 0
        for chunk_idx, (a, b) in enumerate(chunks, start=1):
            label = chunk_label(a, b)
            if label in self._skip_chunks:
                self.stats.chunks_skipped.append(label)
                self._on_progress(f"chunk {chunk_idx}/{len(chunks)} {label} — skipping (resume)")
                continue
            self._on_progress(
                f"chunk {chunk_idx}/{len(chunks)} {label}  query={build_query(plan, after=a, before=b)!r}"
            )
            self._check_deadline()
            try:
                truncated = False
                for msg in self._iter_chunk(plan, after=a, before=b):
                    cid = (msg.get("channel") or {}).get("id") or msg.get("channel_id") or ""
                    ts = msg.get("ts") or ""
                    key = (cid, ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield msg
                    emitted += 1
                    if plan.max_results is not None and emitted >= plan.max_results:
                        self._on_progress(f"hit --max {plan.max_results}; stopping early")
                        return
                    self._check_deadline()
            except _ChunkTruncated:
                truncated = True
            if truncated:
                self.stats.truncated_chunks.append(label)
                self._on_progress(
                    f"  WARN: chunk {label} hit search cap ({SEARCH_MAX_RESULTS}); "
                    f"some matches may be missing. Re-run with a narrower window."
                )
            self.stats.chunks_completed.append(label)
        self.stats.matches_unique = emitted

    # --- internals ---

    def _check_deadline(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise SearchTimeoutError(
                f"fetch timed out after the configured --timeout window; "
                f"chunks completed: {len(self.stats.chunks_completed)}, "
                f"unique matches so far: {self.stats.matches_unique}"
            )

    def run(self, plan: SearchPlan) -> list[dict[str, Any]]:
        return list(self.iter_matches(plan))

    # --- internals ---

    def _chunks_for(self, plan: SearchPlan) -> list[tuple[dt.date | None, dt.date | None]]:
        if plan.date_from is None and plan.date_to is None:
            return [(None, None)]
        a = plan.date_from or dt.date(2010, 1, 1)
        b = plan.date_to or dt.date.today()
        chunks = month_chunks(a, b)
        return [(c0, c1) for (c0, c1) in chunks]

    def _iter_chunk(
        self,
        plan: SearchPlan,
        *,
        after: dt.date | None,
        before: dt.date | None,
    ) -> Iterator[dict[str, Any]]:
        query = build_query(plan, after=after, before=before)
        page = 1
        page_count = 1
        total = 0
        while page <= SEARCH_MAX_PAGE:
            resp = self._search_page(query, page=page)
            self.stats.pages_fetched += 1
            messages = resp.get("messages") or {}
            matches = messages.get("matches") or []
            paging = messages.get("paging") or {}
            if page == 1:
                total = int(
                    paging.get("total")
                    or messages.get("total")
                    or len(matches)
                )
                page_count = int(paging.get("pages") or 1)
                if total >= SEARCH_MAX_RESULTS:
                    # We can still stream what's on this page; the caller
                    # handles the truncation warning post-loop.
                    pass
            self.stats.matches_total += len(matches)
            yield from matches
            full_page = len(matches) >= SEARCH_PER_PAGE
            if page >= page_count and not full_page:
                break
            if not full_page:
                break
            page += 1
        if total >= SEARCH_MAX_RESULTS:
            raise _ChunkTruncated()

    def _search_page(self, query: str, *, page: int) -> dict[str, Any]:
        try:
            data = self._client.api(
                "search.modules.messages",
                {
                    "query": query,
                    "count": SEARCH_PER_PAGE,
                    "page": page,
                    "module": "messages",
                    "sort": "timestamp",
                    "sort_dir": "desc",
                    "extracts": 1,
                    "highlight": 1,
                    "extra_message_data": 1,
                    "query_rewrite_disabled": "true",
                },
            )
        except SlackWebError as e:
            # search.modules.messages emits "no_results" / "no_search_results"
            # for empty windows; treat as a soft empty.
            if e.error in {"no_results", "no_search_results", "empty"}:
                return {"messages": {"matches": [], "paging": {"total": 0, "pages": 0}}}
            raise
        # Slack's response envelope has `items` / `messages` / etc. Normalise.
        if "messages" in data:
            return data
        # `search.modules.messages` returns flat: items + pagination
        items = data.get("items") or []
        pagination = data.get("pagination") or {}
        paging = data.get("paging") or {
            "total": int(pagination.get("total_count") or len(items)),
            "pages": int(pagination.get("page_count") or 1),
            "page": int(pagination.get("page") or 1),
            "count": SEARCH_PER_PAGE,
        }
        return {"messages": {"matches": items, "paging": paging, "pagination": pagination}}


class _ChunkTruncated(Exception):
    """Internal signal — a chunk hit the SEARCH_MAX_RESULTS cap."""
