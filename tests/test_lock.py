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

"""Tests for :mod:`slackwright.lock` — state-dir file lock."""

from __future__ import annotations

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

from slackwright.lock import LockTimeoutError, StateLock


class TestStateLock:
    def test_acquire_and_release(self, tmp_path: Path) -> None:
        lock = StateLock(tmp_path)
        with lock.acquire(timeout=5):
            assert (tmp_path / ".slackwright.lock").exists()
        # After release, second acquisition succeeds quickly
        with lock.acquire(timeout=5):
            pass

    def test_creates_state_dir_if_missing(self, tmp_path: Path) -> None:
        d = tmp_path / "fresh"
        with StateLock(d).acquire(timeout=5):
            assert d.exists()

    def test_records_holder_pid(self, tmp_path: Path) -> None:
        import os
        lock = StateLock(tmp_path)
        with lock.acquire(timeout=5):
            txt = (tmp_path / ".slackwright.lock").read_text().strip()
            assert txt == str(os.getpid())

    @pytest.mark.skipif(sys.platform == "win32", reason="multiprocessing flock test is POSIX-specific")
    def test_concurrent_lock_blocks(self, tmp_path: Path) -> None:
        # Spawn a child that holds the lock for ~3s, signalling readiness
        # via a sentinel file so we don't race the spawn warmup time
        # (which is multi-hundred-ms on macOS using spawn).
        ctx = multiprocessing.get_context("spawn")
        ready = tmp_path / ".child-holding"
        proc = ctx.Process(target=_hold_lock_for, args=(str(tmp_path), 3.0, str(ready)))
        proc.start()
        try:
            deadline = time.monotonic() + 8
            while not ready.exists():
                if time.monotonic() > deadline:
                    pytest.fail("child failed to grab the lock within 8s")
                time.sleep(0.05)
            with pytest.raises(LockTimeoutError), StateLock(tmp_path).acquire(timeout=0.2):
                pass
        finally:
            proc.join(timeout=10)
        # After the child exits, we can acquire again.
        with StateLock(tmp_path).acquire(timeout=2):
            pass


def _hold_lock_for(state_dir_str: str, secs: float, ready_path_str: str) -> None:
    with StateLock(Path(state_dir_str)).acquire(timeout=5):
        Path(ready_path_str).write_text("held")
        time.sleep(secs)
