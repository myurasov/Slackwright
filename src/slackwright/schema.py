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

"""Machine-readable description of the slackwright CLI surface.

Built by introspecting the argparse parser tree from :mod:`slackwright.cli`
and rendering it as a stable JSON document. Consumed by
``slackwright --schema``; agents can use it to enumerate subcommands and
flags without parsing ``--help`` text.

The schema also embeds:
  - the documented :class:`~slackwright.result.ExitCode` table
  - the JSON envelope shape (top-level keys ``ok`` / ``command`` /
    ``exit_code`` / ``error`` / ``message`` / ``remediation`` / ``data``)
  - the runtime version

so a single ``slackwright --schema`` call gives an agent everything it
needs to wrap the tool.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import __version__
from .result import exit_code_table


def describe_parser(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Render an argparse parser (top-level + subparsers) as JSON-friendly dict."""
    return {
        "tool": "slackwright",
        "version": __version__,
        "envelope": {
            "stdout": "single JSON document when --json (otherwise human text or empty)",
            "fields": {
                "ok": "bool",
                "command": "str (subcommand name)",
                "exit_code": "int",
                "exit_code_name": "str (lowercase enum name)",
                "error": "str (lowercase snake_case error code; absent on success)",
                "message": "str (human-readable; absent on success)",
                "remediation": "str (recommended next action; absent on success)",
                "data": "dict (subcommand-specific payload; structure documented per-subcommand)",
            },
        },
        "exit_codes": exit_code_table(),
        "global_options": _describe_actions(parser),
        "subcommands": _describe_subparsers(parser),
    }


# ---------------------------------------------------------------------------
# argparse introspection helpers
# ---------------------------------------------------------------------------


def _describe_actions(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for action in parser._actions:                       # noqa: SLF001 (intentional)
        if isinstance(action, argparse._SubParsersAction):
            continue
        if isinstance(action, argparse._HelpAction):
            continue
        out.append(_describe_action(action))
    return out


def _describe_action(action: argparse.Action) -> dict[str, Any]:
    nargs: Any = action.nargs
    if isinstance(nargs, int):
        nargs_repr: str | int | None = nargs
    else:
        nargs_repr = nargs  # ?, *, +, ...
    result: dict[str, Any] = {
        "names": list(action.option_strings) or [action.dest],
        "dest": action.dest,
        "required": bool(action.required),
        "type": _action_type_name(action.type),
        "default": _safe_default(action.default),
        "help": action.help or "",
    }
    if action.choices:
        result["choices"] = list(action.choices)
    if nargs_repr is not None:
        result["nargs"] = nargs_repr
    if isinstance(action, argparse._StoreTrueAction):
        result["type"] = "flag"
    elif isinstance(action, argparse._StoreFalseAction):
        result["type"] = "flag"
        result["inverts"] = True
    elif isinstance(action, argparse._VersionAction):
        result["type"] = "version"
    return result


def _describe_subparsers(parser: argparse.ArgumentParser) -> dict[str, Any]:
    subs: dict[str, Any] = {}
    for action in parser._actions:                       # noqa: SLF001
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            subs[name] = {
                "name": name,
                "help": action.help or "",
                "description": (sub.description or "").strip(),
                "options": _describe_actions(sub),
                "positionals": [
                    _describe_action(a) for a in sub._actions       # noqa: SLF001
                    if not a.option_strings and not isinstance(a, argparse._SubParsersAction)
                ],
            }
    return subs


# ---------------------------------------------------------------------------
# Type-name flattening (so the JSON stays printable)
# ---------------------------------------------------------------------------


def _action_type_name(t: Any) -> str:
    if t is None:
        return "str"
    if hasattr(t, "__name__"):
        return t.__name__
    return str(t)


def _safe_default(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_default(v) for v in value]
    return repr(value)


__all__ = ["describe_parser"]
