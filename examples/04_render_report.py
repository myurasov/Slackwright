# Copyright 2026 Mikhail Yurasov
# Licensed under the Apache License, Version 2.0
"""Render a self-contained HTML report from an existing slackwright archive.

Equivalent CLI:

    slackwright report ./out --out ./report.html

Run:

    .venv/bin/python examples/04_render_report.py ./out
"""

from __future__ import annotations

import sys
from pathlib import Path

from slackwright import render_report


def main(out_dir: str) -> int:
    target = render_report(Path(out_dir).resolve())
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: 04_render_report.py <slackwright-output-dir>\n")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
