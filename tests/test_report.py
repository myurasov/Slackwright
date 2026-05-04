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

"""Tests for :mod:`slackwright.report` — HTML report renderer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from slackwright.archive import ArchiveWriter
from slackwright.report import _format_plain, _render_text, render_report
from slackwright.resolver import ChannelRecord, EntityResolver, UserRecord

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestFormatPlain:
    def test_html_escapes(self) -> None:
        assert "&lt;script&gt;" in _format_plain("<script>")

    def test_inline_code(self) -> None:
        assert "<code>foo</code>" in _format_plain("`foo`")

    def test_code_block(self) -> None:
        assert "<pre><code>" in _format_plain("```py\nx = 1\n```")

    def test_bold(self) -> None:
        assert "<strong>hi</strong>" in _format_plain("*hi*")

    def test_bare_url(self) -> None:
        out = _format_plain("see https://example.com here")
        assert '<a href="https://example.com"' in out


class TestRenderText:
    def test_user_mention_with_label(self) -> None:
        out = _render_text("hi <@UALICE00|alice>", {}, {})
        assert "@alice" in out
        assert 'class="mention"' in out

    def test_user_mention_via_cache(self) -> None:
        users = {"UALICE00": {"id": "UALICE00", "name": "alice", "real_name": "Alice"}}
        out = _render_text("hi <@UALICE00>", users, {})
        assert "@alice" in out

    def test_channel_mention_via_cache(self) -> None:
        channels = {"CGENERAL": {"id": "CGENERAL", "name": "general"}}
        out = _render_text("see <#CGENERAL>", {}, channels)
        assert "#general" in out

    def test_link_with_label(self) -> None:
        out = _render_text("<https://example.com|click here>", {}, {})
        assert 'href="https://example.com"' in out
        assert "click here" in out


# ---------------------------------------------------------------------------
# End-to-end report rendering
# ---------------------------------------------------------------------------


def _seed_archive(out: Path, *, with_files: bool = False) -> None:
    """Write a small slackwright archive that report.py can render."""
    resolver = EntityResolver(client=None, state_dir=out / ".state")
    resolver.remember_user(UserRecord(
        id="UALICE00", name="alice", real_name="Alice Engineer",
        email="alice@example.com",
    ))
    resolver.remember_user(UserRecord(
        id="UBOB0001", name="bob.builder", real_name="Bob Builder",
        email="bob@example.com",
    ))
    resolver.remember_channel(ChannelRecord(
        id="CGENERAL", name="general", type="channel", is_private=False,
        purpose="company chat",
    ))

    def msg(channel: str, ts: str, user: str, text: str,
            *, files: list[dict[str, Any]] | None = None,
            thread_ts: str | None = None) -> dict[str, Any]:
        m: dict[str, Any] = {
            "channel": {"id": channel, "name": "general"},
            "ts": ts,
            "user": user,
            "text": text,
            "permalink": f"https://acme.slack.com/archives/{channel}/p{ts.replace('.', '')}",
        }
        if files:
            m["files"] = files
        if thread_ts:
            m["thread_ts"] = thread_ts
        return m

    files = (
        [{"id": "F1ABC123", "name": "screenshot.png", "mode": "hosted",
          "url_private": "https://files.slack.com/files-pri/T0/F1ABC123/screenshot.png"}]
        if with_files else None
    )
    messages = [
        msg("CGENERAL", "1745613600.000000", "UALICE00",
            "Hi <@UBOB0001> — here's the *plan*:\n```python\nprint('hi')\n```",
            files=files),
        msg("CGENERAL", "1745613601.000000", "UBOB0001",
            "thanks!", thread_ts="1745613600.000000"),
    ]
    writer = ArchiveWriter(
        out, resolver=resolver, sa_user_id="UALICE00",
        format="archive", plan_summary="from=@alice",
    )
    for m in messages:
        writer.write_match(m)
    writer.write_users_cache(resolver)
    writer.write_channels_cache(resolver)
    writer.write_index(plan_summary="from=@alice", search_query="from:@alice",
                      cost={"api_calls": 4, "elapsed_ms": 1234, "bytes_in": 8192,
                            "rate_limited_seconds": 0})


class TestRenderReport:
    def test_writes_html_with_default_target(self, tmp_path: Path) -> None:
        _seed_archive(tmp_path)
        target = render_report(tmp_path)
        assert target == tmp_path / "report.html"
        assert target.exists()
        text = target.read_text(encoding="utf-8")
        # Basic structural assertions
        assert "<!DOCTYPE html>" in text
        assert "<title>" in text
        assert "Alice Engineer" in text   # author name resolved
        assert "alice@example.com" in text  # author email rendered
        assert "thanks!" in text
        assert "from:@alice" in text       # query embedded in run-meta
        # Inline CSS, no external assets
        assert "<style>" in text
        assert "<script" not in text
        # Channel and thread structure
        assert "general" in text          # channel name
        assert "company chat" in text     # purpose
        assert 'class="mention"' in text   # @-mention rendered

    def test_custom_target_path(self, tmp_path: Path) -> None:
        _seed_archive(tmp_path)
        custom = tmp_path / "elsewhere" / "report.html"
        custom.parent.mkdir()
        target = render_report(tmp_path, target=custom)
        assert target == custom
        assert custom.exists()

    def test_custom_title(self, tmp_path: Path) -> None:
        _seed_archive(tmp_path)
        target = render_report(tmp_path, title="Q2 sweep")
        text = target.read_text(encoding="utf-8")
        assert "Q2 sweep" in text

    def test_with_files_section(self, tmp_path: Path) -> None:
        _seed_archive(tmp_path, with_files=True)
        target = render_report(tmp_path)
        text = target.read_text(encoding="utf-8")
        assert "screenshot.png" in text
        assert "_files/F1ABC123/screenshot.png" in text

    def test_renders_cost_block(self, tmp_path: Path) -> None:
        _seed_archive(tmp_path)
        target = render_report(tmp_path)
        text = target.read_text(encoding="utf-8")
        assert "4 API calls" in text or "4 api calls" in text.lower()
        assert "1,234 ms" in text

    def test_empty_archive(self, tmp_path: Path) -> None:
        # Just an _index.yaml, no messages.
        (tmp_path / "_index.yaml").write_text(yaml.safe_dump({
            "schema_version": 1, "tool": "slackwright", "plan": "(empty)",
            "query": "", "counts": {"created": 0},
        }))
        target = render_report(tmp_path)
        text = target.read_text(encoding="utf-8")
        assert "No messages in this archive." in text

    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            render_report(tmp_path / "doesnotexist")

    def test_round_trips_with_describe_archive_data(self, tmp_path: Path) -> None:
        # Quick sanity: the seeded archive's _index.yaml is loadable and the
        # cost block we set is preserved.
        _seed_archive(tmp_path)
        idx = yaml.safe_load((tmp_path / "_index.yaml").read_text())
        assert idx["cost"]["api_calls"] == 4
        # Also verify the on-disk shape of one message file (no surprises
        # for the report renderer).
        msgs = list((tmp_path / "messages").rglob("*.json"))
        assert msgs, "expected per-message JSON files"
        body = json.loads(msgs[0].read_text())
        assert "channel_id" in body
        assert "_archive" in body
