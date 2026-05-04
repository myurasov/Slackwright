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

"""Login flow + Slack web-app token extraction.

The login flow opens a real Chromium window pointed at the user's workspace
and waits for the user to complete whatever auth journey their org requires
(SSO, MFA, etc.). Once the Slack web client is loaded, we extract:

  - the **xoxc** API token (from ``boot_data.api_token`` injected by the
    Slack web client into ``window``)
  - the **d** cookie (the long-lived auth cookie set on ``.slack.com``)
  - the **team** / **enterprise** metadata (``boot_data.team_id``,
    ``boot_data.enterprise_id``, etc.)
  - the **logged-in user**'s id, real name and email

These are the same credentials the Slack desktop client uses, so any API
endpoint reachable from the web client (search, conversations.history,
users.list, files.info, ...) is reachable here.

Storage:
  - Playwright storage state goes to ``<state-dir>/playwright-state.json``
  - The extracted token bundle goes to ``<state-dir>/auth.json``

Both files contain credentials. Treat them as you would an SSH key: don't
commit them, don't share them. The default state dir
(``~/.cache/slackwright/``) is mode 0700; the auth files inside are 0600.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_DEFAULT_LOGIN_TIMEOUT_S = 300
_BOOT_DATA_POLL_INTERVAL_S = 1.0


@dataclasses.dataclass
class AuthBundle:
    """Everything we extracted from a logged-in Slack web session.

    Persisted to ``<state-dir>/auth.json``. ``storage_state_path`` points at
    the sibling file Playwright wrote with cookies + localStorage; reload
    them together via ``load_auth(state_dir)``.
    """

    workspace_url: str  # e.g. https://acme.enterprise.slack.com
    api_url: str  # e.g. https://acme.enterprise.slack.com/api
    api_token: str  # xoxc-...
    team_id: str | None
    enterprise_id: str | None
    user_id: str
    user_name: str | None  # Slack handle (boot_data.username)
    user_real_name: str | None
    user_email: str | None
    extracted_at: float
    storage_state_path: str

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> AuthBundle:
        return cls(**d)


def workspace_to_api_url(workspace_url: str) -> str:
    """``https://acme.slack.com`` -> ``https://acme.slack.com/api``."""
    p = urlparse(workspace_url)
    if not p.scheme or not p.netloc:
        raise ValueError(f"workspace URL must include scheme and host: {workspace_url!r}")
    return f"{p.scheme}://{p.netloc}/api"


def normalize_workspace_url(value: str) -> str:
    """Accept ``acme``, ``acme.slack.com``, or a full URL; return canonical URL.

    A bare token is treated as the workspace short-name (``<name>.slack.com``).
    Enterprise Grid users are expected to pass the full URL because both
    ``acme.enterprise.slack.com`` and ``acme.slack.com`` exist for many orgs.
    """
    v = value.strip()
    if v.startswith(("http://", "https://")):
        return v.rstrip("/")
    if "." in v:
        return f"https://{v.rstrip('/')}"
    return f"https://{v}.slack.com"


# ---------------------------------------------------------------------------
# JS snippets — kept as constants so we can unit-test the parsing
# ---------------------------------------------------------------------------


# Slack moved its boot data twice in the last few years. We probe the modern
# location first, fall back to the legacy ones, and scrape ``localConfig_v2``
# from localStorage as a last resort. Returns null if nothing matches.
EXTRACT_BOOT_DATA_JS = r"""
() => {
  const out = {};
  const bd = (window).boot_data;
  if (bd) {
    out.api_token = bd.api_token || null;
    out.team_id = bd.team_id || (bd.team && bd.team.id) || null;
    out.team_url = bd.team_url || (bd.team && bd.team.url) || null;
    out.team_name = bd.team_name || (bd.team && bd.team.name) || null;
    out.enterprise_id = bd.enterprise_id || (bd.enterprise && bd.enterprise.id) || null;
    out.user_id = bd.user_id || bd.user || null;
    out.username = bd.username || bd.user_name || null;
    out.real_name = bd.real_name || null;
    out.email = bd.email || null;
    if (out.api_token) return out;
  }

  try {
    const raw = localStorage.getItem("localConfig_v2");
    if (raw) {
      const cfg = JSON.parse(raw);
      const teams = cfg.teams || {};
      const ids = Object.keys(teams);
      if (ids.length) {
        const t = teams[ids[0]];
        out.api_token = t.token || null;
        out.team_id = t.id || ids[0];
        out.team_url = t.url || null;
        out.team_name = t.name || null;
        out.user_id = t.user_id || null;
        out.username = t.name || null;
        if (out.api_token) return out;
      }
    }
  } catch (e) {}

  return null;
}
"""


