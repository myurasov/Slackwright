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

"""slackwright — browser-driven Slack message extractor.

Uses Playwright to drive a logged-in Slack web session and calls Slack's
internal web-search API directly. Bypasses the bot-scope and exclusion-
list limits that constrain official Slack apps and MCP integrations.

Public Python surface (stable for 0.x; minor refactors expected before
1.0). Anything in the modules that's not re-exported here is internal
and may change without notice.

CLI entry-point: see :mod:`slackwright.cli` (``slackwright`` script).
JSON schema of the CLI: ``slackwright --schema``.
Result envelope + exit codes: see :mod:`slackwright.result`.
"""

from __future__ import annotations

__version__ = "0.3.0"

# Auth
# Output writer + post-fetch helpers
from .archive import (
    ARCHIVE_SCHEMA,
    ArchiveWriter,
    WriteStats,
    message_filepath,
    message_key_hash,
    previously_completed_chunks,
    read_index,
)
from .auth import (
    AuthBundle,
    LoginSession,
    has_storage_state,
    is_plausible_api_token,
    load_auth,
    login_non_interactive,
    normalize_workspace_url,
    save_auth,
    workspace_to_api_url,
)

# HTTP client + cost metering
from .client import SlackWebClient, SlackWebError
from .cost import CostTracker

# Attachment downloader
from .files import FileDownloader, FileDownloaderStats, FileDownloadResult

# State-dir locking
from .lock import LockTimeoutError, StateLock

# HTML report
from .report import render_report

# Entity resolution
from .resolver import (
    ChannelRecord,
    EntityResolver,
    ResolvedChannel,
    ResolvedUser,
    UserRecord,
)

# CLI Result envelope + exit codes
from .result import ExitCode, Result, exit_code_table

# Search planning + execution
from .search import (
    SEARCH_MAX_PAGE,
    SEARCH_MAX_RESULTS,
    SEARCH_PER_PAGE,
    SearchPlan,
    SearchRunner,
    SearchStats,
    SearchTimeoutError,
    build_query,
    chunk_label,
    days_back,
    month_chunks,
    parse_date,
)

__all__ = [
    "__version__",
    # auth
    "AuthBundle",
    "LoginSession",
    "has_storage_state",
    "is_plausible_api_token",
    "load_auth",
    "login_non_interactive",
    "normalize_workspace_url",
    "save_auth",
    "workspace_to_api_url",
    # client + cost
    "SlackWebClient",
    "SlackWebError",
    "CostTracker",
    # resolver
    "ChannelRecord",
    "EntityResolver",
    "ResolvedChannel",
    "ResolvedUser",
    "UserRecord",
    # search
    "SEARCH_MAX_PAGE",
    "SEARCH_MAX_RESULTS",
    "SEARCH_PER_PAGE",
    "SearchPlan",
    "SearchRunner",
    "SearchStats",
    "SearchTimeoutError",
    "build_query",
    "chunk_label",
    "days_back",
    "month_chunks",
    "parse_date",
    # archive
    "ARCHIVE_SCHEMA",
    "ArchiveWriter",
    "WriteStats",
    "message_filepath",
    "message_key_hash",
    "previously_completed_chunks",
    "read_index",
    # files
    "FileDownloader",
    "FileDownloadResult",
    "FileDownloaderStats",
    # lock
    "LockTimeoutError",
    "StateLock",
    # CLI envelope
    "ExitCode",
    "Result",
    "exit_code_table",
    # report
    "render_report",
]
