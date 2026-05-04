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

"""End-to-end integration tests that exercise the full fetch → archive →
read-back pipeline against a mocked Slack web API. No live Playwright
session needed — we just substitute :class:`SlackWebClient` with the
:class:`FakeClient` from ``conftest.py``.

These prove that ``slackwright fetch`` produces a complete, internally
consistent archive: per-message JSON files, resolved ``_users`` /
``_channels`` YAMLs, an ``_index.yaml`` summary, and the ``matches.jsonl``
ledger all line up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from slackwright.archive import ArchiveWriter
from slackwright.resolver import EntityResolver, ResolvedUser, UserRecord
from slackwright.search import SearchPlan, SearchRunner


def _match(channel_id: str, ts: str, *, user: str = "UALICE00",
           text: str = "hi", channel_name: str = "general") -> dict[str, Any]:
    return {
        "channel": {"id": channel_id, "name": channel_name},
        "ts": ts,
        "user": user,
        "text": text,
        "permalink": f"https://acme.slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
    }


def _u(uid: str, handle: str | None) -> ResolvedUser:
    return ResolvedUser(UserRecord(id=uid, name=handle))


class TestEndToEndArchive:
    def test_fetch_writes_full_archive(self, state_dir: Path, out_dir: Path,
                                       fake_client) -> None:
        # Mock search results.
        fake_client.register(
            "search.modules.messages",
            {
                "ok": True,
                "items": [
                    _match("CGENERAL", "1745613600.000000", user="UALICE00",
                           text="hello team"),
                    _match("CGENERAL", "1745613601.000000", user="UBOB0001",
                           text="hi alice"),
                    _match("DBOB0001", "1745613602.000000", user="UBOB0001",
                           text="DM only", channel_name="UBOB0001"),
                ],
                "paging": {"total": 3, "pages": 1},
            },
        )
        resolver = EntityResolver(fake_client, state_dir=state_dir)
        # Seed the cache the way the real CLI does: by resolving the
        # --from arg first (which triggers the one-time users.list fetch).
        seed = resolver.resolve_user("alice")
        assert seed.id == "UALICE00"

        plan = SearchPlan(from_user=seed)
        runner = SearchRunner(fake_client, resolver, on_progress=lambda _: None)
        writer = ArchiveWriter(out_dir, resolver=resolver, sa_user_id="UALICE00",
                               format="archive", plan_summary=plan.display())

        all_msgs: list[dict[str, Any]] = []
        for msg in runner.iter_matches(plan):
            writer.write_match(msg)
            all_msgs.append(msg)
        assert len(all_msgs) == 3

        # Resolve every id we saw.
        resolver.resolve_users_in(writer.stats.user_ids_seen)
        resolver.resolve_channels_in(writer.stats.channel_ids_seen)
        resolver.save_caches()

        n_users = writer.write_users_cache(resolver)
        n_chans = writer.write_channels_cache(resolver)
        idx_path = writer.write_index(plan_summary=plan.display(),
                                      search_query="from:@alice")

        # --- post-conditions: per-message JSON files exist
        msg_files = sorted(out_dir.rglob("messages/*/*/*/*.json"))
        assert len(msg_files) == 3

        # --- direction tagging is correct
        bodies = [json.loads(p.read_text()) for p in msg_files]
        out_count = sum(1 for b in bodies if b["_archive"]["direction"] == "out")
        in_count = sum(1 for b in bodies if b["_archive"]["direction"] == "in")
        assert out_count == 1 and in_count == 2

        # --- user / channel YAML caches are populated
        assert n_users >= 2  # UALICE00, UBOB0001
        assert n_chans >= 2  # CGENERAL, DBOB0001
        u_yaml = yaml.safe_load((out_dir / "_users" / "UALICE00.yaml").read_text())
        assert u_yaml["email"] == "alice@example.com"

        # --- index summary matches reality
        idx = yaml.safe_load(idx_path.read_text())
        assert idx["counts"]["created"] == 3
        assert idx["counts"]["users_seen"] == 2
        assert idx["counts"]["channels_seen"] == 2

        # --- jsonl ledger has every match
        rows = (out_dir / "matches.jsonl").read_text().strip().splitlines()
        assert len(rows) == 3
        ts_set = {json.loads(r)["ts"] for r in rows}
        assert ts_set == {"1745613600.000000", "1745613601.000000", "1745613602.000000"}
