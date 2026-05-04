# Copyright 2026 Mikhail Yurasov
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for :mod:`slackwright.client` — the httpx-based session.

We can't actually talk to Slack here, but we can mount an
``httpx.MockTransport`` inside the client to exercise the contract:

  - cookies hydrated from ``playwright-state.json`` ride on the request
  - rotated ``Set-Cookie`` headers persist back on context exit
  - ``ok: false`` responses raise :class:`SlackWebError`
  - retryable HTTP statuses + ``ratelimited`` honour the backoff
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from slackwright.auth import AuthBundle
from slackwright.client import SlackWebClient, SlackWebError


def _write_storage_state(path: Path, cookies: list[dict]) -> None:
    path.write_text(
        json.dumps({"cookies": cookies, "origins": []}, indent=2),
        encoding="utf-8",
    )


def _bundle_for(state_dir: Path) -> AuthBundle:
    return AuthBundle(
        workspace_url="https://acme.slack.com",
        api_url="https://acme.slack.com/api",
        api_token="xoxc-test-token",
        team_id="T12345",
        enterprise_id=None,
        user_id="UALICE00",
        user_name="alice",
        user_real_name="Alice Engineer",
        user_email="alice@example.com",
        extracted_at=0.0,
        storage_state_path=str(state_dir / "playwright-state.json"),
    )


class TestCookieRoundTrip:
    def test_cookies_attached_to_request(self, tmp_path: Path) -> None:
        _write_storage_state(
            tmp_path / "playwright-state.json",
            [
                {
                    "name": "d",
                    "value": "xoxd-abc",
                    "domain": ".slack.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
        )
        seen_cookies: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            cookie_hdr = request.headers.get("cookie", "")
            for pair in cookie_hdr.split("; "):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    seen_cookies[k] = v
            return httpx.Response(200, json={"ok": True, "result": "yes"})

        bundle = _bundle_for(tmp_path)
        with SlackWebClient.open(bundle, state_dir=tmp_path) as client:
            client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
            data = client.api("auth.test")
        assert data["ok"] is True
        assert seen_cookies.get("d") == "xoxd-abc"

    def test_set_cookie_rotates_storage_state(self, tmp_path: Path) -> None:
        ssp = tmp_path / "playwright-state.json"
        _write_storage_state(
            ssp,
            [
                {
                    "name": "d",
                    "value": "xoxd-old",
                    "domain": ".slack.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                }
            ],
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"set-cookie": "d=xoxd-rotated; Domain=.slack.com; Path=/; Secure"},
            )

        bundle = _bundle_for(tmp_path)
        with SlackWebClient.open(bundle, state_dir=tmp_path) as client:
            client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
            client.api("auth.test")

        persisted = json.loads(ssp.read_text())
        names_to_values = {c["name"]: c["value"] for c in persisted["cookies"]}
        assert names_to_values.get("d") == "xoxd-rotated"


class TestApiErrorHandling:
    def test_ok_false_raises_slack_web_error(self, tmp_path: Path) -> None:
        _write_storage_state(tmp_path / "playwright-state.json", [])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "user_not_found"})

        bundle = _bundle_for(tmp_path)
        # auto_refresh path is exercised in TestTokenAutoRefresh — here
        # we use a non-token error to assert the plain raise path.
        with SlackWebClient.open(bundle, state_dir=tmp_path, auto_refresh=False) as client:
            client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
            with pytest.raises(SlackWebError) as exc:
                client.api("users.info")
        assert exc.value.error == "user_not_found"

    def test_5xx_retries_then_succeeds(self, tmp_path: Path, monkeypatch) -> None:
        _write_storage_state(tmp_path / "playwright-state.json", [])
        # Skip the real backoff sleep so the test stays fast.
        monkeypatch.setattr("slackwright.client.time.sleep", lambda _s: None)

        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, text="upstream down")
            return httpx.Response(200, json={"ok": True, "data": "ok-now"})

        bundle = _bundle_for(tmp_path)
        with SlackWebClient.open(bundle, state_dir=tmp_path) as client:
            client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
            data = client.api("auth.test", backoff_schedule=(0,))
        assert data == {"ok": True, "data": "ok-now"}
        assert calls["n"] == 2

    def test_token_error_classifier(self) -> None:
        assert SlackWebClient.is_token_error("invalid_auth")
        assert SlackWebClient.is_token_error("token_expired")
        assert not SlackWebClient.is_token_error("ratelimited")


