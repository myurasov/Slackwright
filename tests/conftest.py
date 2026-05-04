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

"""Shared pytest fixtures for the slackwright test suite.

We don't need a live Slack workspace to test most of the surface — the
:class:`FakeClient` and the canned ``users.list`` / ``conversations.list``
payloads exercise the resolver, search builder, and archive writer end-
to-end without any network.

The Playwright auth / fetch path is smoke-tested separately under
``tests/test_smoke_playwright.py`` (skipped unless ``$SLACKWRIGHT_LIVE`` is
set with a logged-in state dir).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from slackwright.auth import AuthBundle
from slackwright.client import SlackWebError


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    p = tmp_path / "state"
    p.mkdir()
    return p


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    p = tmp_path / "out"
    p.mkdir()
    return p


@pytest.fixture
def auth_bundle() -> AuthBundle:
    return AuthBundle(
        workspace_url="https://acme.slack.com",
        api_url="https://acme.slack.com/api",
        api_token="xoxc-fake-test-token-XXXXXXXXXXXX",
        team_id="T12345",
        enterprise_id=None,
        user_id="UALICE00",
        user_name="alice",
        user_real_name="Alice Engineer",
        user_email="alice@example.com",
        extracted_at=0.0,
        storage_state_path="(none)",
    )


# ---------------------------------------------------------------------------
# Minimal fake-Slack client
# ---------------------------------------------------------------------------


class FakeClient:
    """Stand-in for :class:`slackwright.client.SlackWebClient`.

    Routes ``api(method, params)`` through a dict of canned responses; if a
    method is missing, raises :class:`SlackWebError` so the test fails
    loudly rather than silently returning an empty body.
    """

    def __init__(self, bundle: AuthBundle, responses: dict[str, Any] | None = None) -> None:
        self.bundle = bundle
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses: dict[str, Any] = responses or {}
        self._download_responses: dict[str, bytes] = {}
        # Channel -> (method, body) helper: tests can install custom callable
        # handlers that compute responses dynamically.
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    # --- registration ---

    def register(self, method: str, response: dict[str, Any]) -> None:
        self._responses[method] = response

    def register_handler(
        self, method: str, fn: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        self._handlers[method] = fn

    def register_file(self, url: str, payload: bytes) -> None:
        self._download_responses[url] = payload

    # --- SlackWebClient surface ---

    def api(self, method: str, params: dict[str, Any] | None = None, **_) -> dict[str, Any]:
        body = dict(params or {})
        self.calls.append((method, body))
        if method in self._handlers:
            return self._handlers[method](body)
        if method not in self._responses:
            raise SlackWebError(method, "no_canned_response_in_FakeClient")
        return self._responses[method]

    def download_file(self, url: str, **_) -> bytes:
        if url in self._download_responses:
            return self._download_responses[url]
        raise SlackWebError("download_file", f"no_canned_blob_for {url}")

    def health(self) -> dict[str, Any]:
        return self.api("auth.test", {})


@pytest.fixture
def fake_client(auth_bundle: AuthBundle) -> FakeClient:
    """Pre-seeded FakeClient with a sensible users.list / conversations.list cache."""
    c = FakeClient(auth_bundle)
    c.register("auth.test", {"ok": True, "user": "UALICE00", "team": "T12345"})
    c.register(
        "users.list",
        {
            "ok": True,
            "members": [
                {
                    "id": "UALICE00",
                    "name": "alice",
                    "real_name": "Alice Engineer",
                    "profile": {
                        "real_name": "Alice Engineer",
                        "display_name": "alice",
                        "email": "alice@example.com",
                        "title": "Tech Lead",
                    },
                    "is_bot": False,
                    "deleted": False,
                    "team_id": "T12345",
                },
                {
                    "id": "UBOB0001",
                    "name": "bob.builder",
                    "real_name": "Robert Builder",
                    "profile": {
                        "real_name": "Robert Builder",
                        "display_name": "bob",
                        "email": "robert.builder@example.com",
                        "title": "Engineer",
                    },
                    "is_bot": False,
                    "deleted": False,
                    "team_id": "T12345",
                },
                {
                    "id": "UCARLA01",
                    "name": "carla",
                    "real_name": "Carla Vega",
                    "profile": {
                        "real_name": "Carla Vega",
                        "display_name": "carla",
                        "email": None,
                    },
                },
                {
                    "id": "BBOT0001",
                    "name": "ci-bot",
                    "real_name": "CI Bot",
                    "is_bot": True,
                },
            ],
            "response_metadata": {"next_cursor": ""},
        },
    )
    c.register(
        "conversations.list",
        {
            "ok": True,
            "channels": [
                {
                    "id": "CGENERAL",
                    "name": "general",
                    "is_channel": True,
                    "is_private": False,
                    "is_archived": False,
                    "topic": {"value": "Company-wide"},
                    "purpose": {"value": "Everyone"},
                },
                {
                    "id": "CENGTEAM",
                    "name": "engineering",
                    "is_channel": True,
                    "is_private": False,
                    "topic": {"value": "All things eng"},
                    "purpose": {"value": "Engineering chat"},
                },
                {
                    "id": "DBOB0001",
                    "name": "UBOB0001",
                    "is_im": True,
                    "user": "UBOB0001",
                },
                {
                    "id": "GTHREE01",
                    "name": "mpdm-alice--bob--carla-1",
                    "is_mpim": True,
                },
            ],
            "response_metadata": {"next_cursor": ""},
        },
    )
    return c
