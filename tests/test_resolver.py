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

"""Tests for :mod:`slackwright.resolver` — entity resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from slackwright.resolver import (
    EntityResolver,
    is_channel_id,
    is_email,
    is_self_token,
    is_user_id,
)


class TestInputClassification:
    @pytest.mark.parametrize("v", ["U06HYSK2P2L", "U12345AB", "W0LONGPATH123", "B12345AB"])
    def test_user_ids(self, v: str) -> None:
        assert is_user_id(v)

    @pytest.mark.parametrize("v", ["alice", "alice@example.com", "U_lower", "C12345AB", ""])
    def test_non_user_ids(self, v: str) -> None:
        assert not is_user_id(v)

    @pytest.mark.parametrize("v", ["C07SC7AFW7Q", "D0634N8J8P6", "G123ABCDE"])
    def test_channel_ids(self, v: str) -> None:
        assert is_channel_id(v)

    @pytest.mark.parametrize("v", ["alice@example.com", "first.last+tag@subdomain.example.co"])
    def test_emails(self, v: str) -> None:
        assert is_email(v)

    @pytest.mark.parametrize("v", ["alice", "alice@", "@example.com"])
    def test_non_emails(self, v: str) -> None:
        assert not is_email(v)

    @pytest.mark.parametrize("v", ["me", "Myself", "  self  ", "I"])
    def test_self_tokens(self, v: str) -> None:
        assert is_self_token(v)


class TestUserResolution:
    def test_by_id_uses_cache(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        # Cache populated by passing through users.list once via name lookup
        ru = r.resolve_user("alice")
        assert ru.id == "UALICE00"
        # Second resolve: by id, no extra network call
        before = len(fake_client.calls)
        ru2 = r.resolve_user("UALICE00")
        assert ru2.id == "UALICE00"
        assert len(fake_client.calls) == before  # cache hit, no network

    def test_by_handle(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        ru = r.resolve_user("bob.builder")
        assert ru.id == "UBOB0001"

    def test_by_real_name(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        ru = r.resolve_user("Alice Engineer")
        assert ru.id == "UALICE00"

    def test_by_display_name(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        ru = r.resolve_user("bob")
        assert ru.id == "UBOB0001"

    def test_by_real_name_substring(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        ru = r.resolve_user("Vega")
        assert ru.id == "UCARLA01"

    def test_by_email_uses_lookupByEmail(self, state_dir: Path, fake_client) -> None:
        # Use a user not in the users.list cache to force a real lookup
        fake_client.register(
            "users.lookupByEmail",
            {
                "ok": True,
                "user": {
                    "id": "UDAVE001",
                    "name": "dave",
                    "real_name": "Dave Externalee",
                    "profile": {"email": "dave@thirdparty.com"},
                },
            },
        )
        r = EntityResolver(fake_client, state_dir=state_dir)
        ru = r.resolve_user("dave@thirdparty.com")
        assert ru.id == "UDAVE001"
        # Subsequent resolve uses email cache, no second lookup call
        before = len([c for c in fake_client.calls if c[0] == "users.lookupByEmail"])
        r.resolve_user("dave@thirdparty.com")
        after = len([c for c in fake_client.calls if c[0] == "users.lookupByEmail"])
        assert after == before

    def test_self(self, state_dir: Path, fake_client) -> None:
        fake_client.register(
            "users.info",
            {
                "ok": True,
                "user": {
                    "id": "UALICE00",
                    "name": "alice",
                    "real_name": "Alice Engineer",
                    "profile": {"email": "alice@example.com"},
                },
            },
        )
        r = EntityResolver(fake_client, state_dir=state_dir)
        ru = r.resolve_user("me")
        assert ru.id == "UALICE00"
        ru2 = r.resolve_user("self")
        assert ru2.id == "UALICE00"

    def test_unknown_raises(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        with pytest.raises(LookupError):
            r.resolve_user("nope nope nope")

    def test_ambiguous_raises(self, state_dir: Path, fake_client) -> None:
        # Add another user whose real_name contains "Engineer" (already
        # matched by Alice). Substring match on "Engineer" should be ambiguous.
        fake_client.register(
            "users.list",
            {
                "ok": True,
                "members": [
                    {"id": "UALICE00", "name": "alice", "real_name": "Alice Engineer"},
                    {"id": "UZED0001", "name": "zed", "real_name": "Zed Engineer"},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        )
        r = EntityResolver(fake_client, state_dir=state_dir)
        with pytest.raises(LookupError) as ei:
            r.resolve_user("Engineer")
        assert "ambiguous" in str(ei.value).lower()

    def test_handle_with_at_prefix(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        ru = r.resolve_user("@bob.builder")
        assert ru.id == "UBOB0001"

    def test_empty_raises(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        with pytest.raises(ValueError):
            r.resolve_user("")


class TestChannelResolution:
    def test_by_id(self, state_dir: Path, fake_client) -> None:
        fake_client.register(
            "conversations.info",
            {
                "ok": True,
                "channel": {
                    "id": "CNEWCHAN",
                    "name": "newchan",
                    "is_channel": True,
                    "is_private": False,
                },
            },
        )
        r = EntityResolver(fake_client, state_dir=state_dir)
        rc = r.resolve_channel("CNEWCHAN")
        assert rc.id == "CNEWCHAN"
        assert rc.record.name == "newchan"

    def test_by_name(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        rc = r.resolve_channel("engineering")
        assert rc.id == "CENGTEAM"

    def test_with_hash_prefix(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        rc = r.resolve_channel("#general")
        assert rc.id == "CGENERAL"

    def test_unknown_raises(self, state_dir: Path, fake_client) -> None:
        r = EntityResolver(fake_client, state_dir=state_dir)
        with pytest.raises(LookupError):
            r.resolve_channel("nope-channel")


class TestCachePersistence:
    def test_caches_survive_reload(self, state_dir: Path, fake_client) -> None:
        r1 = EntityResolver(fake_client, state_dir=state_dir)
        r1.resolve_user("alice")
        r1.resolve_channel("general")
        r1.save_caches()

        # New resolver: should hit caches without any network call.
        prev_calls = list(fake_client.calls)
        r2 = EntityResolver(fake_client, state_dir=state_dir)
        ru = r2.resolve_user("alice")
        rc = r2.resolve_channel("general")
        assert ru.id == "UALICE00"
        assert rc.id == "CGENERAL"
        assert fake_client.calls == prev_calls  # no new network calls