# ---------------------------------------------------------------------------
# Login session
# ---------------------------------------------------------------------------


class LoginSession:
    """One-shot login orchestration.

    Usage:

        with LoginSession(workspace_url="https://acme.slack.com",
                          state_dir=Path("~/.cache/slackwright")) as s:
            s.run_interactive(timeout_s=600)
            bundle = s.bundle  # AuthBundle on success

    All Playwright imports happen lazily inside ``__enter__`` so that
    importing :mod:`slackwright.auth` is cheap (and so we can unit-test the
    helpers without a Playwright install).
    """

    def __init__(
        self,
        *,
        workspace_url: str,
        state_dir: Path,
        executable_path: str | None = None,
    ) -> None:
        self.workspace_url = normalize_workspace_url(workspace_url)
        self.api_url = workspace_to_api_url(self.workspace_url)
        self.state_dir = Path(state_dir)
        self.executable_path = executable_path
        self._playwright = None
        self._browser = None
        self._context = None
        self.bundle: AuthBundle | None = None

    # --- lifecycle ---

    def __enter__(self) -> LoginSession:
        from playwright.sync_api import sync_playwright

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": False}
        if self.executable_path:
            launch_kwargs["executable_path"] = self.executable_path
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        # Use a fresh context — the user is logging in, so we explicitly
        # start clean rather than re-loading whatever was on disk.
        self._context = self._browser.new_context(viewport={"width": 1280, "height": 900})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    # --- main flow ---

    def run_interactive(self, *, timeout_s: int = _DEFAULT_LOGIN_TIMEOUT_S) -> AuthBundle:
        """Open a window at the workspace URL, poll until ``boot_data`` shows up.

        Most workspaces follow this flow:

          1. Slack login screen → email or SSO
          2. SSO redirect (Okta / Azure / ...) and back
          3. Workspace lands at ``<workspace>/messages/...`` and the web
             client initialises ``window.boot_data``

        We just keep polling ``boot_data`` until it has an ``api_token``
        (or the user gives up and we time out).
        """
        if self._context is None:
            raise RuntimeError("LoginSession must be used as a context manager")
        page = self._context.new_page()
        page.goto(self.workspace_url, wait_until="domcontentloaded", timeout=120_000)
        sys.stderr.write(
            f"[slackwright] login: window opened at {self.workspace_url}\n"
            f"  Complete the login flow in the browser. We'll detect the\n"
            f"  authenticated session automatically (timeout {timeout_s}s).\n"
        )
        deadline = time.time() + timeout_s
        last_url = ""
        while time.time() < deadline:
            try:
                cur = page.url
                if cur != last_url:
                    sys.stderr.write(f"  [slackwright] page: {cur}\n")
                    last_url = cur
                data = page.evaluate(EXTRACT_BOOT_DATA_JS)
            except Exception:
                data = None
            if data and data.get("api_token"):
                bundle = self._materialize_bundle(data)
                self.bundle = bundle
                save_auth(self.state_dir, bundle, context=self._context)
                sys.stderr.write(
                    f"  [slackwright] login: success "
                    f"(user={bundle.user_name or bundle.user_id}, "
                    f"team={bundle.team_id})\n"
                )
                return bundle
            time.sleep(_BOOT_DATA_POLL_INTERVAL_S)
        raise TimeoutError(
            f"slackwright login timed out after {timeout_s}s — no api_token found. "
            f"Re-run `slackwright login` and complete the flow within the timeout."
        )

    def _materialize_bundle(self, data: dict[str, Any]) -> AuthBundle:
        from .paths import storage_state_path

        return AuthBundle(
            workspace_url=self.workspace_url,
            api_url=self.api_url,
            api_token=data["api_token"],
            team_id=data.get("team_id"),
            enterprise_id=data.get("enterprise_id"),
            user_id=data.get("user_id") or "",
            user_name=data.get("username"),
            user_real_name=data.get("real_name"),
            user_email=data.get("email"),
            extracted_at=time.time(),
            storage_state_path=str(storage_state_path(self.state_dir)),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_auth(state_dir: Path, bundle: AuthBundle, *, context: Any | None = None) -> None:
    """Persist the auth bundle and (if we have a Playwright context) its
    storage state. Both files are chmod-600 to keep the bearer-equivalent
    credentials out of casual reach."""
    from .paths import auth_path, storage_state_path

    state_dir.mkdir(parents=True, exist_ok=True)
    if context is not None:
        try:
            context.storage_state(path=str(storage_state_path(state_dir)))
            os.chmod(storage_state_path(state_dir), 0o600)
        except Exception as e:
            sys.stderr.write(
                f"[slackwright] warning: failed to persist Playwright storage "
                f"state ({e}). Subsequent fetches will need to re-login.\n"
            )
    p = auth_path(state_dir)
    p.write_text(json.dumps(bundle.to_json(), indent=2, sort_keys=True))
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)


