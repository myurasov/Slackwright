# Copyright 2026 Mikhail Yurasov
# Licensed under the Apache License, Version 2.0
"""Resolve a name / email / Slack id to a UserRecord.

Equivalent CLI:

    slackwright resolve alice@example.com

Run:

    .venv/bin/python examples/02_resolve_only.py alice@example.com
"""

from __future__ import annotations

import json
import sys

from slackwright import EntityResolver, SlackWebClient, load_auth
from slackwright.paths import resolve_state_dir


def main(value: str) -> int:
    state_dir = resolve_state_dir()
    bundle = load_auth(state_dir)
    with SlackWebClient.open(bundle, state_dir=state_dir, headed=False) as client:
        resolver = EntityResolver(client, state_dir=state_dir)
        user = resolver.resolve_user(value)
        resolver.save_caches()
    print(json.dumps(user.record.to_json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: 02_resolve_only.py <name|email|U-id|me>\n")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
