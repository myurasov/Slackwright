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
internal web-search API directly. Bypasses the bot-scope and exclusion-list
limits that constrain official Slack apps and MCP integrations.

Public surface:
  - slackwright.cli   — argparse CLI entry-point (``slackwright``)
  - slackwright.client.SlackWebClient
  - slackwright.resolver.EntityResolver
  - slackwright.search.SearchPlan, SearchRunner, build_query
  - slackwright.archive.ArchiveWriter
  - slackwright.auth.LoginSession, AuthBundle, save_auth, load_auth

Stable for 0.x but expect minor refactors before 1.0.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
