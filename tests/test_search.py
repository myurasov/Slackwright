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

"""Tests for :mod:`slackwright.search` — query builder + paginated runner."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from slackwright.resolver import (
    ChannelRecord,
    EntityResolver,
    ResolvedChannel,
    ResolvedUser,
    UserRecord,
)
from slackwright.search import (
    SEARCH_PER_PAGE,
    SearchPlan,
    SearchRunner,
    build_query,
    days_back,
    month_chunks,
    parse_date,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _u(id_: str, handle: str | None) -> ResolvedUser:
    return ResolvedUser(UserRecord(id=id_, name=handle))


def _c(id_: str, name: str | None, type_: str = "channel") -> ResolvedChannel:
    return ResolvedChannel(ChannelRecord(id=id_, name=name, type=type_))


# ---------------------------------------------------------------------------
# date helpers
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_iso_dash(self) -> None:
        assert parse_date("2026-04-25") == dt.date(2026, 4, 25)

    def test_iso_slash(self) -> None:
        assert parse_date("2026/04/25") == dt.date(2026, 4, 25)

    def test_compact_digits(self) -> None:
        assert parse_date("20260425") == dt.date(2026, 4, 25)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_date("")


class TestDaysBack:
    def test_basic(self) -> None:
        today = dt.date(2026, 5, 1)
        assert days_back(7, today=today) == dt.date(2026, 4, 24)

    def test_zero(self) -> None:
        today = dt.date(2026, 5, 1)
        assert days_back(0, today=today) == today


class TestMonthChunks:
    def test_single_month(self) -> None:
        chunks = month_chunks(dt.date(2026, 4, 5), dt.date(2026, 4, 25))
        assert chunks == [(dt.date(2026, 4, 5), dt.date(2026, 4, 25))]

    def test_two_months(self) -> None:
        chunks = month_chunks(dt.date(2026, 3, 15), dt.date(2026, 4, 10))
        assert chunks[0] == (dt.date(2026, 4, 1), dt.date(2026, 4, 10))
        assert chunks[1] == (dt.date(2026, 3, 15), dt.date(2026, 3, 31))

    def test_descending_order(self) -> None:
        chunks = month_chunks(dt.date(2026, 1, 1), dt.date(2026, 6, 30))
        # newest first
        first_starts = [c[0].month for c in chunks]
        assert first_starts == sorted(first_starts, reverse=True)

    def test_swaps_inverted_args(self) -> None:
        a = month_chunks(dt.date(2026, 4, 1), dt.date(2026, 3, 1))
        b = month_chunks(dt.date(2026, 3, 1), dt.date(2026, 4, 1))
        assert a == b


# ---------------------------------------------------------------------------
# query builder
# ---------------------------------------------------------------------------


class TestBuildQuery:
    def test_only_from(self) -> None:
        plan = SearchPlan(from_user=_u("U1", "alice"))
        assert build_query(plan) == "from:@alice"

    def test_handle_fallback_to_id(self) -> None:
        plan = SearchPlan(from_user=_u("U1", None))
        assert build_query(plan) == "from:U1"

    def test_to_and_in(self) -> None:
        plan = SearchPlan(to_user=_u("U2", "bob"), in_channel=_c("C1", "general"))
        q = build_query(plan)
        assert "to:@bob" in q
        assert "in:#general" in q

    def test_with_user(self) -> None:
        plan = SearchPlan(with_user=_u("U3", "carla"))
        assert "with:@carla" in build_query(plan)

    def test_extra_query_appended(self) -> None:
        plan = SearchPlan(from_user=_u("U1", "alice"), extra_query="kubernetes has:link")
        q = build_query(plan)
        assert q.startswith("from:@alice ")
        assert q.endswith("kubernetes has:link")

    def test_dates_inclusive_translation(self) -> None:
        plan = SearchPlan(
            from_user=_u("U1", "alice"),
            date_from=dt.date(2026, 4, 1),
            date_to=dt.date(2026, 4, 30),
        )
        q = build_query(plan)
        # Slack's after: is exclusive, so we expect 2026-03-31 (one day before 04-01)
        assert "after:2026-03-31" in q
        # before: is exclusive, so we expect 2026-05-01 (one day after 04-30)
        assert "before:2026-05-01" in q

    def test_after_and_before_overrides(self) -> None:
        plan = SearchPlan(from_user=_u("U1", "alice"))
        q = build_query(plan, after=dt.date(2026, 1, 1), before=dt.date(2026, 1, 31))
        assert "after:2025-12-31" in q
        assert "before:2026-02-01" in q


# ---------------------------------------------------------------------------
# SearchRunner
# ---------------------------------------------------------------------------


def _make_runner(state_dir: Path, fake_client) -> SearchRunner:
    resolver = EntityResolver(fake_client, state_dir=state_dir)
    return SearchRunner(fake_client, resolver, on_progress=lambda _: None)


def _match(channel_id: str, ts: str, text: str = "hi") -> dict[str, Any]:
    return {
        "channel": {"id": channel_id, "name": "general"},
        "ts": ts,
        "user": "UALICE00",
        "text": text,
        "permalink": f"https://acme.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
    }


class TestSearchRunnerSinglePage:
    def test_no_dates_single_chunk(self, state_dir: Path, fake_client) -> None:
        fake_client.register(
            "search.modules.messages",
            {
                "ok": True,
                "items": [_match("CGENERAL", "1700000001.000100"), _match("CGENERAL", "1700000002.000100")],
                "paging": {"total": 2, "pages": 1},
            },
        )
        runner = _make_runner(state_dir, fake_client)
        plan = SearchPlan(from_user=_u("UALICE00", "alice"))
        out = runner.run(plan)
        assert len(out) == 2
        assert runner.stats.chunks == 1
        assert runner.stats.matches_unique == 2

    def test_dedup_across_chunks(self, state_dir: Path, fake_client) -> None:
        # Two date chunks (one per month). Both return the SAME match — must
        # only emit it once.
        shared = _match("CGENERAL", "1700000003.000100")
        fake_client.register_handler(
            "search.modules.messages",
            lambda body: {
                "ok": True,
                "items": [shared],
                "paging": {"total": 1, "pages": 1},
            },
        )
        runner = _make_runner(state_dir, fake_client)
        plan = SearchPlan(
            from_user=_u("UALICE00", "alice"),
            date_from=dt.date(2026, 3, 1),
            date_to=dt.date(2026, 4, 30),
        )
        out = runner.run(plan)
        assert len(out) == 1


class TestSearchRunnerPagination:
    def test_walks_multiple_pages(self, state_dir: Path, fake_client) -> None:
        # Simulate 2 pages. First page returns 100 items, second returns 1.
        page1 = [_match("CGENERAL", f"170000000{i:02d}.000100") for i in range(100)]
        page2 = [_match("CGENERAL", "1700000099.000200")]

        def handler(body: dict[str, Any]) -> dict[str, Any]:
            page = int(body.get("page") or 1)
            if page == 1:
                return {"ok": True, "items": page1, "paging": {"total": 101, "pages": 2}}
            return {"ok": True, "items": page2, "paging": {"total": 101, "pages": 2}}

        fake_client.register_handler("search.modules.messages", handler)
        runner = _make_runner(state_dir, fake_client)
        plan = SearchPlan(from_user=_u("UALICE00", "alice"))
        out = runner.run(plan)
        assert len(out) == 101
        assert runner.stats.pages_fetched == 2

    def test_max_results_respected(self, state_dir: Path, fake_client) -> None:
        page1 = [_match("CGENERAL", f"170000000{i:02d}.000100") for i in range(SEARCH_PER_PAGE)]
        fake_client.register(
            "search.modules.messages",
            {"ok": True, "items": page1, "paging": {"total": SEARCH_PER_PAGE, "pages": 1}},
        )
        runner = _make_runner(state_dir, fake_client)
        plan = SearchPlan(from_user=_u("UALICE00", "alice"), max_results=10)
        out = runner.run(plan)
        assert len(out) == 10


class TestSearchRunnerSoftEmpty:
    def test_no_results_error_treated_as_empty(self, state_dir: Path, fake_client) -> None:
        from slackwright.client import SlackWebError

        def handler(body: dict[str, Any]) -> dict[str, Any]:
            raise SlackWebError("search.modules.messages", "no_results")

        fake_client.register_handler("search.modules.messages", handler)
        runner = _make_runner(state_dir, fake_client)
        out = runner.run(SearchPlan(from_user=_u("UALICE00", "alice")))
        assert out == []
