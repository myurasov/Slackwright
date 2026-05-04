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

"""Tiny stderr progress helper — keeps the fetch-loop visible without
pulling in tqdm or rich.

Usage::

    p = Progress(label="fetch")
    p.start()
    p.note("chunk 1/3 starting")
    for ...:
        p.tick(matches=42)
    p.stop()
"""

from __future__ import annotations

import sys
import time


class Progress:
    SPINNER = "|/-\\"

    def __init__(self, *, label: str = "slackwright", verbose: bool = False) -> None:
        self.label = label
        self.verbose = verbose
        self._t0 = time.time()
        self._matches = 0
        self._files = 0
        self._spinner_idx = 0
        self._is_tty = sys.stderr.isatty()

    def start(self) -> None:
        self._t0 = time.time()

    def note(self, msg: str) -> None:
        sys.stderr.write(f"[{self.label}] {msg}\n")
        sys.stderr.flush()

    def vnote(self, msg: str) -> None:
        if self.verbose:
            self.note(msg)

    def tick(self, *, matches: int = 0, files: int = 0) -> None:
        self._matches += matches
        self._files += files
        if self._is_tty:
            spin = self.SPINNER[self._spinner_idx % len(self.SPINNER)]
            self._spinner_idx += 1
            elapsed = int(time.time() - self._t0)
            sys.stderr.write(
                f"\r[{self.label}] {spin}  "
                f"matches={self._matches:,}  "
                f"files={self._files:,}  "
                f"({elapsed}s)\033[K"
            )
            sys.stderr.flush()

    def stop(self) -> None:
        if self._is_tty:
            sys.stderr.write("\r\033[K")
        elapsed = int(time.time() - self._t0)
        sys.stderr.write(
            f"[{self.label}] done — matches={self._matches:,}, "
            f"files={self._files:,}, elapsed={elapsed}s\n"
        )
        sys.stderr.flush()
