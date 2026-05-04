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

"""Tests for the small pure helpers in :mod:`slackwright.auth`."""

from __future__ import annotations

import json
from pathlib import Path

from slackwright.auth import (
    AuthBundle,
    is_plausible_api_token,
    load_auth,
    normalize_workspace_url,
    save_auth,
    workspace_to_api_url,
)


class TestNormalizeWorkspaceUrl:
    def test_short_name(self) -> None:
        assert normalize_workspace_url("acme") == "https://acme.slack.com"

    def test_dotted_name(self) -> None:
        assert normalize_workspace_url("acme.slack.com") == "https://acme.slack.com"

    def test_full_url(self) -> None:
        assert normalize_workspace_url("https://acme.slack.com/") == "https://acme.slack.com"

    def test_enterprise_grid_full_url(self) -> None:
        assert (
            normalize_workspace_url("https://acme.enterprise.slack.com")
            == "https://acme.enterprise.slack.com"
        )

    def test_strips_trailing_slash(self) -> None:
        assert (
            normalize_workspace_url("https://acme.slack.com///")
            == "https://acme.slack.com"
        )


class TestWorkspaceToApiUrl:
    def test_appends_api(self) -> None:
        assert workspace_to_api_url("https://acme.slack.com") == "https://acme.slack.com/api"

    def test_handles_enterprise(self) -> None:
        assert (
            workspace_to_api_url("https://acme.enterprise.slack.com")
            == "https://acme.enterprise.slack.com/api"
        )


class TestIsPlausibleApiToken:
    def test_xoxc(self) -> None:
        assert is_plausible_api_token("xoxc-1234")

    def test_xoxs(self) -> None:
        assert is_plausible_api_token("xoxs-1234")

    def test_random(self) -> None:
        assert not is_plausible_api_token("not-a-token")

    def test_empty(self) -> None:
        assert not is_plausible_api_token("")


class TestSaveAndLoadAuth:
    def test_round_trip(self, state_dir: Path, auth_bundle: AuthBundle) -> None:
        save_auth(state_dir, auth_bundle)
        loaded = load_auth(state_dir)
        assert loaded.api_token == auth_bundle.api_token
        assert loaded.user_id == auth_bundle.user_id
        assert loaded.team_id == auth_bundle.team_id

    def test_load_missing_raises(self, state_dir: Path) -> None:
        import pytest

        with pytest.raises(FileNotFoundError):
            load_auth(state_dir)

    def test_persisted_payload_is_json(self, state_dir: Path, auth_bundle: AuthBundle) -> None:
        save_auth(state_dir, auth_bundle)
        d = json.loads((state_dir / "auth.json").read_text())
        assert d["user_email"] == "alice@example.com"
        assert "api_token" in d  # token is stored verbatim
