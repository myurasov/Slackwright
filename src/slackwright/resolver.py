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

"""Entity resolver — turn user-friendly inputs into Slack IDs / handles.

The user can pass any of these for ``--from``, ``--to``, ``--with``:

  - ``U06HYSK2P2L``      — raw Slack user id
  - ``alice@example.com``— email address (resolved via ``users.lookupByEmail``)
  - ``alice``            — Slack handle (matched against the ``name`` field)
  - ``Alice Smith``      — display / real name (cached + searched via ``users.list``)
  - ``me`` / ``self``    — the logged-in user

For ``--in`` channels:

  - ``C07SC7AFW7Q``      — raw channel id
  - ``D0634N8J8P6``      — raw DM id (works too)
  - ``cosmos-pm``        — channel name (with or without leading ``#``)

Resolution is **lazy** and **cached**. Lookups go through ``users.list``
(paginated, expensive — done once per session and cached on disk) and
``conversations.list`` for channels. Both caches survive across runs in
``<state-dir>/users.json`` and ``<state-dir>/channels.json`` so the second
``slackwright fetch`` invocation skips the 30s metadata sync.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import SlackWebClient, SlackWebError
from .paths import channels_cache_path, handle_index_path, users_cache_path

# ---------------------------------------------------------------------------
# Input classification
# ---------------------------------------------------------------------------


_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")
_BOT_ID_RE = re.compile(r"^B[A-Z0-9]{6,}$")
_CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{6,}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SELF_TOKENS = frozenset({"me", "myself", "self", "i"})


def is_user_id(value: str) -> bool:
    return bool(_USER_ID_RE.match(value)) or bool(_BOT_ID_RE.match(value))


def is_channel_id(value: str) -> bool:
    return bool(_CHANNEL_ID_RE.match(value))


def is_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def is_self_token(value: str) -> bool:
    return value.strip().lower() in _SELF_TOKENS


# ---------------------------------------------------------------------------
# Cached entity records (small, JSON-friendly)
# ---------------------------------------------------------------------------


@dataclass
class UserRecord:
    id: str
    name: str | None = None  # @handle (the ``name`` field)
    real_name: str | None = None
    display_name: str | None = None
    email: str | None = None
    title: str | None = None
    team_id: str | None = None
    is_bot: bool = False
    deleted: bool = False
    cached_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> UserRecord:
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class ChannelRecord:
    id: str
    name: str | None = None
    type: str = "channel"  # channel | im | mpim | group
    is_private: bool = False
    is_archived: bool = False
    topic: str | None = None
    purpose: str | None = None
    user: str | None = None  # IM: id of the other user
    cached_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ChannelRecord:
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Cache loading / saving
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


@dataclass
class ResolvedUser:
    """Normalised view of a resolved person — what ``search.py`` consumes."""

    record: UserRecord

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def handle(self) -> str | None:
        """``@``-handle suitable for the Slack search query (``from:@handle``)."""
        return self.record.name


@dataclass
class ResolvedChannel:
    record: ChannelRecord

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def slug(self) -> str | None:
        """``#``-style slug for the Slack search query (``in:#channel``)."""
        return self.record.name


