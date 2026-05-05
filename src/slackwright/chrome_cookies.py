# Copyright 2026 Mikhail Yurasov
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Read + decrypt Chrome cookies from disk.

Used by ``slackwright login --use-chrome`` to grab the user's existing
Slack session **without launching Chrome**. Driving Chrome via
Playwright's persistent context on macOS is unworkable: Chrome either
uses the mock keychain (cookies don't decrypt) or the real one (the
process exits before opening a window). Reading cookies straight off
disk dodges the whole keychain dance.

We then write the decrypted cookies into Playwright's storage-state
JSON shape so the regular bundled-Chromium login path can use them.

Currently macOS only — Linux uses libsecret, Windows uses DPAPI.
Platform support is straightforward to add later.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_CHROME_KEYCHAIN_SERVICE = "Chrome Safe Storage"
_CHROME_SALT = b"saltysalt"
_CHROME_IV = b" " * 16
_CHROME_PBKDF2_ITERATIONS = 1003
_CHROME_AES_KEY_LENGTH = 16


def extract_chrome_cookies(
    user_data_dir: Path,
    profile_directory: str = "Default",
    *,
    domain_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Read + decrypt Chrome cookies for a single profile.

    Returns cookies in Playwright's storage-state shape::

        {name, value, domain, path, expires, httpOnly, secure, sameSite}

    ``domain_filter``, when set, restricts to cookies whose ``host_key``
    contains the substring (e.g. pass ``"slack.com"`` to only export
    Slack's session cookies and skip the dozens of unrelated trackers).
    """
    if sys.platform != "darwin":
        raise NotImplementedError(
            f"Chrome cookie extraction is only implemented on macOS; "
            f"got platform={sys.platform!r}."
        )

    profile = user_data_dir / profile_directory
    # Newer Chrome (>= ~96) stores cookies under ``Network/Cookies``;
    # older builds keep them at the profile root. Use whichever exists.
    candidates = [profile / "Network" / "Cookies", profile / "Cookies"]
    db_path = next((p for p in candidates if p.exists()), None)
    if db_path is None:
        raise FileNotFoundError(
            f"no Chrome Cookies SQLite under {profile} "
            f"(looked for: {[str(c) for c in candidates]})"
        )

    password = _read_keychain_password()
    key = _derive_chrome_key(password)

    # Chrome may have the SQLite open + WAL-locked; copy it to a temp.
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(db_path, tmp_path)
        # Chrome 130+ also stores a sidecar journal; copy if present
        # so the temp DB isn't half-state.
        for sidecar_suffix in ("-journal", "-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + sidecar_suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, tmp_path.with_name(tmp_path.name + sidecar_suffix))

        conn = sqlite3.connect(str(tmp_path))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT name, value, host_key, path, expires_utc, is_secure, "
                "is_httponly, samesite, encrypted_value, has_expires "
                "FROM cookies"
            ).fetchall()
        finally:
            conn.close()
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = tmp_path.with_name(tmp_path.name + suffix) if suffix else tmp_path
            if p.exists():
                p.unlink()

    out: list[dict[str, Any]] = []
    for row in rows:
        host = row["host_key"] or ""
        if domain_filter and domain_filter not in host:
            continue
        plain = row["value"] or _decrypt_cookie(row["encrypted_value"], key)
        if plain is None:
            continue
        out.append(
            {
                "name": row["name"],
                "value": plain,
                "domain": host,
                "path": row["path"] or "/",
                "expires": _chrome_time_to_unix(row["expires_utc"]) if row["has_expires"] else -1,
                "httpOnly": bool(row["is_httponly"]),
                "secure": bool(row["is_secure"]),
                "sameSite": _samesite_label(row["samesite"]),
            }
        )
    return out


def write_storage_state(cookies: list[dict[str, Any]], target: Path) -> None:
    """Write a Playwright-compatible storage-state JSON containing only ``cookies``."""
    import json

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"cookies": cookies, "origins": []}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_keychain_password() -> bytes:
    """Pull the ``Chrome Safe Storage`` secret out of the macOS Keychain.

    On the first run macOS pops a "slackwright wants to access Chrome
    Safe Storage" dialog — clicking *Always Allow* avoids the prompt
    on subsequent runs.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", _CHROME_KEYCHAIN_SERVICE],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"could not read '{_CHROME_KEYCHAIN_SERVICE}' from the macOS Keychain "
            f"({(e.stderr or '').strip() or e}). If macOS prompted for keychain "
            f"access, click 'Always Allow' and retry."
        ) from e
    except FileNotFoundError as e:
        raise RuntimeError("the macOS `security` CLI is missing — is this macOS?") from e
    return result.stdout.strip().encode()


def _derive_chrome_key(password: bytes) -> bytes:
    """PBKDF2-HMAC-SHA1 key derivation (Chrome's hard-coded params).

    Salt = ``b"saltysalt"``, iterations = 1003, key length = 16 bytes.
    These are baked into Chromium's ``components/os_crypt`` and have
    been stable across versions.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=_CHROME_AES_KEY_LENGTH,
        salt=_CHROME_SALT,
        iterations=_CHROME_PBKDF2_ITERATIONS,
    ).derive(password)


def _decrypt_cookie(encrypted: bytes | None, key: bytes) -> str | None:
    """Decrypt a Chrome ``encrypted_value`` blob. Returns None for unsupported formats."""
    if not encrypted:
        return None
    # Chrome prefixes the ciphertext with a 3-byte version tag. We
    # handle the two common ones (``v10`` macOS/Windows and ``v11``
    # Linux libsecret); the newer ``v20`` (Chrome 130+ on Windows)
    # uses an entirely different scheme.
    if encrypted[:3] not in (b"v10", b"v11"):
        return None
    ciphertext = encrypted[3:]

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.CBC(_CHROME_IV))
    decryptor = cipher.decryptor()
    try:
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    except ValueError:
        return None
    # PKCS#7 unpad
    if not plaintext:
        return None
    pad = plaintext[-1]
    if pad < 1 or pad > 16 or pad > len(plaintext):
        return None
    plaintext = plaintext[:-pad]

    # Chrome 130+ may prefix decrypted values with a 32-byte SHA-256
    # binding the cookie to its origin. If the first 32 bytes don't
    # decode as printable text, strip them and try again.
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        if len(plaintext) > 32:
            try:
                return plaintext[32:].decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


def _chrome_time_to_unix(chrome_time: int | None) -> float:
    """Chrome stores time as microseconds since 1601-01-01 UTC; convert to Unix seconds."""
    if not chrome_time:
        return -1
    return (chrome_time / 1_000_000) - 11_644_473_600


def _samesite_label(value: int | None) -> str:
    if value == 0:
        return "None"
    if value == 1:
        return "Lax"
    if value == 2:
        return "Strict"
    # ``-1`` (unspecified) / unknown — Lax is the modern default.
    return "Lax"
