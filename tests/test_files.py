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

"""Tests for :mod:`slackwright.files` — attachment downloader."""

from __future__ import annotations

import json
from pathlib import Path

from slackwright.files import FileDownloader, _safe_filename, iter_files_in_messages


class TestSafeFilename:
    def test_alphanumeric(self) -> None:
        assert _safe_filename("hello.png", "fid") == "hello.png"

    def test_strips_unicode(self) -> None:
        assert "_" in _safe_filename("héllo.png", "fid")

    def test_fallback_when_empty(self) -> None:
        assert _safe_filename("", "F123") == "F123"

    def test_fallback_when_only_special(self) -> None:
        assert _safe_filename("..", "F123") == "F123"


class TestIterFiles:
    def test_message_files(self) -> None:
        msgs = [
            {"files": [{"id": "F1", "name": "a.png"}, {"id": "F2", "name": "b.pdf"}]},
        ]
        out = list(iter_files_in_messages(msgs))
        assert [f["id"] for f in out] == ["F1", "F2"]

    def test_attachment_files(self) -> None:
        msgs = [
            {"attachments": [{"files": [{"id": "F3", "name": "c.png"}]}]},
        ]
        out = list(iter_files_in_messages(msgs))
        assert [f["id"] for f in out] == ["F3"]

    def test_skips_tombstones(self) -> None:
        msgs = [
            {"files": [{"id": "F4", "name": "deleted.png", "mode": "tombstone"},
                       {"id": "F5", "name": "ok.png"}]}
        ]
        out = list(iter_files_in_messages(msgs))
        assert [f["id"] for f in out] == ["F5"]

    def test_skips_missing_id(self) -> None:
        msgs = [{"files": [{"name": "no-id.png"}]}]
        assert list(iter_files_in_messages(msgs)) == []


class TestFileDownloader:
    def test_downloads_and_writes(self, tmp_path: Path, fake_client) -> None:
        url = "https://files.slack.com/files-pri/T1/F1/hello.png"
        fake_client.register_file(url, b"PNG-bytes")
        msgs = [
            {
                "files": [{
                    "id": "F1",
                    "name": "hello.png",
                    "url_private_download": url,
                    "url_private": url,
                }]
            }
        ]
        d = FileDownloader(fake_client, tmp_path, on_progress=lambda _: None)
        stats = d.download_for_messages(msgs)
        assert stats.attempted == 1
        assert stats.downloaded == 1
        assert stats.bytes_total == len(b"PNG-bytes")
        assert (tmp_path / "_files" / "F1" / "hello.png").read_bytes() == b"PNG-bytes"
        assert json.loads((tmp_path / "_files" / "F1" / "_meta.json").read_text())["id"] == "F1"

    def test_idempotent_skip(self, tmp_path: Path, fake_client) -> None:
        url = "https://files.slack.com/files-pri/T1/F1/hello.png"
        fake_client.register_file(url, b"PNG-bytes")
        msg = {
            "files": [{
                "id": "F1",
                "name": "hello.png",
                "url_private_download": url,
            }]
        }
        d1 = FileDownloader(fake_client, tmp_path, on_progress=lambda _: None)
        d1.download_for_messages([msg])
        d2 = FileDownloader(fake_client, tmp_path, on_progress=lambda _: None)
        stats = d2.download_for_messages([msg])
        assert stats.attempted == 1
        assert stats.downloaded == 0
        assert stats.skipped == 1

    def test_missing_url_records_error(self, tmp_path: Path, fake_client) -> None:
        msg = {"files": [{"id": "F2", "name": "no-url.bin"}]}
        d = FileDownloader(fake_client, tmp_path, on_progress=lambda _: None)
        stats = d.download_for_messages([msg])
        assert stats.errors == 1
        assert stats.by_id["F2"].error == "no url_private"

    def test_dedup_across_messages(self, tmp_path: Path, fake_client) -> None:
        url = "https://files.slack.com/files-pri/T1/F1/hello.png"
        fake_client.register_file(url, b"PNG-bytes")
        msg = {
            "files": [{
                "id": "F1",
                "name": "hello.png",
                "url_private_download": url,
            }]
        }
        d = FileDownloader(fake_client, tmp_path, on_progress=lambda _: None)
        stats = d.download_for_messages([msg, msg, msg])
        assert stats.attempted == 1  # deduped to one network call
