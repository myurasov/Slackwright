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

"""Archive writer — emits a sharded message tree plus YAML user/channel
caches and a per-run JSONL ledger.

Layout (under ``--out``)::

    messages/YYYY/MM/DD/YYYY-MM-DD-<chan-slug>-<hash8>.json
    _users/<U_id>.yaml
    _channels/<C_id>.yaml
    _files/<F_id>/<safe_filename>            # only when --with-files
    _files/<F_id>/_meta.json
    _index.yaml
    matches.jsonl                            # one row per match (slim ledger)

Each per-message JSON contains the raw Slack search match, a ``channel_id``
injection (so the (channel, ts) key survives without filename context),
and an ``_archive`` sidecar carrying capture metadata. The dedup key is
``sha256("<channel_id>:<ts>")`` — re-runs merge cleanly into an
existing tree, and the layout deliberately mirrors the conventions used
by other Slack-archive tools so downstream readers can consume it
unchanged.

If you want a simpler dump, pass ``--format jsonl`` to skip the sharded
per-message tree and produce only ``matches.jsonl`` (one match per line).
``--format raw`` writes the unprocessed Slack response objects under
``_raw/`` for forensic inspection.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .resolver import EntityResolver

ARCHIVE_SCHEMA = 2

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 28) -> str:
    if not text:
        return ""
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:max_len].strip("-")


def message_key_hash(channel_id: str, ts: str) -> str:
    """Stable 64-hex sha256 of ``<channel_id>:<ts>`` — the canonical
    Slack-archive dedup key."""
    return hashlib.sha256(f"{channel_id}:{ts}".encode()).hexdigest()


def _ts_to_local_date(ts: str) -> str:
    try:
        sec = float(ts)
    except (TypeError, ValueError):
        return dt.datetime.now().astimezone().strftime("%Y-%m-%d")
    return dt.datetime.fromtimestamp(sec, tz=dt.timezone.utc).astimezone().strftime("%Y-%m-%d")


def _channel_slug(channel_id: str, channel_name: str | None, channel_type: str) -> str:
    if channel_name and not channel_name.startswith(("U", "C", "D", "G")):
        s = slugify(channel_name)
        if s:
            return s
    if not channel_id:
        return "unknown"
    if channel_id.startswith("D"):
        prefix = "im"
    elif channel_id.startswith("G"):
        prefix = "mpim"
    else:
        prefix = "ch"
    return f"{prefix}-{channel_id[-8:].lower()}"


def message_filepath(out_root: Path, channel_id: str, ts: str, channel_name: str | None,
                     channel_type: str) -> Path:
    h = message_key_hash(channel_id, ts)[:8]
    date_iso = _ts_to_local_date(ts)
    yyyy, mm, dd = date_iso.split("-")
    chan_slug = _channel_slug(channel_id, channel_name, channel_type)
    fname = f"{date_iso}-{chan_slug}-{h}.json"
    return out_root / "messages" / yyyy / mm / dd / fname


# ---------------------------------------------------------------------------
# Stats / index
# ---------------------------------------------------------------------------


@dataclass
class WriteStats:
    created: int = 0
    updated: int = 0
    noop: int = 0
    by_month: dict[str, int] = field(default_factory=dict)
    by_channel_type: dict[str, int] = field(default_factory=dict)
    user_ids_seen: set[str] = field(default_factory=set)
    channel_ids_seen: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class ArchiveWriter:
    """Stream Slack search matches to disk in the canonical archive layout.

    The writer is **idempotent**: re-saving an existing message merges
    capture metadata but never overwrites the Slack body unless it
    actually changed (mirrors ``slack-archive-write.py``'s
    ``diff_significant`` semantics).
    """

    def __init__(
        self,
        out_root: Path,
        *,
        resolver: EntityResolver | None = None,
        sa_user_id: str | None = None,
        format: str = "archive",
        plan_summary: str = "",
    ) -> None:
        self._out = out_root
        self._resolver = resolver
        self._sa_user_id = sa_user_id
        self._format = format
        self._plan_summary = plan_summary
        self.stats = WriteStats()
        self._matches_jsonl_path = out_root / "matches.jsonl"
        self._raw_dir = out_root / "_raw"
        self._captured_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        out_root.mkdir(parents=True, exist_ok=True)
        # Truncate the per-run jsonl ledger at start (one ledger per run).
        if self._format != "raw" and self._matches_jsonl_path.exists():
            self._matches_jsonl_path.unlink()

    def write_match(self, msg: dict[str, Any]) -> str:
        """Write one Slack search match to disk. Returns
        ``'created'`` / ``'updated'`` / ``'noop'``."""
        ts = msg.get("ts")
        if not ts:
            return "noop"
        ch = msg.get("channel") or {}
        cid = (ch.get("id") if isinstance(ch, dict) else ch) or msg.get("channel_id") or ""
        if not cid:
            return "noop"

        if self._format == "raw":
            self._raw_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{message_key_hash(cid, ts)[:16]}.json"
            (self._raw_dir / fname).write_text(json.dumps(msg, indent=2, sort_keys=True))
            self.stats.created += 1
            return "created"

        if self._format == "jsonl":
            self._append_jsonl(msg, cid)
            self.stats.created += 1
            self._track_ids(msg, cid)
            return "created"

        return self._write_archive_one(msg, cid, ts)

    def _write_archive_one(self, msg: dict[str, Any], cid: str, ts: str) -> str:
        channel_meta = self._resolver.get_channel(cid) if self._resolver else None
        ch_obj = msg.get("channel") if isinstance(msg.get("channel"), dict) else {}
        channel_name = (ch_obj.get("name") if ch_obj else None) or (channel_meta.name if channel_meta else None)
        channel_type = (channel_meta.type if channel_meta else None) or _infer_channel_type(cid, ch_obj)

        target = message_filepath(self._out, cid, ts, channel_name, channel_type)
        target.parent.mkdir(parents=True, exist_ok=True)

        sender = msg.get("user") or ""
        direction = "out" if (self._sa_user_id and sender == self._sa_user_id) else "in"
        thread_ts = msg.get("thread_ts") or _thread_ts_from(msg) or ts

        payload = dict(msg)
        payload["channel_id"] = cid
        payload["_archive"] = {
            "captured_at": self._captured_at,
            "direction": direction,
            "archive_schema": ARCHIVE_SCHEMA,
            "source_tool": "slackwright",
            "thread_ts": thread_ts,
            "search_plan": self._plan_summary,
        }

        outcome = "created"
        if target.exists():
            try:
                old = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                old = None
            if isinstance(old, dict) and not _diff_significant(old, payload):
                outcome = "noop"
            else:
                outcome = "updated"
                target.write_text(_render_file(payload), encoding="utf-8")
        else:
            target.write_text(_render_file(payload), encoding="utf-8")

        self._append_jsonl(msg, cid)

        if outcome == "created":
            self.stats.created += 1
        elif outcome == "updated":
            self.stats.updated += 1
        else:
            self.stats.noop += 1

        # Bookkeeping for the index.
        self._track_ids(msg, cid)
        self._bump_counts(channel_type, ts)
        return outcome

    # --- side outputs ---

    def _append_jsonl(self, msg: dict[str, Any], cid: str) -> None:
        ch = msg.get("channel") or {}
        row = {
            "channel_id": cid,
            "channel_name": ch.get("name") if isinstance(ch, dict) else None,
            "channel_type": ch.get("type") if isinstance(ch, dict) else None,
            "user": msg.get("user"),
            "username": msg.get("username"),
            "ts": msg.get("ts"),
            "thread_ts": msg.get("thread_ts"),
            "permalink": msg.get("permalink"),
            "text_preview": (msg.get("text") or "")[:120],
            "files_count": len(msg.get("files") or []),
        }
        with self._matches_jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _track_ids(self, msg: dict[str, Any], cid: str) -> None:
        if cid:
            self.stats.channel_ids_seen.add(cid)
        u = msg.get("user")
        if u:
            self.stats.user_ids_seen.add(u)
        for att in (msg.get("attachments") or []):
            au = att.get("author_id")
            if au:
                self.stats.user_ids_seen.add(au)

    def _bump_counts(self, channel_type: str | None, ts: str) -> None:
        try:
            sec = float(ts)
            month = dt.datetime.fromtimestamp(sec, tz=dt.timezone.utc).strftime("%Y-%m")
        except (TypeError, ValueError):
            month = "unknown"
        self.stats.by_month[month] = self.stats.by_month.get(month, 0) + 1
        ctype = channel_type or "channel"
        self.stats.by_channel_type[ctype] = self.stats.by_channel_type.get(ctype, 0) + 1

    # --- finalise ---

    def write_users_cache(self, resolver: EntityResolver) -> int:
        target = self._out / "_users"
        target.mkdir(parents=True, exist_ok=True)
        n = 0
        for uid in self.stats.user_ids_seen:
            rec = resolver.get_user(uid)
            if rec is None:
                continue
            (target / f"{uid}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "id": rec.id,
                        "name": rec.name,
                        "real_name": rec.real_name,
                        "display_name": rec.display_name,
                        "email": rec.email,
                        "title": rec.title,
                        "team_id": rec.team_id,
                        "deleted": rec.deleted,
                        "is_bot": rec.is_bot,
                        "captured_at": self._captured_at,
                    },
                    sort_keys=False,
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            n += 1
        return n

    def write_channels_cache(self, resolver: EntityResolver) -> int:
        target = self._out / "_channels"
        target.mkdir(parents=True, exist_ok=True)
        n = 0
        for cid in self.stats.channel_ids_seen:
            rec = resolver.get_channel(cid)
            if rec is None:
                continue
            (target / f"{cid}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "id": rec.id,
                        "name": rec.name,
                        "type": rec.type,
                        "is_private": rec.is_private,
                        "is_archived": rec.is_archived,
                        "topic": rec.topic,
                        "purpose": rec.purpose,
                        "user": rec.user,
                        "members": [],
                        "captured_at": self._captured_at,
                    },
                    sort_keys=False,
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            n += 1
        return n

    def write_index(self, *, plan_summary: str, search_query: str | None = None,
                    extra: dict[str, Any] | None = None) -> Path:
        idx: dict[str, Any] = {
            "schema_version": 1,
            "tool": "slackwright",
            "last_updated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "captured_at": self._captured_at,
            "format": self._format,
            "plan": plan_summary,
            "query": search_query,
            "counts": {
                "created": self.stats.created,
                "updated": self.stats.updated,
                "noop": self.stats.noop,
                "by_month": dict(sorted(self.stats.by_month.items())),
                "by_channel_type": dict(sorted(self.stats.by_channel_type.items())),
                "users_seen": len(self.stats.user_ids_seen),
                "channels_seen": len(self.stats.channel_ids_seen),
            },
        }
        if extra:
            idx["extra"] = extra
        target = self._out / "_index.yaml"
        target.write_text(
            yaml.safe_dump(idx, sort_keys=False, allow_unicode=True, width=10_000),
            encoding="utf-8",
        )
        return target


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_channel_type(cid: str, ch: dict[str, Any] | None) -> str:
    if ch and isinstance(ch, dict):
        if ch.get("is_im"):
            return "im"
        if ch.get("is_mpim"):
            return "mpim"
        if ch.get("is_group"):
            return "group"
    if cid.startswith("D"):
        return "im"
    if cid.startswith("G"):
        return "mpim"
    return "channel"


def _thread_ts_from(msg: dict[str, Any]) -> str | None:
    ti = msg.get("thread_info")
    if isinstance(ti, dict):
        return ti.get("thread_ts")
    return None


def _render_file(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _diff_significant(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Strip ``_archive`` (always changes on capture) and compare the rest."""
    def strip(d: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in d.items() if k != "_archive"}
    return strip(old) != strip(new)
