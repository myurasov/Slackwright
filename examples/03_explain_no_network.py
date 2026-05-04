# Copyright 2026 Mikhail Yurasov
# Licensed under the Apache License, Version 2.0
"""Build a SearchPlan + chunk schedule entirely from the on-disk cache.

No Playwright spawn, no Slack API calls. Only works for inputs that are
already cached (the local resolver hits the on-disk users.json /
channels.json files, plus uses the literal U-id / C-id passes through
without resolution).

Equivalent CLI:

    slackwright fetch --from UALICE00 --in CGENERAL --days 30 --explain --json

Run:

    .venv/bin/python examples/03_explain_no_network.py
"""

from __future__ import annotations

import datetime as dt
import json

from slackwright import (
    EntityResolver,
    SearchPlan,
    build_query,
    chunk_label,
    days_back,
    month_chunks,
)
from slackwright.paths import resolve_state_dir


def main() -> int:
    state_dir = resolve_state_dir()
    resolver = EntityResolver(client=None, state_dir=state_dir)
    plan = SearchPlan(
        from_user=resolver.resolve_user("UALICE00"),     # already a Slack id
        in_channel=resolver.resolve_channel("CGENERAL"),  # already a Slack id
        date_from=days_back(30),
        date_to=dt.date.today(),
    )
    chunks = [
        {"label": chunk_label(a, b),
         "after": a.isoformat(),
         "before": b.isoformat(),
         "query": build_query(plan, after=a, before=b)}
        for a, b in month_chunks(plan.date_from, plan.date_to)
    ]
    print(json.dumps({
        "plan": plan.display(),
        "query": build_query(plan),
        "chunks": chunks,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
