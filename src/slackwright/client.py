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

"""Slack web-app HTTP client driven through Playwright.

Why Playwright instead of plain ``requests``?

  - The Slack web app frequently rejects requests that don't carry the full
    cookie jar set up at login (``d``, ``d-s``, ``lc``, ``b``, …) including
    HttpOnly cookies that browser DevTools never let you copy.
  - Driving the session through a real Chromium context means our HTTP
    requests are indistinguishable from the user's normal traffic — same
    cookies, same TLS fingerprint, same User-Agent.
  - The same session can render UI as needed (debugging) without any
    auth divergence.

The client is intentionally thin: it knows how to call ``api/<method>``
(form-encoded POST, ``token=xoxc-...`` body field) and how to download
file URLs (binary GET), with retry/backoff for transient errors. Higher-
level concerns (search, resolve, archive) live in their own modules.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .auth import AuthBundle


# Slack's web client never sets these explicitly, but our Playwright build
# sometimes runs without them, which causes cors-y rejections. Setting them
# to the workspace origin replicates a normal page request.
def _origin_headers(api_url: str) -> dict[str, str]:
    base = api_url.rsplit("/api", 1)[0]
    return {
        "Origin": base,
        "Referer": f"{base}/",
    }


_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504, 522, 524}
_DEFAULT_BACKOFF_S = (5, 15, 30, 60, 120)


class SlackWebError(RuntimeError):
    """Raised when the Slack API returns a documented error
    (``ok: false``) that isn't ratelimited / retried away."""

    def __init__(self, method: str, error: str, body: dict[str, Any] | None = None) -> None:
        super().__init__(f"slack API {method!r} returned error: {error}")
        self.method = method
        self.error = error
        self.body = body or {}