class EntityResolver:
    """Resolve human-readable inputs to Slack ids / handles.

    Construct with a :class:`SlackWebClient` (live mode) or with
    ``client=None`` to operate purely from cache (useful for tests).
    """

    def __init__(self, client: SlackWebClient | None, *, state_dir: Path) -> None:
        self._client = client
        self._state_dir = state_dir
        self._users: dict[str, UserRecord] = {}
        self._channels: dict[str, ChannelRecord] = {}
        # name-index: lower-cased lookup keys -> user id
        self._handle_index: dict[str, str] = {}
        self._real_name_index: dict[str, str] = {}
        self._display_name_index: dict[str, str] = {}
        self._email_index: dict[str, str] = {}
        self._channel_name_index: dict[str, str] = {}
        self._users_listed = False
        self._channels_listed = False
        self._load_caches()

    # --- cache I/O ---

    def _load_caches(self) -> None:
        users_raw = _load_json(users_cache_path(self._state_dir))
        for uid, d in (users_raw.get("users") or {}).items():
            try:
                self._users[uid] = UserRecord.from_json(d)
            except Exception:
                continue
        ch_raw = _load_json(channels_cache_path(self._state_dir))
        for cid, d in (ch_raw.get("channels") or {}).items():
            try:
                self._channels[cid] = ChannelRecord.from_json(d)
            except Exception:
                continue
        idx_raw = _load_json(handle_index_path(self._state_dir))
        self._handle_index.update(idx_raw.get("handle") or {})
        self._real_name_index.update(idx_raw.get("real_name") or {})
        self._display_name_index.update(idx_raw.get("display_name") or {})
        self._email_index.update(idx_raw.get("email") or {})
        self._channel_name_index.update(idx_raw.get("channel_name") or {})
        self._users_listed = bool(idx_raw.get("users_listed"))
        self._channels_listed = bool(idx_raw.get("channels_listed"))

    def save_caches(self) -> None:
        _atomic_write(
            users_cache_path(self._state_dir),
            {"users": {uid: u.to_json() for uid, u in self._users.items()}},
        )
        _atomic_write(
            channels_cache_path(self._state_dir),
            {"channels": {cid: c.to_json() for cid, c in self._channels.items()}},
        )
        _atomic_write(
            handle_index_path(self._state_dir),
            {
                "handle": self._handle_index,
                "real_name": self._real_name_index,
                "display_name": self._display_name_index,
                "email": self._email_index,
                "channel_name": self._channel_name_index,
                "users_listed": self._users_listed,
                "channels_listed": self._channels_listed,
            },
        )

    # --- record ingestion / indexing ---

    def remember_user(self, rec: UserRecord) -> None:
        if not rec.id:
            return
        self._users[rec.id] = rec
        if rec.name:
            self._handle_index[rec.name.lower()] = rec.id
        if rec.real_name:
            self._real_name_index[rec.real_name.lower()] = rec.id
        if rec.display_name:
            self._display_name_index[rec.display_name.lower()] = rec.id
        if rec.email:
            self._email_index[rec.email.lower()] = rec.id

    def remember_channel(self, rec: ChannelRecord) -> None:
        if not rec.id:
            return
        self._channels[rec.id] = rec
        if rec.name:
            self._channel_name_index[rec.name.lower()] = rec.id

    def get_user(self, user_id: str) -> UserRecord | None:
        return self._users.get(user_id)

    def get_channel(self, channel_id: str) -> ChannelRecord | None:
        return self._channels.get(channel_id)

    @property
    def users(self) -> dict[str, UserRecord]:
        return dict(self._users)

    @property
    def channels(self) -> dict[str, ChannelRecord]:
        return dict(self._channels)

    # --- resolve people ---

    def resolve_user(self, value: str) -> ResolvedUser:
        """Best-effort resolution of an arbitrary ``--from`` / ``--to`` arg."""
        if not value or not value.strip():
            raise ValueError("empty user reference")
        v = value.strip()

        if is_self_token(v):
            return self._resolve_self()

        if is_user_id(v):
            rec = self._users.get(v)
            if rec is None:
                rec = self._fetch_user_by_id(v) or UserRecord(id=v)
                self.remember_user(rec)
            return ResolvedUser(rec)

        if is_email(v):
            uid = self._email_index.get(v.lower())
            if uid and uid in self._users:
                return ResolvedUser(self._users[uid])
            rec = self._fetch_user_by_email(v)
            if rec is None:
                raise LookupError(f"no Slack user found for email {v!r}")
            self.remember_user(rec)
            return ResolvedUser(rec)

        # Strip a leading @ if user typed `@alice`
        v_handle = v.lstrip("@")

        # Try cached handle / display / real name (in that order)
        for idx in (self._handle_index, self._display_name_index, self._real_name_index):
            uid = idx.get(v_handle.lower())
            if uid and uid in self._users:
                return ResolvedUser(self._users[uid])

        # Fall back to a full users.list sync (cached on success)
        self._ensure_users_listed()
        for idx in (self._handle_index, self._display_name_index, self._real_name_index):
            uid = idx.get(v_handle.lower())
            if uid and uid in self._users:
                return ResolvedUser(self._users[uid])

        # Last resort — case-insensitive substring match on real_name. We
        # only accept a unique hit (no ambiguity surfaces silently).
        candidates = [
            (uid, self._users[uid])
            for uid in self._users
            if (self._users[uid].real_name or "").lower().find(v_handle.lower()) >= 0
            or (self._users[uid].display_name or "").lower().find(v_handle.lower()) >= 0
        ]
        candidates = [c for c in candidates if not c[1].deleted]
        if len(candidates) == 1:
            return ResolvedUser(candidates[0][1])
        if len(candidates) > 1:
            sample = ", ".join(f"{u.real_name or u.name} ({u.id})" for _, u in candidates[:6])
            raise LookupError(
                f"ambiguous user reference {v!r} matched {len(candidates)} users: {sample}"
                f"{' …' if len(candidates) > 6 else ''}"
            )
        raise LookupError(f"no Slack user matches {v!r} (try the email or the U… id)")

    def _resolve_self(self) -> ResolvedUser:
        if self._client is None:
            raise LookupError("cannot resolve `me` without an authenticated client")
        bundle = self._client.bundle
        rec = self._users.get(bundle.user_id)
        if rec is None:
            rec = self._fetch_user_by_id(bundle.user_id) or UserRecord(
                id=bundle.user_id,
                name=bundle.user_name,
                real_name=bundle.user_real_name,
                email=bundle.user_email,
            )
            self.remember_user(rec)
        return ResolvedUser(rec)

    # --- resolve channels ---

    def resolve_channel(self, value: str) -> ResolvedChannel:
        if not value or not value.strip():
            raise ValueError("empty channel reference")
        v = value.strip().lstrip("#")

        if is_channel_id(v):
            rec = self._channels.get(v)
            if rec is None:
                rec = self._fetch_channel_by_id(v) or ChannelRecord(id=v)
                self.remember_channel(rec)
            return ResolvedChannel(rec)

        cid = self._channel_name_index.get(v.lower())
        if cid and cid in self._channels:
            return ResolvedChannel(self._channels[cid])

        self._ensure_channels_listed()
        cid = self._channel_name_index.get(v.lower())
        if cid and cid in self._channels:
            return ResolvedChannel(self._channels[cid])

        raise LookupError(f"no channel matches {v!r} (try the leading # or the C… id)")

    # --- network-backed lookups ---

    def _need_client(self) -> SlackWebClient:
        if self._client is None:
            raise LookupError("client is offline (cache-only mode); cannot fetch live")
        return self._client

    def _fetch_user_by_id(self, uid: str) -> UserRecord | None:
        # Cache-only mode (no client): return None so the caller can fall
        # back to a stub UserRecord(id=uid). This is what `--explain`
        # relies on to avoid spinning up Playwright when the input is
        # already a Slack id.
        if self._client is None:
            return None
        try:
            data = self._client.api("users.info", {"user": uid})
        except SlackWebError as e:
            if e.error in {"user_not_found", "users_not_found"}:
                return None
            raise
        return _user_from_payload(data.get("user") or {})

    def _fetch_user_by_email(self, email: str) -> UserRecord | None:
        try:
            data = self._need_client().api("users.lookupByEmail", {"email": email})
        except SlackWebError as e:
            if e.error in {"users_not_found", "user_not_found"}:
                return None
            raise
        return _user_from_payload(data.get("user") or {})

    def _fetch_channel_by_id(self, cid: str) -> ChannelRecord | None:
        # Same cache-only escape hatch as _fetch_user_by_id.
        if self._client is None:
            return None
        try:
            data = self._client.api("conversations.info", {"channel": cid})
        except SlackWebError as e:
            if e.error in {"channel_not_found"}:
                return None
            raise
        return _channel_from_payload(data.get("channel") or {})

    def _ensure_users_listed(self) -> None:
        if self._users_listed:
            return
        client = self._need_client()
        sys.stderr.write("[slackwright] fetching users.list (one-time, cached for next runs)…\n")
        cursor: str | None = None
        page = 0
        while True:
            page += 1
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = client.api("users.list", params)
            for u in data.get("members") or []:
                rec = _user_from_payload(u)
                if rec is not None:
                    self.remember_user(rec)
            cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
            if page > 200:  # 40K users — bail before something pathological
                sys.stderr.write("[slackwright] users.list cursor exceeded 200 pages, stopping\n")
                break
        self._users_listed = True
        self.save_caches()

    def _ensure_channels_listed(self) -> None:
        if self._channels_listed:
            return
        client = self._need_client()
        sys.stderr.write(
            "[slackwright] fetching conversations.list (one-time, cached for next runs)…\n"
        )
        cursor: str | None = None
        page = 0
        while True:
            page += 1
            params: dict[str, Any] = {
                "limit": 200,
                "types": "public_channel,private_channel,mpim,im",
                "exclude_archived": False,
            }
            if cursor:
                params["cursor"] = cursor
            data = client.api("conversations.list", params)
            for c in data.get("channels") or []:
                rec = _channel_from_payload(c)
                if rec is not None:
                    self.remember_channel(rec)
            cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
            if page > 200:
                sys.stderr.write(
                    "[slackwright] conversations.list cursor exceeded 200 pages, stopping\n"
                )
                break
        self._channels_listed = True
        self.save_caches()

    # --- bulk resolve helpers (called by archive writer) ---

    def resolve_users_in(self, ids: Iterable[str]) -> dict[str, UserRecord]:
        """Best-effort batch — fetch any uncached users by id."""
        out: dict[str, UserRecord] = {}
        for uid in ids:
            if not uid:
                continue
            rec = self._users.get(uid)
            if rec is None:
                try:
                    rec = self._fetch_user_by_id(uid)
                except SlackWebError:
                    rec = None
                if rec is None:
                    rec = UserRecord(id=uid)
                self.remember_user(rec)
            out[uid] = rec
        return out

    def resolve_channels_in(self, ids: Iterable[str]) -> dict[str, ChannelRecord]:
        out: dict[str, ChannelRecord] = {}
        for cid in ids:
            if not cid:
                continue
            rec = self._channels.get(cid)
            if rec is None:
                try:
                    rec = self._fetch_channel_by_id(cid)
                except SlackWebError:
                    rec = None
                if rec is None:
                    rec = ChannelRecord(id=cid)
                self.remember_channel(rec)
            out[cid] = rec
        return out


