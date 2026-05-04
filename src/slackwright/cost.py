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

"""Per-invocation cost / observability tracker.

Counts API calls, bytes transferred, retries, time spent waiting on
rate-limit backoff, and total elapsed wall-clock time. Surfaced in
every command's JSON output (via the ``cost`` block) and in
``_index.yaml`` so agents can budget against it.

Design intent: cheap to mutate from hot paths (incrementing ints), no
locking required because we pin one tracker per :class:`SlackWebClient`
and SlackWebClient itself is single-threaded (sync_playwright).
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any


@dataclasses.dataclass
class CostTracker:
    """Cumulative per-invocation metering of work done against Slack.

    All fields are simple counters; ``elapsed_ms`` is set when
    :meth:`finalise` is called (or fetched fresh via :attr:`elapsed_ms_now`).
    """

    api_calls: int = 0  # total tools/call style requests sent
    api_calls_by_method: dict[str, int] = dataclasses.field(default_factory=dict)
    bytes_in: int = 0  # response bytes received from Slack
    bytes_out: int = 0  # request bytes sent to Slack (best-effort)
    file_downloads: int = 0  # successful Slack file downloads
    retries: int = 0  # transient retries after a backoff
    rate_limited_seconds: int = 0  # cumulative seconds slept on rate-limit
    transport_errors: int = 0  # exceptions raised by the HTTP transport
    api_errors: int = 0  # documented Slack `ok:false` errors

    started_at: float = dataclasses.field(default_factory=time.monotonic)
    finished_at: float | None = None

    # --- mutators (called from hot paths) --------------------------------

    def record_api_call(
        self,
        method: str,
        *,
        bytes_in: int = 0,
        bytes_out: int = 0,
    ) -> None:
        self.api_calls += 1
        self.api_calls_by_method[method] = self.api_calls_by_method.get(method, 0) + 1
        self.bytes_in += int(bytes_in)
        self.bytes_out += int(bytes_out)

    def record_file_download(self, *, bytes_in: int = 0) -> None:
        self.file_downloads += 1
        self.bytes_in += int(bytes_in)

    def record_retry(self) -> None:
        self.retries += 1

    def record_rate_limit_sleep(self, seconds: int) -> None:
        self.rate_limited_seconds += int(seconds)

    def record_transport_error(self) -> None:
        self.transport_errors += 1

    def record_api_error(self) -> None:
        self.api_errors += 1

    # --- lifecycle ------------------------------------------------------

    def finalise(self) -> None:
        if self.finished_at is None:
            self.finished_at = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return int((end - self.started_at) * 1000)

    @property
    def elapsed_ms_now(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    # --- serialisation --------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "elapsed_ms": self.elapsed_ms,
            "api_calls": self.api_calls,
            "api_calls_by_method": dict(self.api_calls_by_method),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "file_downloads": self.file_downloads,
            "retries": self.retries,
            "rate_limited_seconds": self.rate_limited_seconds,
            "transport_errors": self.transport_errors,
            "api_errors": self.api_errors,
        }


__all__ = ["CostTracker"]