class SlackWebClient:
    """Minimal Slack web-app client.

    Usage::

        with SlackWebClient.open(bundle, headed=False, state_dir=...) as c:
            data = c.api("users.info", {"user": "U123"})
            for chunk in c.download_file(url):
                ...

    Construct via :meth:`open` (the context-manager helper); it owns the
    Playwright lifecycle and persists storage state on close.
    """

    def __init__(
        self,
        *,
        bundle: AuthBundle,
        page,
        context,
        playwright,
        browser,
        state_dir: Path,
    ) -> None:
        self.bundle = bundle
        self._page = page
        self._context = context
        self._playwright = playwright
        self._browser = browser
        self._state_dir = state_dir

    # --- lifecycle ---

    @classmethod
    @contextmanager
    def open(
        cls,
        bundle: AuthBundle,
        *,
        state_dir: Path,
        headed: bool = False,
        executable_path: str | None = None,
    ) -> Iterator[SlackWebClient]:
        """Spin up a Playwright context with the saved storage state and
        yield a ready-to-use client.

        We always navigate the page to the workspace once before the first
        API call: Slack only accepts ``api/`` calls when the request comes
        from a context that's been on the workspace origin (so cookies are
        scoped correctly).
        """
        from playwright.sync_api import sync_playwright

        from .paths import storage_state_path

        ssp = storage_state_path(state_dir)
        if not ssp.exists():
            raise FileNotFoundError(
                f"missing Playwright storage state at {ssp}. Run `slackwright login` first."
            )

        playwright = sync_playwright().start()
        try:
            launch_kwargs: dict[str, Any] = {"headless": not headed}
            if executable_path:
                launch_kwargs["executable_path"] = executable_path
            browser = playwright.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context(
                    storage_state=str(ssp),
                    viewport={"width": 1280, "height": 900},
                )
                try:
                    page = context.new_page()
                    page.goto(bundle.workspace_url, wait_until="domcontentloaded", timeout=60_000)
                    client = cls(
                        bundle=bundle,
                        page=page,
                        context=context,
                        playwright=playwright,
                        browser=browser,
                        state_dir=state_dir,
                    )
                    yield client
                    # Refresh the persisted storage state so cookies that
                    # rotated during this run are kept for next time.
                    with contextlib.suppress(Exception):
                        context.storage_state(path=str(ssp))
                finally:
                    context.close()
            finally:
                browser.close()
        finally:
            playwright.stop()

    # --- API call ---

    def api(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        backoff_schedule: tuple[int, ...] = _DEFAULT_BACKOFF_S,
        timeout_ms: int = 60_000,
    ) -> dict[str, Any]:
        """Call ``<api_url>/<method>`` with form-encoded params + xoxc token.

        Retries 429 / 5xx with the supplied backoff schedule. Raises
        :class:`SlackWebError` for documented ``ok: false`` errors. The
        request goes through Playwright's ``page.request`` so all session
        cookies are attached automatically.
        """
        body = dict(params or {})
        body.setdefault("token", self.bundle.api_token)
        url = f"{self.bundle.api_url}/{method}"

        attempt = 0
        last_status: int | None = None
        last_text: str = ""
        while True:
            try:
                resp = self._page.request.post(
                    url,
                    form=body,
                    headers=_origin_headers(self.bundle.api_url),
                    timeout=timeout_ms,
                )
            except Exception as e:
                if attempt >= len(backoff_schedule):
                    raise SlackWebError(method, f"transport: {e}") from e
                wait = backoff_schedule[attempt]
                attempt += 1
                sys.stderr.write(
                    f"[slackwright] {method} transport error ({e}); "
                    f"retry {attempt}/{len(backoff_schedule)} in {wait}s\n"
                )
                time.sleep(wait)
                continue

            last_status = resp.status
            last_text = resp.text() or ""
            if resp.ok:
                try:
                    payload = resp.json()
                except Exception as e:
                    raise SlackWebError(method, f"non-JSON 2xx body: {last_text[:200]!r}") from e
                if isinstance(payload, dict) and payload.get("ok"):
                    return payload
                err = (payload or {}).get("error") if isinstance(payload, dict) else "unknown"
                # ratelimited can be wrapped in ok:false too
                if err == "ratelimited":
                    if attempt >= len(backoff_schedule):
                        raise SlackWebError(method, err, payload)
                    retry_after = int(resp.headers.get("retry-after") or backoff_schedule[attempt])
                    attempt += 1
                    sys.stderr.write(
                        f"[slackwright] {method} ratelimited; "
                        f"sleeping {retry_after}s (retry {attempt}/{len(backoff_schedule)})\n"
                    )
                    time.sleep(retry_after)
                    continue
                raise SlackWebError(method, err or "unknown", payload if isinstance(payload, dict) else None)

            if resp.status in _TRANSIENT_HTTP_STATUSES and attempt < len(backoff_schedule):
                retry_after_header = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
                wait = int(retry_after_header) if retry_after_header and retry_after_header.isdigit() else backoff_schedule[attempt]
                attempt += 1
                sys.stderr.write(
                    f"[slackwright] {method} HTTP {resp.status}; "
                    f"retry {attempt}/{len(backoff_schedule)} in {wait}s\n"
                )
                time.sleep(wait)
                continue

            raise SlackWebError(
                method,
                f"HTTP {resp.status}: {last_text[:200]!r}",
            )

        # Unreachable, but keeps the type checker happy.
        raise SlackWebError(method, f"HTTP {last_status}: {last_text[:200]!r}")

    # --- file download ---

    def download_file(self, url: str, *, timeout_ms: int = 120_000) -> bytes:
        """GET a Slack file URL using the authed Playwright context."""
        resp = self._page.request.get(
            url,
            headers=_origin_headers(self.bundle.api_url),
            timeout=timeout_ms,
        )
        if not resp.ok:
            raise SlackWebError(
                "download_file",
                f"HTTP {resp.status} for {url}: {(resp.text() or '')[:200]!r}",
            )
        return resp.body()

    # --- diagnostics ---

    def health(self) -> dict[str, Any]:
        """Cheap end-to-end smoke test: ``auth.test``."""
        return self.api("auth.test", {})

    @staticmethod
    def is_token_error(err: str) -> bool:
        return err in {
            "invalid_auth",
            "not_authed",
            "token_revoked",
            "account_inactive",
            "token_expired",
        }
