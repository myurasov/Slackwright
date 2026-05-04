# slackwright Python embedding examples

These scripts illustrate the **stable Python API** that slackwright
exports from `slackwright.__init__`. Anything imported here is part of
the public surface and will not break across 0.x patch releases.

For agents (LangChain, dspy, custom orchestrators, MCP wrappers) that
already run inside a Python process, embedding directly skips the
subprocess + JSON-parse round-trip and gives you typed access to the
result objects.

To run any example:

```bash
# from the repo root, after a one-time `./slackwright install`
.venv/bin/python examples/01_fetch_in_process.py
```

The examples assume you've already run `./slackwright login --workspace
https://your-workspace.slack.com` interactively at least once, so the
state directory has a valid auth bundle.

## Files

- [`01_fetch_in_process.py`](01_fetch_in_process.py) — minimal end-to-end
  fetch: open a client, build a SearchPlan, stream messages into an
  ArchiveWriter. Mirrors what `slackwright fetch` does internally.
- [`02_resolve_only.py`](02_resolve_only.py) — resolve a name / email /
  Slack id to a fully populated UserRecord. Useful if you only need
  identity lookups.
- [`03_explain_no_network.py`](03_explain_no_network.py) — build a
  SearchPlan and the chunk schedule entirely from cached data, no
  Playwright spawn. The same fast-path the CLI's `--explain` uses.
- [`04_render_report.py`](04_render_report.py) — point the HTML
  renderer at an existing output directory and produce `report.html`.