class TestTokenAutoRefresh:
    def test_invalid_auth_triggers_one_refresh_then_succeeds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_storage_state(tmp_path / "playwright-state.json", [])
        bundle = _bundle_for(tmp_path)

        # Stub the headless refresh: pretend we got a fresh xoxc token.
        from slackwright import auth as auth_mod

        def fake_refresh(b, *, state_dir, **_kw):
            import dataclasses

            return dataclasses.replace(b, api_token="xoxc-FRESH-TOKEN")

        monkeypatch.setattr(auth_mod, "refresh_token_headless", fake_refresh)

        seen_tokens: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = (request.content or b"").decode()
            for pair in body.split("&"):
                if pair.startswith("token="):
                    seen_tokens.append(pair.split("=", 1)[1])
            if len(seen_tokens) == 1:
                return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
            return httpx.Response(200, json={"ok": True, "data": "post-refresh"})

        with SlackWebClient.open(bundle, state_dir=tmp_path) as client:
            client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
            data = client.api("auth.test")
        assert data["ok"] is True
        assert seen_tokens[0].startswith("xoxc-test")
        assert seen_tokens[1] == "xoxc-FRESH-TOKEN"

    def test_refresh_failure_surfaces_original_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_storage_state(tmp_path / "playwright-state.json", [])
        bundle = _bundle_for(tmp_path)

        from slackwright import auth as auth_mod

        def boom(b, *, state_dir, **_kw):
            raise auth_mod.TokenRefreshError("cookies expired")

        monkeypatch.setattr(auth_mod, "refresh_token_headless", boom)
        # Force the non-interactive branch — otherwise the headless
        # failure would trip the LoginSession fallback (which would try
        # to launch a real Chromium under the test harness).
        monkeypatch.setattr(SlackWebClient, "_can_run_interactive_login", staticmethod(lambda: False))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

        with SlackWebClient.open(bundle, state_dir=tmp_path) as client:
            client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
            with pytest.raises(SlackWebError) as exc:
                client.api("auth.test")
        assert exc.value.error == "invalid_auth"

    def test_interactive_login_fallback_when_headless_refresh_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Headless refresh fails (cookie-level expiry, e.g. SAML SSO).
        On a TTY we fall back to a real ``LoginSession`` so the user
        completes SSO once and the in-flight call retries."""
        _write_storage_state(tmp_path / "playwright-state.json", [])
        bundle = _bundle_for(tmp_path)

        from slackwright import auth as auth_mod

        def boom(b, *, state_dir, **_kw):
            raise auth_mod.TokenRefreshError("sso_failed")

        monkeypatch.setattr(auth_mod, "refresh_token_headless", boom)
        monkeypatch.setattr(SlackWebClient, "_can_run_interactive_login", staticmethod(lambda: True))

        # Stub LoginSession: pretend the user completed SSO and we got
        # a fresh bundle. The session should be a context manager.
        login_called = {"n": 0}

        class FakeLoginSession:
            def __init__(self, **kw):
                self.workspace_url = kw["workspace_url"]
                self.state_dir = kw["state_dir"]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def run_interactive(self, *, timeout_s: int):
                login_called["n"] += 1
                import dataclasses

                return dataclasses.replace(bundle, api_token="xoxc-AFTER-SSO")

        monkeypatch.setattr(auth_mod, "LoginSession", FakeLoginSession)

        seen_tokens: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = (request.content or b"").decode()
            for pair in body.split("&"):
                if pair.startswith("token="):
                    seen_tokens.append(pair.split("=", 1)[1])
            if len(seen_tokens) == 1:
                return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
            return httpx.Response(200, json={"ok": True, "data": "post-login"})

        with SlackWebClient.open(bundle, state_dir=tmp_path) as client:
            client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
            data = client.api("auth.test")

        assert data["ok"] is True
        assert login_called["n"] == 1
        assert seen_tokens[1] == "xoxc-AFTER-SSO"

    def test_no_refresh_disables_path(self, tmp_path: Path, monkeypatch) -> None:
        _write_storage_state(tmp_path / "playwright-state.json", [])
        bundle = _bundle_for(tmp_path)

        from slackwright import auth as auth_mod

        called = {"n": 0}

        def fake_refresh(*_a, **_k):
            called["n"] += 1
            import dataclasses

            return dataclasses.replace(bundle, api_token="xoxc-FRESH")

        monkeypatch.setattr(auth_mod, "refresh_token_headless", fake_refresh)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

        with SlackWebClient.open(bundle, state_dir=tmp_path, auto_refresh=False) as client:
            client._http._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
            with pytest.raises(SlackWebError):
                client.api("auth.test")
        assert called["n"] == 0


class TestOpenMissingState:
    def test_raises_when_storage_state_absent(self, tmp_path: Path) -> None:
        bundle = _bundle_for(tmp_path)
        with (
            pytest.raises(FileNotFoundError),
            SlackWebClient.open(bundle, state_dir=tmp_path),
        ):
            pass

    def test_legacy_kwargs_silently_ignored(self, tmp_path: Path) -> None:
        # Older scripts may still pass ``headed`` / ``executable_path`` —
        # keep them no-op rather than raising TypeError.
        _write_storage_state(tmp_path / "playwright-state.json", [])
        bundle = _bundle_for(tmp_path)
        with SlackWebClient.open(
            bundle,
            state_dir=tmp_path,
            headed=True,
            executable_path="/path/to/chromium",
        ) as client:
            assert client.bundle is bundle
