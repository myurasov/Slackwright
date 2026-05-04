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

"""Slack web-app HTTP client (plain ``httpx``, no browser).

Login is the only flow that needs a real Chromium (SAML / MFA / SSO
redirects); see :mod:`slackwright.auth`. Every other call path —
``search.modules.messages``, ``users.info``, ``conversations.info``,
file downloads — is just an HTTP POST/GET that needs the right cookie
jar and the ``xoxc-`` token. We replay both from
``<state-dir>/playwright-state.json`` (the JSON Playwright wrote at
login time, in its standard ``{cookies, origins}`` shape) and write
rotated cookies back on close so long-lived sessions stay alive.

The previous implementation drove every API call through
``page.request`` inside a Playwright-launched Chromium so the cookie
jar (including the HttpOnly ``d``-family cookies) and TLS fingerprint
matched a real browser. That was robust but heavy: each fetch run
spawned a hidden Chromium just to issue form-encoded POSTs. ``httpx``
is enough — Slack's ``api/<method>`` accepts the same cookies + token
regardless of who's holding the TCP socket.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

import httpx

from .auth import AuthBundle
from .cost import CostTracker

# A current-stable Chromium UA. Slack's API doesn't strictly require it
# but the desktop / web clients send something Chromium-shaped, so we
# keep traffic indistinguishable from the regular page.
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# Slack's web client never sets these explicitly, but our stripped-down
# request sometimes runs without them, which causes cors-y rejections.
# Setting them to the workspace origin replicates a normal page request.
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

        with SlackWebClient.open(bundle, state_dir=...) as c:
            data = c.api("users.info", {"user": "U123"})
            blob = c.download_file(url)

    Construct via :meth:`open` (the context-manager helper); it loads
    cookies from the storage-state JSON, rides them through one
    long-lived ``httpx.Client``, and writes rotated cookies back when
    the context exits.

    The legacy ``headed`` / ``executable_path`` keyword arguments are
    accepted for backward compatibility (older scripts may still pass
    them) but ignored — there is no browser process to launch.
    """

    def __init__(
        self,
        *,
        bundle: AuthBundle,
        http: httpx.Client,
        storage_state_path: Path,
        storage_state: dict[str, Any],
        cost: CostTracker | None = None,
        auto_refresh: bool = True,
    ) -> None:
        self.bundle = bundle
        self._http = http
        self._ssp = storage_state_path
        self._storage_state = storage_state
        self.cost = cost or CostTracker()
        self._auto_refresh = auto_refresh

    # --- lifecycle ---

    @classmethod
    @contextmanager
    def open(
        cls,
        bundle: AuthBundle,
        *,
        state_dir: Path,
        headed: bool = False,  # noqa: ARG003 — kept for backward-compat
        executable_path: str | None = None,  # noqa: ARG003 — kept for backward-compat
        cost: CostTracker | None = None,
        auto_refresh: bool = True,
    ) -> Iterator[SlackWebClient]:
        """Open an authed httpx session against the workspace.

        Loads the cookie jar from ``<state-dir>/playwright-state.json``
        (Playwright's storage-state shape) on entry, persists rotated
        cookies back on exit so long sessions don't lose their refreshed
        ``d-s`` value.
        """
        from .paths import storage_state_path

        ssp = storage_state_path(state_dir)
        if not ssp.exists():
            raise FileNotFoundError(
                f"missing session state at {ssp}. Run `slackwright login` first."
            )

        try:
            storage_state = json.loads(ssp.read_text(encoding="utf-8"))
        except Exception as e:
            raise SlackWebError(
                "open",
                f"could not read storage state at {ssp}: {e}",
            ) from e
        if not isinstance(storage_state, dict):
            raise SlackWebError("open", f"storage state at {ssp} is not a JSON object")

        jar = _build_cookie_jar(storage_state.get("cookies") or [])
        http = httpx.Client(
            cookies=jar,
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={
                "User-Agent": _DEFAULT_UA,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=False,
        )
        try:
            yield cls(
                bundle=bundle,
                http=http,
                storage_state_path=ssp,
                storage_state=storage_state,
                cost=cost,
                auto_refresh=auto_refresh,
            )
            with contextlib.suppress(Exception):
                _persist_storage_state(ssp, storage_state, http.cookies.jar)
        finally:
            http.close()

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
        :class:`SlackWebError` for documented ``ok: false`` errors.
        Cookies are managed by the underlying ``httpx.Client``: any
        ``Set-Cookie`` Slack returns is automatically merged into the
        jar and persisted back to disk on context-manager exit.
        """
        body = dict(params or {})
        body.setdefault("token", self.bundle.api_token)
        url = f"{self.bundle.api_url}/{method}"
        # Best-effort outbound size: form-encoded body length is the
        # dominant byte cost; we don't bother with the headers.
        bytes_out_estimate = sum(len(str(k)) + len(str(v)) + 2 for k, v in body.items())
        timeout_s = timeout_ms / 1000.0

        attempt = 0
        refresh_attempted = False
        last_status: int | None = None
        last_text: str = ""
        while True:
            try:
                resp = self._http.post(
                    url,
                    data=body,
                    headers=_origin_headers(self.bundle.api_url),
                    timeout=timeout_s,
                )
            except httpx.HTTPError as e:
                self.cost.record_transport_error()
                if attempt >= len(backoff_schedule):
                    raise SlackWebError(method, f"transport: {e}") from e
                wait = backoff_schedule[attempt]
                attempt += 1
                self.cost.record_retry()
                sys.stderr.write(
                    f"[slackwright] {method} transport error ({e}); "
                    f"retry {attempt}/{len(backoff_schedule)} in {wait}s\n"
                )
                time.sleep(wait)
                continue

            last_status = resp.status_code
            last_text = resp.text or ""
            self.cost.record_api_call(
                method,
                bytes_in=len(last_text.encode("utf-8", errors="ignore")),
                bytes_out=bytes_out_estimate,
            )
            if resp.is_success:
                try:
                    payload = resp.json()
                except Exception as e:
                    raise SlackWebError(method, f"non-JSON 2xx body: {last_text[:200]!r}") from e
                if isinstance(payload, dict) and payload.get("ok"):
                    return payload
                err = (payload or {}).get("error") if isinstance(payload, dict) else "unknown"
                self.cost.record_api_error()
                # ratelimited can be wrapped in ok:false too
                if err == "ratelimited":
                    if attempt >= len(backoff_schedule):
                        raise SlackWebError(method, err, payload)
                    retry_after = int(resp.headers.get("retry-after") or backoff_schedule[attempt])
                    attempt += 1
                    self.cost.record_retry()
                    self.cost.record_rate_limit_sleep(retry_after)
                    sys.stderr.write(
                        f"[slackwright] {method} ratelimited; "
                        f"sleeping {retry_after}s (retry {attempt}/{len(backoff_schedule)})\n"
                    )
                    time.sleep(retry_after)
                    continue
                if (
                    self._auto_refresh
                    and not refresh_attempted
                    and self.is_token_error(err or "")
                ):
                    # One refresh attempt per api() call — avoids both
                    # infinite loops (failed refresh → same error → loop)
                    # and "first call refreshes, second call refuses to"
                    # for long-running fetches that span multiple xoxc
                    # rotations.
                    refresh_attempted = True
                    if self._try_refresh_token(method):
                        body["token"] = self.bundle.api_token
                        continue
                raise SlackWebError(
                    method, err or "unknown", payload if isinstance(payload, dict) else None
                )

            if resp.status_code in _TRANSIENT_HTTP_STATUSES and attempt < len(backoff_schedule):
                retry_after_header = resp.headers.get("retry-after")
                wait = (
                    int(retry_after_header)
                    if retry_after_header and retry_after_header.isdigit()
                    else backoff_schedule[attempt]
                )
                attempt += 1
                self.cost.record_retry()
                if resp.status_code == 429:
                    self.cost.record_rate_limit_sleep(wait)
                sys.stderr.write(
                    f"[slackwright] {method} HTTP {resp.status_code}; "
                    f"retry {attempt}/{len(backoff_schedule)} in {wait}s\n"
                )
                time.sleep(wait)
                continue

            raise SlackWebError(
                method,
                f"HTTP {resp.status_code}: {last_text[:200]!r}",
            )

        # Unreachable, but keeps the type checker happy.
        raise SlackWebError(method, f"HTTP {last_status}: {last_text[:200]!r}")

    # --- token refresh ---

    def _try_refresh_token(self, method: str) -> bool:
        """Re-establish auth: headless refresh first, interactive login as fallback.

        Two-tier strategy because Slack's ``xoxc`` lifetime and its
        cookie session lifetime aren't the same:

          1. *Token-only expiry* (common): the cookies are still good,
             only the bearer token rotated. A short-lived headless
             Chromium can navigate to the workspace, read the freshly-
             issued ``boot_data.api_token``, and we're back in business
             with no user interaction.

          2. *Session expiry* (Enterprise Grid + SAML): the IdP refused
             silent SSO from our automated browser (Microsoft / Okta
             require a user gesture for safety). The headless attempt
             will fail with :class:`TokenRefreshError`. If we're on a
             TTY we open a real headed login window so the user can
             complete SSO once and the fetch keeps going. Non-
             interactive shells (cron / CI) skip this and surface the
             original ``invalid_auth`` to the caller.

        Returns ``True`` on success. The on-disk auth + storage state
        get rewritten and the in-memory cookie jar / bundle are
        refreshed before returning.
        """
        from .auth import LoginSession, TokenRefreshError, refresh_token_headless

        sys.stderr.write(
            f"[slackwright] {method} returned invalid_auth — "
            f"refreshing xoxc token via headless browser…\n"
        )
        new_bundle: AuthBundle | None = None
        try:
            new_bundle = refresh_token_headless(
                self.bundle,
                state_dir=self._ssp.parent,
                verbose=True,
            )
        except TokenRefreshError as e:
            sys.stderr.write(f"[slackwright] token refresh failed: {e}\n")
            if self._can_run_interactive_login():
                sys.stderr.write(
                    "[slackwright] opening browser for full re-login "
                    "(SSO needs a real user gesture)…\n"
                )
                try:
                    with LoginSession(
                        workspace_url=self.bundle.workspace_url,
                        state_dir=self._ssp.parent,
                    ) as s:
                        new_bundle = s.run_interactive(timeout_s=300)
                except Exception as login_e:
                    sys.stderr.write(
                        f"[slackwright] interactive login failed: {login_e}\n"
                    )
                    return False
            else:
                sys.stderr.write(
                    "[slackwright] non-interactive shell — "
                    "run `slackwright login --workspace ...` to re-authenticate.\n"
                )
                return False
        except Exception as e:
            sys.stderr.write(f"[slackwright] token refresh errored: {e}\n")
            return False

        if new_bundle is None:
            return False

        # The refresh path (headless or interactive) rewrote
        # storage-state on disk — reload cookies into our jar so the
        # very next request rides the fresh set instead of the now-
        # stale in-memory copy.
        try:
            fresh_state = json.loads(self._ssp.read_text(encoding="utf-8"))
        except Exception:
            fresh_state = self._storage_state
        self._http.cookies = _build_cookie_jar(fresh_state.get("cookies") or [])
        self._storage_state = fresh_state
        self.bundle = new_bundle
        sys.stderr.write("[slackwright] session refreshed; retrying request.\n")
        return True

    @staticmethod
    def _can_run_interactive_login() -> bool:
        """True when both stderr and stdin are TTYs.

        Stdin's TTY-ness is what tells us a real human is at the
        keyboard ready to complete SSO; stderr's is just a
        belt-and-braces check for terminal output.
        """
        try:
            return sys.stderr.isatty() and sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    # --- file download ---

    def download_file(self, url: str, *, timeout_ms: int = 120_000) -> bytes:
        """GET a Slack file URL using the authed cookie jar."""
        resp = self._http.get(
            url,
            headers=_origin_headers(self.bundle.api_url),
            timeout=timeout_ms / 1000.0,
        )
        if not resp.is_success:
            raise SlackWebError(
                "download_file",
                f"HTTP {resp.status_code} for {url}: {(resp.text or '')[:200]!r}",
            )
        body = resp.content
        self.cost.record_file_download(bytes_in=len(body))
        return body

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


# ---------------------------------------------------------------------------
# Cookie-jar <-> Playwright storage_state.json round-trip
# ---------------------------------------------------------------------------


def _build_cookie_jar(cookies: list[dict[str, Any]]) -> httpx.Cookies:
    """Hydrate an :class:`httpx.Cookies` jar from Playwright's storage shape.

    Each Playwright cookie dict carries ``name``, ``value``, ``domain``,
    ``path``, ``expires`` (-1 for session cookies, otherwise unix
    seconds), and the ``httpOnly`` / ``secure`` flags. We translate
    those into stdlib :class:`http.cookiejar.Cookie` instances and add
    them to httpx's jar — that's the same jar that auto-merges any
    ``Set-Cookie`` headers Slack returns mid-session.
    """
    jar = httpx.Cookies()
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not name:
            continue
        value = c.get("value", "")
        domain = c.get("domain") or ""
        path = c.get("path") or "/"
        expires_raw = c.get("expires")
        expires = (
            int(expires_raw)
            if isinstance(expires_raw, (int, float)) and expires_raw and expires_raw > 0
            else None
        )
        rest: dict[str, str] = {}
        if c.get("httpOnly"):
            rest["HttpOnly"] = ""
        cookie = Cookie(
            version=0,
            name=str(name),
            value=str(value),
            port=None,
            port_specified=False,
            domain=str(domain),
            domain_specified=bool(domain),
            domain_initial_dot=str(domain).startswith("."),
            path=str(path),
            path_specified=True,
            secure=bool(c.get("secure", False)),
            expires=expires,
            discard=expires is None,
            comment=None,
            comment_url=None,
            rest=rest,
            rfc2109=False,
        )
        jar.jar.set_cookie(cookie)
    return jar


def _persist_storage_state(path: Path, original: dict[str, Any], jar: Any) -> None:
    """Serialize the rotated cookie jar back to Playwright's JSON shape.

    Cookies present in the jar replace the on-disk ``cookies`` block;
    the ``origins`` block is preserved verbatim so localStorage written
    at login isn't dropped. Failures are swallowed by the caller — a
    stale storage state means the next run re-uses the previously-known
    cookies, which is acceptable.
    """
    fresh: list[dict[str, Any]] = []
    for cookie in jar:
        d: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value or "",
            "domain": cookie.domain or "",
            "path": cookie.path or "/",
            "expires": int(cookie.expires) if cookie.expires else -1,
            "httpOnly": "HttpOnly" in (cookie._rest or {}) if hasattr(cookie, "_rest") else False,
            "secure": bool(cookie.secure),
            "sameSite": "Lax",
        }
        fresh.append(d)
    payload = {
        "cookies": fresh,
        "origins": original.get("origins") or [],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
