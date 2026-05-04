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

"""Tests for :mod:`slackwright.result` — Result + ExitCode + JSON envelope."""

from __future__ import annotations

import io
import json

from slackwright.result import ExitCode, Result, exit_code_table


class TestExitCode:
    def test_known_values(self) -> None:
        assert int(ExitCode.OK) == 0
        assert int(ExitCode.USAGE) == 2
        assert int(ExitCode.NO_LOGIN) == 3
        assert int(ExitCode.RESOLUTION_FAILED) == 4
        assert int(ExitCode.TRANSIENT_API) == 5
        assert int(ExitCode.PERMANENT_API) == 6
        assert int(ExitCode.IO) == 7
        assert int(ExitCode.INTERRUPTED) == 130

    def test_table_contents(self) -> None:
        rows = exit_code_table()
        names = {row["name"] for row in rows}
        assert {"ok", "usage", "no_login", "resolution_failed", "transient_api",
                "permanent_api", "io", "interrupted"} <= names
        # Every non-OK row carries a remediation hint.
        for row in rows:
            if row["name"] == "ok":
                continue
            assert row["remediation"] is not None, f"missing remediation for {row['name']!r}"


class TestResultSuccess:
    def test_basic_success(self) -> None:
        r = Result.success("whoami", data={"user_id": "UALICE00"})
        assert r.ok is True
        assert r.exit_code == ExitCode.OK
        assert r.error is None

    def test_to_json_success_shape(self) -> None:
        r = Result.success("fetch", data={"counts": {"created": 3}})
        d = r.to_json()
        assert d["ok"] is True
        assert d["exit_code"] == 0
        assert d["exit_code_name"] == "ok"
        assert d["data"] == {"counts": {"created": 3}}
        assert "error" not in d
        assert "message" not in d

    def test_render_json_writes_one_document(self) -> None:
        buf = io.StringIO()
        r = Result.success("doctor", data={"x": 1})
        r.render_json(stream=buf)
        out = buf.getvalue()
        assert out.endswith("\n")
        d = json.loads(out)
        assert d["command"] == "doctor"
        assert d["data"] == {"x": 1}


class TestResultFailure:
    def test_failure_includes_remediation(self) -> None:
        r = Result.failure("fetch", ExitCode.NO_LOGIN, "no_login", "missing")
        assert r.ok is False
        assert r.error == "no_login"
        assert r.remediation is not None
        assert "slackwright login" in r.remediation

    def test_failure_to_json(self) -> None:
        r = Result.failure("fetch", ExitCode.RESOLUTION_FAILED,
                           "resolution_failed", "no match for 'xyz'")
        d = r.to_json()
        assert d["ok"] is False
        assert d["exit_code"] == 4
        assert d["exit_code_name"] == "resolution_failed"
        assert d["error"] == "resolution_failed"
        assert d["message"] == "no match for 'xyz'"
        assert "remediation" in d

    def test_explicit_remediation_overrides_default(self) -> None:
        r = Result.failure("login", ExitCode.PERMANENT_API, "permanent_api",
                           "boom", remediation="Try X instead.")
        assert r.remediation == "Try X instead."

    def test_render_human_writes_to_stderr_on_failure(self) -> None:
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        r = Result.failure("fetch", ExitCode.NO_LOGIN, "no_login", "nope")
        r.render_human(stdout=buf_out, stderr=buf_err)
        assert buf_out.getvalue() == ""
        assert "no_login" in buf_err.getvalue()
        assert "hint:" in buf_err.getvalue()
