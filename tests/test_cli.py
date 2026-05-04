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

"""CLI parsing tests — make sure flags wire up the way the docs claim.

These tests don't invoke the actual subcommand handlers (which need a live
Playwright session). They just exercise ``_build_parser`` to catch regressions
in the user-visible surface.
"""

from __future__ import annotations

import pytest

from slackwright.cli import _build_parser


@pytest.fixture
def parser():
    return _build_parser()


class TestLogin:
    def test_requires_workspace(self, parser) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["login"])

    def test_workspace_short(self, parser) -> None:
        ns = parser.parse_args(["login", "--workspace", "acme"])
        assert ns.cmd == "login"
        assert ns.workspace == "acme"

    def test_default_timeout(self, parser) -> None:
        ns = parser.parse_args(["login", "--workspace", "acme"])
        assert ns.timeout == 300


class TestFetch:
    def test_minimal(self, parser) -> None:
        ns = parser.parse_args(["fetch"])
        assert ns.cmd == "fetch"
        assert ns.from_user is None
        assert ns.to_user is None
        assert ns.headed is False
        assert ns.format == "archive"
        assert ns.out == "./slackwright-out"
        assert ns.with_files is False

    def test_from_to_with_in_query(self, parser) -> None:
        ns = parser.parse_args([
            "fetch",
            "--from", "alice@example.com",
            "--to", "bob",
            "--with", "UCARLA01",
            "--in", "#general",
            "--query", "kubernetes has:link",
        ])
        assert ns.from_user == "alice@example.com"
        assert ns.to_user == "bob"
        assert ns.with_user == "UCARLA01"
        assert ns.in_channel == "#general"
        assert ns.extra_query == "kubernetes has:link"

    def test_days_flag(self, parser) -> None:
        ns = parser.parse_args(["fetch", "--days", "30"])
        assert ns.days == 30

    def test_since_until(self, parser) -> None:
        ns = parser.parse_args(["fetch", "--since", "2026-04-01", "--until", "2026-04-30"])
        assert ns.since == "2026-04-01"
        assert ns.until == "2026-04-30"

    def test_max_and_with_files(self, parser) -> None:
        ns = parser.parse_args(["fetch", "--max", "500", "--with-files"])
        assert ns.max_results == 500
        assert ns.with_files is True

    def test_headed_overrides_default(self, parser) -> None:
        ns = parser.parse_args(["fetch", "--headed"])
        assert ns.headed is True

    def test_headless_explicit(self, parser) -> None:
        ns = parser.parse_args(["fetch", "--headless"])
        assert ns.headed is False

    def test_format_choices(self, parser) -> None:
        ns = parser.parse_args(["fetch", "--format", "jsonl"])
        assert ns.format == "jsonl"

    def test_format_invalid_rejected(self, parser) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["fetch", "--format", "html"])

    def test_dry_run(self, parser) -> None:
        ns = parser.parse_args(["fetch", "--dry-run", "--from", "alice"])
        assert ns.dry_run is True


class TestResolve:
    def test_default_kind_auto(self, parser) -> None:
        ns = parser.parse_args(["resolve", "alice"])
        assert ns.value == "alice"
        assert ns.kind == "auto"

    def test_kind_user(self, parser) -> None:
        ns = parser.parse_args(["resolve", "general", "--kind", "channel"])
        assert ns.kind == "channel"


class TestStateDir:
    def test_global_state_dir_flag(self, parser) -> None:
        ns = parser.parse_args(["--state-dir", "/tmp/sr-state", "whoami"])
        assert ns.state_dir == "/tmp/sr-state"
