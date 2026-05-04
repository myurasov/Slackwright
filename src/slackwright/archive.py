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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .resolver import EntityResolver, _channel_from_payload

ARCHIVE_SCHEMA = 2

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)")


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


def message_filepath(
    out_root: Path, channel_id: str, ts: str, channel_name: str | None, channel_type: str
) -> Path:
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
    dropped: int = 0
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
        self._drop_warned = False
        out_root.mkdir(parents=True, exist_ok=True)
        # ``matches.jsonl`` is append-and-dedup across runs: prior rows
        # stay on disk so two consecutive fetches into the same --out
        # produce a real union, while ``(channel_id, ts)`` deduping
        # avoids double-counting when both runs surface the same
        # message.
        self._jsonl_seen_keys: set[tuple[str, str]] = self._load_jsonl_keys()

    def write_match(self, msg: dict[str, Any]) -> str:
        """Write one Slack search match to disk. Returns
        ``'created'`` / ``'updated'`` / ``'noop'`` / ``'dropped'``."""
        ts = msg.get("ts")
        if not ts:
            return self._drop("missing ts", msg)
        ch = msg.get("channel") or {}
        cid = (ch.get("id") if isinstance(ch, dict) else ch) or msg.get("channel_id") or ""
        if not cid:
            return self._drop("missing channel id", msg)

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

    def _drop(self, reason: str, msg: dict[str, Any]) -> str:
        # Silent drops used to mask schema drift (e.g. Slack returning
        # grouped {channel, messages:[…]} envelopes with no top-level ts).
        # Track + warn once so it can never zero out a run unnoticed.
        self.stats.dropped += 1
        if not self._drop_warned:
            self._drop_warned = True
            sample_keys = sorted(msg.keys()) if isinstance(msg, dict) else []
            sys.stderr.write(
                f"[slackwright] WARN: dropped match — {reason}. "
                f"Top-level keys: {sample_keys}. "
                f"This usually means Slack changed the search response shape; "
                f"open an issue with this stderr if matches keep dropping.\n"
            )
        return "dropped"

    def _write_archive_one(self, msg: dict[str, Any], cid: str, ts: str) -> str:
        channel_meta = self._resolver.get_channel(cid) if self._resolver else None
        ch_obj = msg.get("channel") if isinstance(msg.get("channel"), dict) else {}
        channel_name = (ch_obj.get("name") if ch_obj else None) or (
            channel_meta.name if channel_meta else None
        )
        channel_type = (channel_meta.type if channel_meta else None) or _infer_channel_type(
            cid, ch_obj
        )

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
        ts = msg.get("ts") or ""
        if (cid, ts) in self._jsonl_seen_keys:
            return
        self._jsonl_seen_keys.add((cid, ts))
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

    def _load_jsonl_keys(self) -> set[tuple[str, str]]:
        """Load existing ``(channel_id, ts)`` pairs out of ``matches.jsonl``.

        Used by the append-and-dedup path so a second run into the
        same ``--out`` doesn't double-write rows for messages the
        first run already recorded. Failures are best-effort: a
        corrupt or empty ledger just yields an empty set, and the
        run continues writing fresh rows.
        """
        if not self._matches_jsonl_path.exists():
            return set()
        seen: set[tuple[str, str]] = set()
        try:
            with self._matches_jsonl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    cid = str(row.get("channel_id") or "")
                    ts = str(row.get("ts") or "")
                    if cid and ts:
                        seen.add((cid, ts))
        except Exception:
            return seen
        return seen

    def _track_ids(self, msg: dict[str, Any], cid: str) -> None:
        if cid:
            self.stats.channel_ids_seen.add(cid)
            # Slack's search response embeds a full channel envelope —
            # name, type flags, members. Seed the resolver from it so we
            # don't waste a `conversations.info` round-trip per channel
            # later (and so we still have a name for MPIMs / private
            # channels that conversations.info can't see on Enterprise
            # Grid).
            ch_obj = msg.get("channel")
            if self._resolver is not None and isinstance(ch_obj, dict) and ch_obj.get("name"):
                rec = _channel_from_payload(ch_obj)
                if rec is not None and self._resolver.get_channel(cid) is None:
                    self._resolver.remember_channel(rec)
            # IMs: Slack uses the partner's user id as the channel name
            # (and sometimes echoes it back in ``channel.user``). Without
            # this, a DM where only the SA user spoke leaves the partner
            # unresolved — the report ends up showing "DM with U…".
            if isinstance(ch_obj, dict) and ch_obj.get("is_im"):
                partner = ch_obj.get("user") or ch_obj.get("name")
                if isinstance(partner, str) and partner.startswith(("U", "W")):
                    self.stats.user_ids_seen.add(partner)
        u = msg.get("user")
        if u:
            self.stats.user_ids_seen.add(u)
        for att in msg.get("attachments") or []:
            au = att.get("author_id")
            if au:
                self.stats.user_ids_seen.add(au)
        # ``<@Uxxx>`` mentions in message text need to be resolvable too,
        # otherwise the report renders the raw id inside message bodies.
        text = msg.get("text") or ""
        if "<@" in text:
            for match in _USER_MENTION_RE.finditer(text):
                self.stats.user_ids_seen.add(match.group(1))

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

    def write_index(
        self,
        *,
        plan_summary: str,
        search_query: str | None = None,
        extra: dict[str, Any] | None = None,
        cost: dict[str, Any] | None = None,
    ) -> Path:
        """Persist ``_index.yaml`` accumulating per-run records.

        The index is **append-and-aggregate**: every run appends a
        ``run`` entry under ``runs:`` capturing its plan / query /
        counts / cost / search_stats, then top-level fields are
        recomputed:

          - ``counts.*``: summed across all runs (so they reflect the
            total work done in this --out, not just the latest run)
          - ``plan`` / ``query`` / ``cost``: latest run's values, kept
            at top level for backward compatibility with readers that
            don't know about the multi-run schema
          - ``captured_at``: timestamp of the **first** run; the
            ``last_updated`` field tracks the most recent

        A legacy single-run index (no ``runs:``) is migrated in place
        on first write by promoting its top-level fields into the
        first ``run`` entry.
        """
        finished_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        run_record: dict[str, Any] = {
            "plan": plan_summary,
            "query": search_query,
            "captured_at": self._captured_at,
            "finished_at": finished_at,
            "format": self._format,
            "counts": {
                "created": self.stats.created,
                "updated": self.stats.updated,
                "noop": self.stats.noop,
                "dropped": self.stats.dropped,
                "by_month": dict(sorted(self.stats.by_month.items())),
                "by_channel_type": dict(sorted(self.stats.by_channel_type.items())),
                "users_seen": len(self.stats.user_ids_seen),
                "channels_seen": len(self.stats.channel_ids_seen),
            },
        }
        if cost is not None:
            run_record["cost"] = cost
        if extra:
            run_record["extra"] = extra

        existing = read_index(self._out) or {}
        prior_runs = (
            list(existing["runs"])
            if isinstance(existing.get("runs"), list)
            else _migrate_legacy_index_to_run(existing)
        )
        all_runs = prior_runs + [run_record]
        first_captured_at = (
            existing.get("captured_at")
            or (prior_runs[0].get("captured_at") if prior_runs else self._captured_at)
        )

        idx: dict[str, Any] = {
            "schema_version": 2,
            "tool": "slackwright",
            "captured_at": first_captured_at,
            "last_updated": finished_at,
            "format": self._format,
            "plan": plan_summary,
            "query": search_query,
            "counts": _aggregate_counts(all_runs),
            "runs": all_runs,
        }
        if cost is not None:
            idx["cost"] = cost
        if extra:
            idx["extra"] = extra
        target = self._out / "_index.yaml"
        target.write_text(
            yaml.safe_dump(idx, sort_keys=False, allow_unicode=True, width=10_000),
            encoding="utf-8",
        )
        return target


