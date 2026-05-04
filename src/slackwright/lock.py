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

"""Cross-process file lock for the slackwright state directory.

Two slackwright invocations running in parallel against the same state
directory could race-corrupt the auth bundle, the user/channel caches,
or the Playwright storage state. This module provides a context-manager
file lock that serialises them.

Implementation:
  - On Unix (macOS, Linux): :func:`fcntl.flock` advisory lock on a
    sentinel file inside the state dir. Released automatically on
    process exit even if the process crashes.
  - On Windows: :func:`msvcrt.locking` with byte-range lock; same
    crash-safety guarantee.
  - On unsupported platforms: a noisy noop with a stderr warning. The
    runtime degrades gracefully — single-agent users see no difference.

Usage::

    from slackwright.lock import StateLock
    with StateLock(state_dir).acquire(timeout=60):
        # do mutating work on <state_dir>/auth.json, users.json, ...
        pass
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from typing import IO

_LOCK_FILENAME = ".slackwright.lock"


class LockTimeoutError(RuntimeError):
    """Raised when :meth:`StateLock.acquire` cannot grab the lock in time."""


class StateLock:
    """Process-wide lock on a state directory.

    Use as a context manager; both blocking and non-blocking modes are
    supported via the ``timeout`` argument. ``timeout=0`` returns
    immediately if the lock is held; ``timeout=None`` blocks forever.
    """

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._path = self._dir / _LOCK_FILENAME
        self._fp: IO[bytes] | None = None

    @contextlib.contextmanager
    def acquire(self, *, timeout: float | None = 60.0):
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fp = self._path.open("ab+")
        try:
            self._lock(timeout=timeout)
            yield
        finally:
            self._unlock()
            with contextlib.suppress(Exception):
                self._fp.close()
            self._fp = None

    # --- platform-specific bits -----------------------------------------

    def _lock(self, *, timeout: float | None) -> None:
        if sys.platform == "win32":
            self._lock_windows(timeout=timeout)
        elif sys.platform in {"darwin", "linux", "linux2", "freebsd"} or sys.platform.startswith(
            "linux"
        ):
            self._lock_unix(timeout=timeout)
        else:
            sys.stderr.write(
                f"[slackwright] warning: file locking unsupported on "
                f"{sys.platform!r}; concurrent invocations may race.\n"
            )

    def _lock_unix(self, *, timeout: float | None) -> None:
        import fcntl

        deadline: float | None = None
        if timeout is not None and timeout >= 0:
            deadline = time.monotonic() + float(timeout)

        while True:
            try:
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Write our pid for diagnostic purposes (visible to anyone
                # who `cat`s the file). Best-effort.
                with contextlib.suppress(Exception):
                    self._fp.seek(0)
                    self._fp.truncate()
                    self._fp.write(f"{os.getpid()}\n".encode())
                    self._fp.flush()
                return
            except BlockingIOError:
                pass
            if deadline is None:
                # Block indefinitely — switch to a blocking flock call.
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX)
                return
            if time.monotonic() >= deadline:
                holder = self._read_holder_pid()
                raise LockTimeoutError(
                    f"could not acquire {self._path} within {timeout}s "
                    f"(currently held by PID {holder or 'unknown'})"
                )
            time.sleep(0.1)

    def _lock_windows(self, *, timeout: float | None) -> None:
        import msvcrt

        deadline: float | None = None
        if timeout is not None and timeout >= 0:
            deadline = time.monotonic() + float(timeout)
        while True:
            try:
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"could not acquire {self._path} within {timeout}s"
                    ) from None
                time.sleep(0.1)

    def _unlock(self) -> None:
        if self._fp is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                with contextlib.suppress(Exception):
                    self._fp.seek(0)
                    msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

    def _read_holder_pid(self) -> int | None:
        try:
            txt = self._path.read_text().strip()
            return int(txt) if txt.isdigit() else None
        except Exception:
            return None


__all__ = ["StateLock", "LockTimeoutError"]
