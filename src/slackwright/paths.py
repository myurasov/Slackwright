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

"""State-directory and cache-path resolution.

The default state dir is ``~/.cache/slackwright/`` (override via ``--state-dir``
or ``SLACKWRIGHT_STATE_DIR``). It contains:

  - ``playwright-state.json`` — Playwright storage state (cookies + localStorage)
  - ``auth.json``             — extracted xoxc token bundle + workspace metadata
  - ``users.json``            — cache of resolved user lookups (id -> profile)
  - ``channels.json``         — cache of resolved channel lookups (id -> meta)
  - ``handle-index.json``     — handle / email / name -> id reverse index
  - ``browsers/``             — Playwright browser data dir (separate from
                                 the system Chromium so we don't clash)
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / ".cache" / "slackwright"

ENV_STATE_DIR = "SLACKWRIGHT_STATE_DIR"


def resolve_state_dir(cli_value: str | None = None) -> Path:
    """Pick the state dir from (precedence: ``--state-dir``, env, default)."""
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    env = os.environ.get(ENV_STATE_DIR)
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_STATE_DIR


def ensure(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def storage_state_path(state_dir: Path) -> Path:
    return state_dir / "playwright-state.json"


def auth_path(state_dir: Path) -> Path:
    return state_dir / "auth.json"


def users_cache_path(state_dir: Path) -> Path:
    return state_dir / "users.json"


def channels_cache_path(state_dir: Path) -> Path:
    return state_dir / "channels.json"


def handle_index_path(state_dir: Path) -> Path:
    return state_dir / "handle-index.json"


def browsers_dir(state_dir: Path) -> Path:
    return state_dir / "browsers"
