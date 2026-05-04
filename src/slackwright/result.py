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

"""Structured CLI result + exit-code surface.

Every subcommand handler in :mod:`slackwright.cli` returns a
:class:`Result`. ``main()`` then renders it either as human-readable
text (default) or as a single JSON document on stdout (``--json``).

The :class:`ExitCode` enum is the documented contract that wrapper
scripts and AI agents can rely on to decide *retry vs. give up*:

  0   ok                    success
  2   usage                 bad CLI invocation (argparse, missing arg, ...)
  3   no_login              no persisted login; run `slackwright login`
  4   resolution_failed     --from / --to / --in didn't resolve
  5   transient_api         retryable Slack error (rate-limit, 5xx, network)
  6   permanent_api         non-retryable Slack error (invalid_auth, ...)
  7   io                    local filesystem error (permission, full disk, ...)
  130 interrupted           SIGINT / Ctrl-C

The numeric codes are stable across versions; the symbolic names live
in this module. Tests should compare to the enum, not the literal int.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import sys
from typing import IO, Any


class ExitCode(enum.IntEnum):
    """Documented exit codes — see module docstring."""

    OK = 0
    USAGE = 2
    NO_LOGIN = 3
    RESOLUTION_FAILED = 4
    TRANSIENT_API = 5
    PERMANENT_API = 6
    IO = 7
    INTERRUPTED = 130


# Map of (error_code, recommended remediation hint). Agents can read the
# `error` field and decide based on the code; the human message is always
# rendered too.
_REMEDIATION: dict[str, str] = {
    "no_login": "Run: slackwright login --workspace https://<your-workspace>.slack.com",
    "resolution_failed": "Re-check --from / --to / --with / --in; pass an email or U-id if the name is ambiguous.",
    "transient_api": "Wait a few minutes and re-run; the Slack API was rate-limited or unavailable.",
    "permanent_api": "Re-login with `slackwright login`; the saved session may be invalid or revoked.",
    "io": "Check the destination directory permissions and free disk space.",
    "usage": "See `slackwright <subcommand> --help` for valid arguments.",
    "interrupted": "Re-run; the prior invocation was cancelled before completion.",
    "unknown": "If the failure persists, open an issue with the stderr output attached.",
}


@dataclasses.dataclass
class Result:
    """Outcome of one CLI subcommand invocation.

    Successful results carry ``data`` (whatever the command produced).
    Failures carry ``error`` (a stable code, lowercase snake_case),
    ``message`` (human-readable), and ``remediation`` (what to do next).

    The JSON envelope is intentionally flat so it's cheap for agents to
    parse: top-level keys ``ok``, ``command``, ``exit_code``, ``error``,
    ``message``, ``remediation``, ``data``.
    """

    command: str
    exit_code: ExitCode = ExitCode.OK
    data: dict[str, Any] | None = None
    error: str | None = None
    message: str | None = None
    remediation: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == ExitCode.OK

    # --- factories -------------------------------------------------------

    @classmethod
    def success(cls, command: str, data: dict[str, Any] | None = None) -> Result:
        return cls(command=command, exit_code=ExitCode.OK, data=data or {})

    @classmethod
    def failure(
        cls,
        command: str,
        exit_code: ExitCode,
        error: str,
        message: str,
        *,
        remediation: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Result:
        if remediation is None:
            remediation = _REMEDIATION.get(error) or _REMEDIATION["unknown"]
        return cls(
            command=command,
            exit_code=exit_code,
            error=error,
            message=message,
            remediation=remediation,
            data=data,
        )

    # --- serialisation ---------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "command": self.command,
            "exit_code": int(self.exit_code),
            "exit_code_name": self.exit_code.name.lower(),
        }
        if self.error is not None:
            out["error"] = self.error
        if self.message is not None:
            out["message"] = self.message
        if self.remediation is not None:
            out["remediation"] = self.remediation
        if self.data is not None:
            out["data"] = self.data
        return out

    # --- rendering -------------------------------------------------------

    def render_json(self, stream: IO[str] | None = None) -> None:
        """Emit the canonical JSON document on stdout (or supplied stream)."""
        s = stream if stream is not None else sys.stdout
        json.dump(self.to_json(), s, indent=2, sort_keys=False, ensure_ascii=False)
        s.write("\n")
        s.flush()

    def render_human(self, *, stdout: IO[str] | None = None, stderr: IO[str] | None = None) -> None:
        """Emit a human-friendly summary.

        On success: caller-provided text on stdout (when ``data`` carries
        a ``human`` key) plus a one-line `[slackwright]` summary on stderr.
        On failure: the error + remediation on stderr; nothing on stdout.
        """
        out = stdout if stdout is not None else sys.stdout
        err = stderr if stderr is not None else sys.stderr
        if self.ok:
            human = (self.data or {}).get("human") if self.data else None
            if isinstance(human, str) and human:
                out.write(human)
                if not human.endswith("\n"):
                    out.write("\n")
                out.flush()
            return
        err.write(f"[slackwright] {self.error}: {self.message}\n")
        if self.remediation:
            err.write(f"[slackwright] hint: {self.remediation}\n")
        err.flush()


def exit_code_table() -> list[dict[str, Any]]:
    """Machine-readable enumeration of every exit code (consumed by
    ``slackwright --schema``)."""
    return [
        {
            "name": ec.name.lower(),
            "value": int(ec),
            "remediation": _REMEDIATION.get(ec.name.lower()),
        }
        for ec in ExitCode
    ]


__all__ = ["ExitCode", "Result", "exit_code_table"]