# ---------------------------------------------------------------------------
# Payload normalisers (pure helpers — covered by unit tests)
# ---------------------------------------------------------------------------


def _user_from_payload(u: dict[str, Any]) -> UserRecord | None:
    uid = u.get("id")
    if not uid:
        return None
    profile = u.get("profile") or {}
    return UserRecord(
        id=uid,
        name=u.get("name"),
        real_name=u.get("real_name") or profile.get("real_name"),
        display_name=profile.get("display_name") or None,
        email=profile.get("email") or u.get("email"),
        title=profile.get("title"),
        team_id=u.get("team_id") or profile.get("team"),
        is_bot=bool(u.get("is_bot")),
        deleted=bool(u.get("deleted")),
    )


def _channel_from_payload(c: dict[str, Any]) -> ChannelRecord | None:
    cid = c.get("id")
    if not cid:
        return None
    if c.get("is_im"):
        ctype = "im"
    elif c.get("is_mpim"):
        ctype = "mpim"
    elif c.get("is_group"):
        ctype = "group"
    else:
        ctype = "channel"
    name = c.get("name") or c.get("name_normalized")
    topic = (c.get("topic") or {}).get("value") if isinstance(c.get("topic"), dict) else None
    purpose = (c.get("purpose") or {}).get("value") if isinstance(c.get("purpose"), dict) else None
    return ChannelRecord(
        id=cid,
        name=name,
        type=ctype,
        is_private=bool(c.get("is_private")),
        is_archived=bool(c.get("is_archived")),
        topic=topic,
        purpose=purpose,
        user=c.get("user"),
    )
