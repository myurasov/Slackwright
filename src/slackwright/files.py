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

"""Attachment downloader.

Each Slack message can carry a ``files`` array of file metadata. The
downloader walks the messages, GETs each ``url_private_download`` (or
``url_private`` as a fallback) through the authed Playwright session, and
writes the bytes under the archive's ``_files/`` directory. The on-disk
layout is content-addressable by file id::

    _files/<file_id>/<safe_filename>
    _files/<file_id>/_meta.json    # the file dict from Slack

so re-downloads are idempotent and dedup naturally.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import SlackWebClient, SlackWebError

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str, fallback: str) -> str:
    if not name:
        return fallback
    cleaned = _SAFE_NAME_RE.sub("_", name).strip("._")
    return cleaned or fallback


def iter_files_in_messages(messages: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Yield every Slack ``file`` dict referenced from any of ``messages``.

    Skips entries that look incomplete (``mode == "tombstone"``, missing id,
    deleted) so the downloader doesn't burn requests on dead links.
    """
    for m in messages:
        for f in m.get("files") or []:
            if not isinstance(f, dict):
                continue
            if f.get("mode") == "tombstone":
                continue
            if not f.get("id"):
                continue
            yield f
        # Slack also exposes file unfurls under `attachments[*].files`
        for att in m.get("attachments") or []:
            for f in att.get("files") or []:
                if isinstance(f, dict) and f.get("id"):
                    yield f


@dataclass
class FileDownloadResult:
    file_id: str
    name: str
    path: Path | None
    bytes_written: int = 0
    skipped: bool = False
    error: str | None = None


@dataclass
class FileDownloaderStats:
    attempted: int = 0
    downloaded: int = 0
    skipped: int = 0
    errors: int = 0
    bytes_total: int = 0
    by_id: dict[str, FileDownloadResult] = field(default_factory=dict)


class FileDownloader:
    """Download Slack file attachments using an authed :class:`SlackWebClient`."""

    def __init__(
        self,
        client: SlackWebClient,
        out_root: Path,
        *,
        on_progress=None,
    ) -> None:
        self._client = client
        self._files_root = out_root / "_files"
        self._on_progress = on_progress or (lambda s: sys.stderr.write(f"[slackwright] {s}\n"))
        self.stats = FileDownloaderStats()

    def download_for_messages(self, messages: Iterable[dict[str, Any]]) -> FileDownloaderStats:
        seen: set[str] = set()
        for f in iter_files_in_messages(messages):
            fid = f.get("id")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            self.stats.attempted += 1
            self._download_one(f)
        return self.stats

    def _download_one(self, f: dict[str, Any]) -> FileDownloadResult:
        fid = f["id"]
        name = _safe_filename(f.get("name") or "", fallback=fid)
        target_dir = self._files_root / fid
        target_dir.mkdir(parents=True, exist_ok=True)
        meta_path = target_dir / "_meta.json"
        target = target_dir / name

        # Always refresh the meta file (cheap, small).
        meta_path.write_text(json.dumps(f, indent=2, sort_keys=True))

        if target.exists() and target.stat().st_size > 0:
            self.stats.skipped += 1
            res = FileDownloadResult(file_id=fid, name=name, path=target, skipped=True)
            self.stats.by_id[fid] = res
            return res

        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            self.stats.errors += 1
            res = FileDownloadResult(file_id=fid, name=name, path=None, error="no url_private")
            self.stats.by_id[fid] = res
            return res

        try:
            data = self._client.download_file(url)
        except SlackWebError as e:
            self.stats.errors += 1
            res = FileDownloadResult(file_id=fid, name=name, path=None, error=str(e))
            self.stats.by_id[fid] = res
            self._on_progress(f"file {fid} ({name}) — error: {e}")
            return res

        target.write_bytes(data)
        self.stats.downloaded += 1
        self.stats.bytes_total += len(data)
        res = FileDownloadResult(file_id=fid, name=name, path=target, bytes_written=len(data))
        self.stats.by_id[fid] = res
        self._on_progress(
            f"file {fid} -> {target.relative_to(self._files_root.parent)} ({len(data):,}B)"
        )
        return res