def _migrate_legacy_index_to_run(existing: dict[str, Any]) -> list[dict[str, Any]]:
    """Promote a legacy (schema v1) single-run index into a one-element runs list.

    Pre-multi-run archives have ``plan`` / ``query`` / ``counts`` /
    ``cost`` / ``extra`` at the top level. We don't want to lose that
    history when someone runs a second fetch into the same --out, so
    we synthesize the equivalent ``run`` record and prepend it before
    appending the new run.
    """
    if not existing.get("plan") and not existing.get("query") and not existing.get("counts"):
        return []
    legacy: dict[str, Any] = {
        "plan": existing.get("plan"),
        "query": existing.get("query"),
        "captured_at": existing.get("captured_at"),
        "finished_at": existing.get("last_updated"),
        "format": existing.get("format"),
        "counts": existing.get("counts") or {},
    }
    if existing.get("cost") is not None:
        legacy["cost"] = existing["cost"]
    if existing.get("extra") is not None:
        legacy["extra"] = existing["extra"]
    return [legacy]


def _aggregate_counts(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum per-run counts into a single top-level summary.

    ``users_seen`` / ``channels_seen`` are summed too — they're not
    deduped across runs because the underlying ``WriteStats`` only
    knew about the run-local set. The actual on-disk truth lives in
    ``_users/`` / ``_channels/`` (which **is** unioned across runs).
    """
    out: dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "noop": 0,
        "dropped": 0,
        "by_month": {},
        "by_channel_type": {},
        "users_seen": 0,
        "channels_seen": 0,
    }
    for run in runs:
        c = run.get("counts") or {}
        for k in ("created", "updated", "noop", "dropped", "users_seen", "channels_seen"):
            v = c.get(k)
            if isinstance(v, int):
                out[k] += v
        for month, n in (c.get("by_month") or {}).items():
            if isinstance(n, int):
                out["by_month"][month] = out["by_month"].get(month, 0) + n
        for ctype, n in (c.get("by_channel_type") or {}).items():
            if isinstance(n, int):
                out["by_channel_type"][ctype] = out["by_channel_type"].get(ctype, 0) + n
    out["by_month"] = dict(sorted(out["by_month"].items()))
    out["by_channel_type"] = dict(sorted(out["by_channel_type"].items()))
    return out


def read_index(out_dir: Path) -> dict[str, Any] | None:
    """Load an existing ``_index.yaml`` from a slackwright output dir.

    Returns ``None`` if no index exists. Used by ``--resume`` (to discover
    which chunks were already completed) and by the ``describe-archive``
    subcommand.
    """
    p = Path(out_dir) / "_index.yaml"
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or None
    except Exception:
        return None


def previously_completed_chunks(out_dir: Path, *, query: str | None = None) -> set[str]:
    """Return chunk labels that a prior run finished successfully.

    With multi-run archives we match by ``query`` so ``--resume`` only
    skips chunks completed by a prior run of the *same* query — mixing
    ``--from me`` and ``--to me`` runs in one --out shouldn't trick a
    resume into thinking the other query's chunks are done.

    Falls back to the latest run's ``chunks_completed`` when ``query``
    is unspecified, and to the legacy v1 ``extra.search_stats.chunks_completed``
    field for archives written before the multi-run schema.
    """
    idx = read_index(out_dir)
    if not idx:
        return set()
    runs = idx.get("runs") if isinstance(idx, dict) else None
    if isinstance(runs, list) and runs:
        candidates = (
            [r for r in runs if isinstance(r, dict) and r.get("query") == query]
            if query is not None
            else [runs[-1]] if isinstance(runs[-1], dict) else []
        )
        out: set[str] = set()
        for r in candidates:
            ss = ((r.get("extra") or {}).get("search_stats") or {}) if isinstance(r, dict) else {}
            for c in ss.get("chunks_completed") or []:
                out.add(str(c))
        return out
    # Legacy v1 schema: top-level ``extra.search_stats``.
    extra = (idx.get("extra") or {}) if isinstance(idx, dict) else {}
    ss = (extra.get("search_stats") or {}) if isinstance(extra, dict) else {}
    completed = ss.get("chunks_completed") or []
    if not isinstance(completed, list):
        return set()
    return {str(c) for c in completed}


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
