# Copyright 2026 Mikhail Yurasov
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the Chrome cookie extractor — covers the wire format we
need to match (PBKDF2 params, AES-128-CBC, ``v10`` prefix, PKCS#7
padding) and the SQLite-to-Playwright translation."""

from __future__ import annotations

import sqlite3
import sys

import pytest

if sys.platform != "darwin":
    pytest.skip("Chrome cookie extraction is only implemented on macOS", allow_module_level=True)

from slackwright.chrome_cookies import (
    _CHROME_AES_KEY_LENGTH,
    _CHROME_IV,
    _CHROME_PBKDF2_ITERATIONS,
    _CHROME_SALT,
    _decrypt_cookie,
    _derive_chrome_key,
    extract_chrome_cookies,
)


def _encrypt_v10(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt ``plaintext`` the way Chrome does, prefixed with ``v10``."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad] * pad)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(_CHROME_IV)).encryptor()
    return b"v10" + encryptor.update(padded) + encryptor.finalize()


class TestKeyDerivation:
    def test_pbkdf2_params_match_chrome(self) -> None:
        # PBKDF2-HMAC-SHA1 with Chrome's hard-coded salt + iterations
        # is the contract we have to match — change either side and
        # decryption silently produces garbage.
        key = _derive_chrome_key(b"swordfish")
        assert len(key) == _CHROME_AES_KEY_LENGTH
        assert _CHROME_SALT == b"saltysalt"
        assert _CHROME_PBKDF2_ITERATIONS == 1003


class TestDecryption:
    def test_round_trip_v10(self) -> None:
        key = _derive_chrome_key(b"hunter2")
        ct = _encrypt_v10(b"xoxd-test-cookie-value", key)
        assert _decrypt_cookie(ct, key) == "xoxd-test-cookie-value"

    def test_unknown_format_returns_none(self) -> None:
        key = _derive_chrome_key(b"hunter2")
        # ``v20`` is Chrome 130+ Windows — different scheme entirely.
        assert _decrypt_cookie(b"v20" + b"\x00" * 32, key) is None

    def test_empty_returns_none(self) -> None:
        key = _derive_chrome_key(b"hunter2")
        assert _decrypt_cookie(b"", key) is None
        assert _decrypt_cookie(None, key) is None


class TestExtraction:
    """End-to-end: build a Cookies SQLite the way Chrome does, decrypt
    it via the public ``extract_chrome_cookies`` entry point, and check
    the resulting Playwright-shaped cookie objects."""

    def test_reads_decrypts_and_filters(self, tmp_path, monkeypatch) -> None:
        from slackwright import chrome_cookies as cc

        # Stub out the keychain read with a known password so the test
        # doesn't depend on the developer's macOS Keychain.
        password = b"swordfish"
        monkeypatch.setattr(cc, "_read_keychain_password", lambda: password)
        key = _derive_chrome_key(password)

        profile_dir = tmp_path / "user-data" / "Default"
        profile_dir.mkdir(parents=True)
        db = profile_dir / "Cookies"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE cookies (name TEXT, value TEXT, host_key TEXT, path TEXT, "
            "expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER, "
            "samesite INTEGER, encrypted_value BLOB, has_expires INTEGER)"
        )
        rows = [
            # Slack's `d` cookie — encrypted, needs decryption
            (
                "d",
                "",
                ".slack.com",
                "/",
                13_000_000_000_000_000,  # ~ year 2012 in Chrome time
                1,
                1,
                1,
                _encrypt_v10(b"xoxd-FAKE-FOR-TEST", key),
                1,
            ),
            # An unrelated tracker cookie (should be filtered out)
            (
                "tracker",
                "",
                ".doubleclick.net",
                "/",
                0,
                1,
                0,
                2,
                _encrypt_v10(b"opaque-id", key),
                0,
            ),
            # Plaintext value path (rare but supported)
            ("legacy", "plain", ".slack.com", "/", 0, 0, 0, 1, b"", 0),
        ]
        conn.executemany(
            "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()

        out = extract_chrome_cookies(
            tmp_path / "user-data", "Default", domain_filter="slack.com"
        )
        names = sorted(c["name"] for c in out)
        assert names == ["d", "legacy"]
        d = next(c for c in out if c["name"] == "d")
        assert d["value"] == "xoxd-FAKE-FOR-TEST"
        assert d["domain"] == ".slack.com"
        assert d["httpOnly"] is True
        assert d["secure"] is True
        # has_expires=1, expires_utc set → converted to a Unix timestamp
        assert isinstance(d["expires"], (int, float))
        assert d["expires"] > 0
        # has_expires=0 on `legacy` → -1 (session cookie sentinel)
        legacy = next(c for c in out if c["name"] == "legacy")
        assert legacy["value"] == "plain"
        assert legacy["expires"] == -1

    def test_uses_network_cookies_when_present(self, tmp_path, monkeypatch) -> None:
        """Chrome ≥ ~96 stores cookies under ``Network/Cookies``;
        the extractor should prefer that over the legacy root path."""
        from slackwright import chrome_cookies as cc

        password = b"hunter2"
        monkeypatch.setattr(cc, "_read_keychain_password", lambda: password)
        key = _derive_chrome_key(password)

        profile_dir = tmp_path / "user-data" / "Default"
        (profile_dir / "Network").mkdir(parents=True)
        # Build BOTH locations — the extractor must pick Network/.
        for path, val in (
            (profile_dir / "Network" / "Cookies", b"new-location"),
            (profile_dir / "Cookies", b"old-location"),
        ):
            conn = sqlite3.connect(str(path))
            conn.execute(
                "CREATE TABLE cookies (name TEXT, value TEXT, host_key TEXT, "
                "path TEXT, expires_utc INTEGER, is_secure INTEGER, "
                "is_httponly INTEGER, samesite INTEGER, encrypted_value BLOB, "
                "has_expires INTEGER)"
            )
            conn.execute(
                "INSERT INTO cookies VALUES "
                "('marker','','.slack.com','/',0,1,1,1,?,0)",
                (_encrypt_v10(val, key),),
            )
            conn.commit()
            conn.close()

        out = extract_chrome_cookies(tmp_path / "user-data", "Default")
        # Prefer the Network/ path
        assert any(c["value"] == "new-location" for c in out)
        assert not any(c["value"] == "old-location" for c in out)

    def test_missing_cookies_db_raises_filenotfounderror(self, tmp_path) -> None:
        (tmp_path / "user-data" / "Default").mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="no Chrome Cookies SQLite"):
            extract_chrome_cookies(tmp_path / "user-data", "Default")
