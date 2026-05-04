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

"""Tests for :mod:`slackwright.schema` — CLI introspection."""

from __future__ import annotations

import json

from slackwright import __version__
from slackwright.cli import _build_parser
from slackwright.schema import describe_parser


class TestDescribeParser:
    def test_top_level_shape(self) -> None:
        s = describe_parser(_build_parser())
        assert s["tool"] == "slackwright"
        assert s["version"] == __version__
        assert "envelope" in s
        assert "exit_codes" in s
        assert "global_options" in s
        assert "subcommands" in s

    def test_envelope_documents_fields(self) -> None:
        s = describe_parser(_build_parser())
        fields = s["envelope"]["fields"]
        assert "ok" in fields
        assert "exit_code" in fields
        assert "command" in fields
        assert "data" in fields

    def test_subcommands_present(self) -> None:
        s = describe_parser(_build_parser())
        subs = s["subcommands"]
        for name in ("login", "whoami", "fetch", "resolve", "doctor",
                     "describe-archive", "report"):
            assert name in subs, f"missing subcommand {name!r}"

    def test_fetch_options_include_new_flags(self) -> None:
        s = describe_parser(_build_parser())
        fetch = s["subcommands"]["fetch"]
        opt_names = {o["dest"] for o in fetch["options"]}
        for new in ("explain", "resume", "stream_json", "timeout",
                     "with_files", "format", "max_results"):
            assert new in opt_names, f"missing fetch flag {new!r}"

    def test_login_options_include_non_interactive(self) -> None:
        s = describe_parser(_build_parser())
        login = s["subcommands"]["login"]
        dests = {o["dest"] for o in login["options"]}
        assert {"api_token", "cookie_d", "user_id", "user_email", "team_id"} <= dests

    def test_global_options_include_json_quiet_schema(self) -> None:
        s = describe_parser(_build_parser())
        dests = {o["dest"] for o in s["global_options"]}
        assert {"json", "quiet", "schema", "state_dir"} <= dests

    def test_serialises_to_json(self) -> None:
        s = describe_parser(_build_parser())
        # Round-trip: must be valid JSON (no enums, no Path objects, etc.)
        text = json.dumps(s)
        round_tripped = json.loads(text)
        assert round_tripped["tool"] == "slackwright"
