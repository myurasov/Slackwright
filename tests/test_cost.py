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

"""Tests for :mod:`slackwright.cost` — CostTracker observability."""

from __future__ import annotations

from slackwright.cost import CostTracker


class TestCostTracker:
    def test_zeros_at_start(self) -> None:
        c = CostTracker()
        d = c.to_json()
        assert d["api_calls"] == 0
        assert d["bytes_in"] == 0
        assert d["bytes_out"] == 0
        assert d["retries"] == 0
        assert d["rate_limited_seconds"] == 0
        assert d["transport_errors"] == 0
        assert d["api_errors"] == 0
        assert d["api_calls_by_method"] == {}

    def test_record_api_call_aggregates_by_method(self) -> None:
        c = CostTracker()
        c.record_api_call("users.info", bytes_in=100, bytes_out=20)
        c.record_api_call("users.info", bytes_in=200, bytes_out=20)
        c.record_api_call("conversations.list", bytes_in=500, bytes_out=30)
        d = c.to_json()
        assert d["api_calls"] == 3
        assert d["api_calls_by_method"]["users.info"] == 2
        assert d["api_calls_by_method"]["conversations.list"] == 1
        assert d["bytes_in"] == 800
        assert d["bytes_out"] == 70

    def test_record_file_download(self) -> None:
        c = CostTracker()
        c.record_file_download(bytes_in=4096)
        c.record_file_download(bytes_in=8192)
        d = c.to_json()
        assert d["file_downloads"] == 2
        assert d["bytes_in"] == 12288

    def test_retries_and_rate_limits(self) -> None:
        c = CostTracker()
        c.record_retry()
        c.record_retry()
        c.record_rate_limit_sleep(30)
        c.record_rate_limit_sleep(60)
        d = c.to_json()
        assert d["retries"] == 2
        assert d["rate_limited_seconds"] == 90

    def test_transport_and_api_errors(self) -> None:
        c = CostTracker()
        c.record_transport_error()
        c.record_api_error()
        c.record_api_error()
        d = c.to_json()
        assert d["transport_errors"] == 1
        assert d["api_errors"] == 2

    def test_elapsed_ms_is_monotonic(self) -> None:
        import time as _t
        c = CostTracker()
        _t.sleep(0.01)
        a = c.elapsed_ms_now
        _t.sleep(0.01)
        b = c.elapsed_ms_now
        assert b >= a >= 0

    def test_finalise_freezes_elapsed(self) -> None:
        import time as _t
        c = CostTracker()
        _t.sleep(0.01)
        c.finalise()
        a = c.elapsed_ms
        _t.sleep(0.05)
        b = c.elapsed_ms
        assert a == b
