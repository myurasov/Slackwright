# Copyright 2026 Mikhail Yurasov
# Licensed under the Apache License, Version 2.0
"""Minimal in-process fetch using the slackwright Python API.

Equivalent CLI:

    slackwright fetch --from me --days 14 --out ./out

Run:

    .venv/bin/python examples/01_fetch_in_process.py

Requires: a prior `slackwright login` so ~/.cache/slackwright/ has a
valid auth bundle.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from slackwright import (
    ArchiveWriter,
    CostTracker,
    EntityResolver,
    SearchPlan,
    SearchRunner,
    SlackWebClient,
    days_back,
    load_auth,
)
from slackwright.paths import resolve_state_dir


def main() -> int:
    state_dir = resolve_state_dir()
    bundle = load_auth(state_dir)
    out = Path("./out").resolve()
    out.mkdir(parents=True, exist_ok=True)

    cost = CostTracker()
    with SlackWebClient.open(bundle, state_dir=state_dir, headed=False, cost=cost) as client:
        resolver = EntityResolver(client, state_dir=state_dir)
        plan = SearchPlan(
            from_user=resolver.resolve_user("me"),
            date_from=days_back(14),
            date_to=dt.date.today(),
        )
        runner = SearchRunner(client, resolver)
        writer = ArchiveWriter(
            out, resolver=resolver, sa_user_id=bundle.user_id,
            format="archive", plan_summary=plan.display(),
        )

        n = 0
        for msg in runner.iter_matches(plan):
            writer.write_match(msg)
            n += 1

        # Resolve every id we saw + persist user/channel YAML caches.
        resolver.resolve_users_in(writer.stats.user_ids_seen)
        resolver.resolve_channels_in(writer.stats.channel_ids_seen)
        resolver.save_caches()
        writer.write_users_cache(resolver)
        writer.write_channels_cache(resolver)
        cost.finalise()
        writer.write_index(
            plan_summary=plan.display(),
            search_query=str(plan),
            cost=cost.to_json(),
        )

    print(f"wrote {n} messages to {out}")
    print(f"cost: {cost.to_json()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