def load_auth(state_dir: Path) -> AuthBundle:
    """Load the persisted auth bundle, raising :class:`FileNotFoundError`
    with a friendly message when nothing has been logged in yet."""
    from .paths import auth_path

    p = auth_path(state_dir)
    if not p.exists():
        raise FileNotFoundError(
            f"no slackwright login found at {p}. "
            f"Run `slackwright login --workspace <workspace-short-name>` first."
        )
    return AuthBundle.from_json(json.loads(p.read_text()))


def has_storage_state(state_dir: Path) -> bool:
    from .paths import storage_state_path

    return storage_state_path(state_dir).exists()


# ---------------------------------------------------------------------------
# Non-interactive login (--token / --cookie-d)
# ---------------------------------------------------------------------------


def login_non_interactive(
    *,
    workspace_url: str,
    api_token: str,
    cookie_d: str,
    state_dir: Path,
    user_id: str | None = None,
    user_name: str | None = None,
    user_real_name: str | None = None,
    user_email: str | None = None,
    team_id: str | None = None,
    enterprise_id: str | None = None,
) -> AuthBundle:
    """Persist a pre-extracted xoxc token + d cookie as if the user had
    completed the headed login flow.

    Used by CI / automated agents that already hold valid Slack web
    credentials (e.g. extracted from a previous interactive run on a
    sibling machine or from a password-manager export). The headed
    :class:`LoginSession` flow is still the recommended path for
    interactive users — this is purely an automation escape hatch.

    The supplied cookie is written into a Playwright storage-state JSON
    file with the right scope so subsequent ``SlackWebClient.open()``
    calls can replay the session.
    """
    import time as _time

    from .paths import storage_state_path

    if not is_plausible_api_token(api_token):
        raise ValueError(
            f"api_token does not look like a Slack web token "
            f"(expected xoxc-/xoxs-/...; got {api_token[:6]!r}…)"
        )
    if not cookie_d or not cookie_d.startswith("xoxd-"):
        raise ValueError(
            f"cookie_d does not look like a Slack `d` cookie "
            f"(expected xoxd-…; got {cookie_d[:8]!r}…)"
        )

    workspace_url = normalize_workspace_url(workspace_url)
    api_url = workspace_to_api_url(workspace_url)
    parsed_host = urlparse(workspace_url).netloc

    # Slack's `d` cookie is set on the parent .slack.com domain so it
    # rides along on every workspace subdomain. Mirror that here so the
    # session works regardless of which workspace URL we replay against.
    storage_state = {
        "cookies": [
            {
                "name": "d",
                "value": cookie_d,
                "domain": ".slack.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }

    state_dir.mkdir(parents=True, exist_ok=True)
    ssp = storage_state_path(state_dir)
    ssp.write_text(json.dumps(storage_state, indent=2, sort_keys=True))
    with contextlib.suppress(OSError):
        os.chmod(ssp, 0o600)

    bundle = AuthBundle(
        workspace_url=workspace_url,
        api_url=api_url,
        api_token=api_token,
        team_id=team_id,
        enterprise_id=enterprise_id,
        user_id=user_id or "",
        user_name=user_name,
        user_real_name=user_real_name,
        user_email=user_email,
        extracted_at=_time.time(),
        storage_state_path=str(ssp),
    )
    save_auth(state_dir, bundle)
    sys.stderr.write(
        f"[slackwright] login (non-interactive): persisted token + cookie for "
        f"{parsed_host} -> {state_dir}\n"
    )
    return bundle


# ---------------------------------------------------------------------------
# Headless token refresh
# ---------------------------------------------------------------------------


class TokenRefreshError(RuntimeError):
    """Raised when a headless refresh can't produce a fresh xoxc token.

    Distinct from ``TimeoutError`` (interactive login flow) so callers
    can decide whether to fall back to a full re-login or just bubble
    the original ``invalid_auth`` to the user.
    """


_REFRESH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _refresh_diagnostics(page: Any) -> str:
    """Best-effort snapshot of where the headless browser ended up.

    Helps the user understand why ``boot_data`` wasn't there: usually
    Slack redirected to a sign-in page, an org-picker, or a "download
    the app" landing.
    """
    try:
        url = page.url
    except Exception:
        url = "(unknown)"
    try:
        title = page.title()
    except Exception:
        title = "(unknown)"
    try:
        probe = page.evaluate(
            "() => ({"
            "  hasBoot: typeof window.boot_data === 'object',"
            "  bootKeys: window.boot_data ? Object.keys(window.boot_data).slice(0, 8) : [],"
            "  hasLocalConfig: !!localStorage.getItem('localConfig_v2'),"
            "  loginVisible: !!document.querySelector('input[type=email], #email, .p-signin')"
            "})"
        )
    except Exception:
        probe = {}
    return (
        f"  diagnostics: url={url!r} title={title!r} "
        f"hasBoot={probe.get('hasBoot')!r} "
        f"bootKeys={probe.get('bootKeys')!r} "
        f"hasLocalConfig={probe.get('hasLocalConfig')!r} "
        f"loginVisible={probe.get('loginVisible')!r}"
    )


def refresh_token_headless(
    bundle: AuthBundle,
    *,
    state_dir: Path,
    timeout_s: int = 60,
    executable_path: str | None = None,
    verbose: bool = False,
) -> AuthBundle:
    """Mint a fresh ``xoxc`` token from the existing browser session.

    Slack's ``xoxc`` rotates often (~30 min on Enterprise Grid). The
    cookies stay valid much longer, so we can replay them in a
    short-lived headless Chromium, navigate to the workspace, read
    ``window.boot_data.api_token`` again, and persist the new token —
    no user interaction required.

    We launch with Chromium's *new* headless mode (``--headless=new``)
    and a non-Headless UA so Slack doesn't redirect us to its
    download-the-app landing page (the old headless mode trips
    standard "is this a bot?" heuristics on Enterprise Grid).

    Raises :class:`TokenRefreshError` if Chromium loads but
    ``boot_data`` is missing within ``timeout_s`` (cookies fully
    expired → full re-login needed) or the storage state isn't on
    disk.
    """
    from playwright.sync_api import sync_playwright

    from .paths import storage_state_path

    ssp = storage_state_path(state_dir)
    if not ssp.exists():
        raise TokenRefreshError(f"missing storage state at {ssp}; run `slackwright login`")

    with sync_playwright() as pw:
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            # The new headless mode shares the production rendering
            # pipeline with regular Chromium and doesn't advertise
            # ``HeadlessChrome`` in the UA. Falling back to old headless
            # if the bundled Chromium is older than 119 is fine — the
            # extra arg is silently ignored.
            "args": ["--headless=new"],
        }
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        browser = pw.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(
                storage_state=str(ssp),
                viewport={"width": 1280, "height": 900},
                user_agent=_REFRESH_UA,
            )
            # Mask navigator.webdriver — Slack's web client checks it
            # in some flows and silently degrades when it's true.
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
            )
            try:
                page = context.new_page()
                try:
                    page.goto(
                        bundle.workspace_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_s * 1000,
                    )
                except Exception as e:
                    raise TokenRefreshError(
                        f"could not load {bundle.workspace_url} for token refresh: {e}"
                    ) from e
                deadline = time.time() + timeout_s
                data: dict[str, Any] | None = None
                while time.time() < deadline:
                    try:
                        data = page.evaluate(EXTRACT_BOOT_DATA_JS)
                    except Exception:
                        data = None
                    if data and data.get("api_token"):
                        break
                    time.sleep(0.5)
                if not data or not data.get("api_token"):
                    diag = _refresh_diagnostics(page) if verbose else ""
                    raise TokenRefreshError(
                        "boot_data.api_token not present — session cookies likely expired. "
                        "Run `slackwright login` to re-authenticate."
                        + (f"\n{diag}" if diag else "")
                    )
                # Persist the rotated cookies that Slack set during the
                # navigation back to the storage-state file. Failure here
                # isn't fatal — the next run still works with the old jar.
                with contextlib.suppress(Exception):
                    context.storage_state(path=str(ssp))
                    os.chmod(ssp, 0o600)
            finally:
                context.close()
        finally:
            browser.close()

    new_bundle = dataclasses.replace(
        bundle,
        api_token=data["api_token"],
        team_id=data.get("team_id") or bundle.team_id,
        enterprise_id=data.get("enterprise_id") or bundle.enterprise_id,
        user_id=data.get("user_id") or bundle.user_id,
        user_name=data.get("username") or bundle.user_name,
        user_real_name=data.get("real_name") or bundle.user_real_name,
        user_email=data.get("email") or bundle.user_email,
        extracted_at=time.time(),
    )
    save_auth(state_dir, new_bundle)
    return new_bundle


# ---------------------------------------------------------------------------
# Validation helpers (small, pure — covered by unit tests)
# ---------------------------------------------------------------------------


_XOXC_RE = re.compile(r"^xox[cspbarpe]-")


def is_plausible_api_token(token: str) -> bool:
    """Sanity-check: real Slack web tokens start with ``xox<letter>-``."""
    return bool(token) and bool(_XOXC_RE.match(token))
