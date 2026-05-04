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

"""Tests for :mod:`slackwright.archive` — output writer + on-disk schema."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import yaml

from slackwright.archive import (
    ArchiveWriter,
    _channel_slug,
    _ts_to_local_date,
    message_filepath,
    message_key_hash,
    previously_completed_chunks,
    read_index,
    slugify,
)
from slackwright.resolver import (
    ChannelRecord,
    EntityResolver,
    UserRecord,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Hello World") == "hello-world"

    def test_strips_punctuation(self) -> None:
        assert slugify("foo, bar! baz?") == "foo-bar-baz"

    def test_truncates(self) -> None:
        s = slugify("a" * 100, max_len=10)
        assert len(s) <= 10
        assert s == "a" * 10


class TestChannelSlug:
    def test_named_channel(self) -> None:
        assert _channel_slug("CGENERAL", "general", "channel") == "general"

    def test_im_fallback(self) -> None:
        assert _channel_slug("D0634N8J8P6", None, "im") == "im-34n8j8p6"

    def test_mpim_fallback(self) -> None:
        assert _channel_slug("G0123ABCDE", None, "mpim") == "mpim-123abcde"

    def test_user_id_pretending_to_be_name(self) -> None:
        # Slack's IM channel.name is sometimes set to the other user's id;
        # that should fall through to the prefix-based slug.
        assert _channel_slug("D0634N8J8P6", "U06HYSK2P2L", "im") == "im-34n8j8p6"


class TestKeyHash:
    def test_stable(self) -> None:
        h1 = message_key_hash("CGENERAL", "1700000001.000100")
        h2 = message_key_hash("CGENERAL", "1700000001.000100")
        assert h1 == h2
        assert len(h1) == 64

    def test_distinct(self) -> None:
        assert message_key_hash("CCHANNAA", "1.0") != message_key_hash("CCHANBB1", "1.0")
        assert message_key_hash("CCHANNAA", "1.0") != message_key_hash("CCHANNAA", "2.0")


class TestTsToLocalDate:
    def test_known_ts(self) -> None:
        # 2026-04-25 19:00 UTC ≈ 12:00 PT — depends on machine TZ,
        # so just sanity-check the format.
        d = _ts_to_local_date("1745613600.000000")
        assert len(d) == 10
        dt.date.fromisoformat(d)  # parses cleanly

    def test_bad_ts_falls_back_to_today(self) -> None:
        d = _ts_to_local_date("not-a-number")
        dt.date.fromisoformat(d)


# ---------------------------------------------------------------------------
# message_filepath layout
# ---------------------------------------------------------------------------


class TestMessageFilepath:
    def test_layout(self, tmp_path: Path) -> None:
        p = message_filepath(
            tmp_path,
            "CGENERAL",
            "1745613600.000000",
            channel_name="general",
            channel_type="channel",
        )
        # tmp_path/messages/YYYY/MM/DD/<file>.json
        rel = p.relative_to(tmp_path)
        assert rel.parts[0] == "messages"
        assert len(rel.parts) == 5  # messages/yyyy/mm/dd/file.json
        # Filename starts with YYYY-MM-DD where YYYY/MM/DD match the dir parts
        yyyy, mm, dd = rel.parts[1], rel.parts[2], rel.parts[3]
        assert rel.parts[4].startswith(f"{yyyy}-{mm}-{dd}-")
        assert rel.parts[4].endswith(".json")
        assert "general" in rel.parts[4]


# ---------------------------------------------------------------------------
# ArchiveWriter
# ---------------------------------------------------------------------------


def _match(
    channel_id: str,
    ts: str,
    *,
    user: str = "UALICE00",
    text: str = "hi",
    channel_name: str = "general",
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "channel": {"id": channel_id, "name": channel_name},
        "ts": ts,
        "user": user,
        "text": text,
        "permalink": f"https://acme.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
    }
    if files:
        m["files"] = files
    return m


class TestArchiveWriterArchiveFormat:
    def test_writes_per_message_json(self, out_dir: Path) -> None:
        w = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        outcome = w.write_match(_match("CGENERAL", "1745613600.000000"))
        assert outcome == "created"
        files = list(out_dir.glob("messages/*/*/*/*.json"))
        assert len(files) == 1
        body = json.loads(files[0].read_text())
        assert body["channel_id"] == "CGENERAL"
        assert body["text"] == "hi"
        assert body["_archive"]["direction"] == "out"  # author == sa_user_id
        assert body["_archive"]["source_tool"] == "slackwright"

    def test_direction_in_for_other_sender(self, out_dir: Path) -> None:
        w = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w.write_match(_match("CGENERAL", "1745613601.000000", user="UBOB0001"))
        files = list(out_dir.glob("messages/*/*/*/*.json"))
        body = json.loads(files[0].read_text())
        assert body["_archive"]["direction"] == "in"

    def test_idempotent_noop(self, out_dir: Path) -> None:
        w1 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w1.write_match(_match("CGENERAL", "1745613602.000000"))

        w2 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        outcome = w2.write_match(_match("CGENERAL", "1745613602.000000"))
        assert outcome == "noop"

    def test_updated_when_text_changes(self, out_dir: Path) -> None:
        w1 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w1.write_match(_match("CGENERAL", "1745613603.000000", text="original"))
        w2 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        outcome = w2.write_match(_match("CGENERAL", "1745613603.000000", text="edited"))
        assert outcome == "updated"

    def test_appends_to_jsonl_ledger(self, out_dir: Path) -> None:
        w = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w.write_match(_match("CGENERAL", "1745613604.000000"))
        w.write_match(_match("CENGTEAM", "1745613605.000000"))
        rows = (out_dir / "matches.jsonl").read_text().strip().splitlines()
        assert len(rows) == 2
        assert all(json.loads(r)["channel_id"] for r in rows)

    def test_jsonl_unions_across_runs(self, out_dir: Path) -> None:
        # Consecutive runs into the same --out should produce a real
        # union — prior rows preserved, new rows appended, dupes
        # (same channel_id+ts seen by both runs) collapsed.
        w1 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w1.write_match(_match("CGENERAL", "1745613606.000000"))
        w1.write_match(_match("CSHARED", "1745613608.000000"))
        w2 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w2.write_match(_match("CSHARED", "1745613608.000000"))  # dup → skipped
        w2.write_match(_match("CENGTEAM", "1745613607.000000"))
        rows = (out_dir / "matches.jsonl").read_text().strip().splitlines()
        assert len(rows) == 3
        ids = [json.loads(r)["channel_id"] for r in rows]
        assert ids == ["CGENERAL", "CSHARED", "CENGTEAM"]


class TestArchiveWriterIndex:
    def test_index_summary(self, out_dir: Path) -> None:
        w = ArchiveWriter(
            out_dir, sa_user_id="UALICE00", format="archive", plan_summary="from=@alice"
        )
        w.write_match(_match("CGENERAL", "1745613608.000000", user="UALICE00"))
        w.write_match(
            _match("DBOB0001", "1745613609.000000", user="UBOB0001", channel_name="UBOB0001")
        )
        idx_path = w.write_index(plan_summary="from=@alice", search_query="from:@alice")
        idx = yaml.safe_load(idx_path.read_text())
        assert idx["tool"] == "slackwright"
        assert idx["counts"]["created"] == 2
        assert idx["counts"]["users_seen"] == 2
        assert idx["counts"]["channels_seen"] == 2
        # Channel type counts: one channel, one im (D-prefix → inferred)
        assert idx["counts"]["by_channel_type"].get("channel", 0) >= 1


class TestArchiveWriterUserChannelCaches:
    def test_writes_user_yamls(self, out_dir: Path, state_dir: Path) -> None:
        # Build a resolver pre-loaded with user records (no live client).
        resolver = EntityResolver(client=None, state_dir=state_dir)
        resolver.remember_user(
            UserRecord(
                id="UALICE00",
                name="alice",
                real_name="Alice Engineer",
                email="alice@example.com",
            )
        )
        resolver.remember_user(UserRecord(id="UBOB0001", name="bob", real_name="Bob"))
        resolver.remember_channel(
            ChannelRecord(
                id="CGENERAL",
                name="general",
                type="channel",
                is_private=False,
            )
        )

        w = ArchiveWriter(out_dir, resolver=resolver, sa_user_id="UALICE00", format="archive")
        w.write_match(_match("CGENERAL", "1745613610.000000", user="UALICE00"))
        w.write_match(_match("CGENERAL", "1745613611.000000", user="UBOB0001"))

        n_users = w.write_users_cache(resolver)
        n_chans = w.write_channels_cache(resolver)
        assert n_users == 2
        assert n_chans == 1

        u_yaml = yaml.safe_load((out_dir / "_users" / "UALICE00.yaml").read_text())
        assert u_yaml["email"] == "alice@example.com"
        c_yaml = yaml.safe_load((out_dir / "_channels" / "CGENERAL.yaml").read_text())
        assert c_yaml["name"] == "general"


# ---------------------------------------------------------------------------
# read_index + previously_completed_chunks (resume support)
# ---------------------------------------------------------------------------


class TestReadIndex:
    def test_returns_none_when_missing(self, out_dir: Path) -> None:
        assert read_index(out_dir) is None

    def test_loads_yaml(self, out_dir: Path) -> None:
        w = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w.write_match(_match("CGENERAL", "1745613600.000000"))
        w.write_index(plan_summary="from=@alice", search_query="from:@alice")
        idx = read_index(out_dir)
        assert idx is not None
        assert idx["tool"] == "slackwright"
        assert idx["plan"] == "from=@alice"


class TestPreviouslyCompletedChunks:
    def test_empty_when_no_index(self, out_dir: Path) -> None:
        assert previously_completed_chunks(out_dir) == set()

    def test_empty_when_no_chunks_recorded(self, out_dir: Path) -> None:
        w = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w.write_match(_match("CGENERAL", "1745613600.000000"))
        w.write_index(plan_summary="from=@alice", search_query="from:@alice")
        assert previously_completed_chunks(out_dir) == set()

    def test_reads_from_extra_search_stats(self, out_dir: Path) -> None:
        w = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w.write_match(_match("CGENERAL", "1745613600.000000"))
        w.write_index(
            plan_summary="from=@alice",
            search_query="from:@alice",
            extra={
                "search_stats": {
                    "chunks_completed": ["2026-04-01..2026-04-30", "2026-03-01..2026-03-31"],
                }
            },
        )
        completed = previously_completed_chunks(out_dir)
        assert completed == {"2026-04-01..2026-04-30", "2026-03-01..2026-03-31"}

    def test_cost_block_in_index(self, out_dir: Path) -> None:
        w = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w.write_match(_match("CGENERAL", "1745613600.000000"))
        w.write_index(
            plan_summary="from=@alice",
            search_query="from:@alice",
            cost={"api_calls": 12, "elapsed_ms": 5000},
        )
        idx = read_index(out_dir)
        assert idx["cost"]["api_calls"] == 12
        assert idx["cost"]["elapsed_ms"] == 5000


class TestMultiRunIndex:
    def test_index_accumulates_runs(self, out_dir: Path) -> None:
        # Run 1: from-me
        w1 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w1.write_match(_match("CGENERAL", "1745613600.000000", user="UALICE00"))
        w1.write_index(plan_summary="from=@alice", search_query="from:@alice", cost={"api_calls": 4})

        # Run 2: to-me
        w2 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w2.write_match(_match("CGENERAL", "1745613601.000000", user="UBOB0001"))
        w2.write_index(plan_summary="to=@alice", search_query="to:@alice", cost={"api_calls": 7})

        idx = read_index(out_dir)
        assert idx["schema_version"] == 2
        # Top-level reflects latest run (backward-compat)
        assert idx["plan"] == "to=@alice"
        assert idx["query"] == "to:@alice"
        # Aggregate counts: created=1+1=2
        assert idx["counts"]["created"] == 2
        # Per-run records preserved
        assert len(idx["runs"]) == 2
        queries = [r["query"] for r in idx["runs"]]
        assert queries == ["from:@alice", "to:@alice"]
        # Each run keeps its own cost
        assert idx["runs"][0]["cost"]["api_calls"] == 4
        assert idx["runs"][1]["cost"]["api_calls"] == 7

    def test_legacy_v1_index_migrates_on_next_run(self, out_dir: Path) -> None:
        # Hand-craft a v1 single-run index, then run a v2 fetch into the same out.
        legacy = {
            "schema_version": 1,
            "tool": "slackwright",
            "plan": "from=@bob",
            "query": "from:@bob",
            "captured_at": "2024-12-31T00:00:00-08:00",
            "last_updated": "2024-12-31T00:01:00-08:00",
            "format": "archive",
            "counts": {"created": 5, "updated": 0, "noop": 0, "by_month": {"2024-12": 5}},
            "extra": {"search_stats": {"chunks_completed": ["2024-12-01..2024-12-31"]}},
        }
        (out_dir / "_index.yaml").write_text(yaml.safe_dump(legacy), encoding="utf-8")

        w = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w.write_match(_match("CGENERAL", "1745613600.000000", user="UALICE00"))
        w.write_index(plan_summary="from=@alice", search_query="from:@alice")

        idx = read_index(out_dir)
        assert idx["schema_version"] == 2
        assert len(idx["runs"]) == 2
        # Legacy run preserved as the first entry
        assert idx["runs"][0]["query"] == "from:@bob"
        assert idx["runs"][1]["query"] == "from:@alice"
        # Aggregate sums legacy + new
        assert idx["counts"]["created"] == 5 + 1

    def test_resume_matches_by_query(self, out_dir: Path) -> None:
        # Two runs: --from finishes its chunks, --to runs separately.
        # Resuming --from should skip its chunks; resuming --to should
        # not (those chunks were never run for --to).
        w1 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w1.write_match(_match("CGENERAL", "1745613600.000000"))
        w1.write_index(
            plan_summary="from=@alice",
            search_query="from:@alice",
            extra={"search_stats": {"chunks_completed": ["2026-04-01..2026-04-30"]}},
        )
        w2 = ArchiveWriter(out_dir, sa_user_id="UALICE00", format="archive")
        w2.write_match(_match("CGENERAL", "1745613601.000000"))
        w2.write_index(
            plan_summary="to=@alice",
            search_query="to:@alice",
            extra={"search_stats": {"chunks_completed": ["2026-04-01..2026-04-30"]}},
        )
        # Resume against --from query: completed chunks visible
        assert previously_completed_chunks(out_dir, query="from:@alice") == {
            "2026-04-01..2026-04-30"
        }
        # Resume against an unrelated query: nothing to skip
        assert previously_completed_chunks(out_dir, query="from:@nobody") == set()
        # Without query arg: returns the latest run (existing behavior)
        assert previously_completed_chunks(out_dir) == {"2026-04-01..2026-04-30"}
