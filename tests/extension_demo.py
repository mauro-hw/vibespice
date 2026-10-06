#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
A program built on vibespice, for the tests: it uses the two extension points.

- tools.register_tool(): an extra tool, 'measure_divider', with a line for the system
  prompt; what it simulates counts for the origin check.
- agent.run_once(review=...): the program checks the final answer and sends it back once.

run_tests.py runs it against the fake server (scenario 'extension') on challenge 0.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vibespice import agent, config, tools  # noqa: E402
from vibespice import challenges as C  # noqa: E402

_last: list[dict] = []


def measure_divider(r1: str, r2: str) -> str:
    """Simulates the 12 V divider of challenge 0 with the given resistors."""
    _last.clear()
    d = tools.simulate_data(f"V1 in 0 DC 12\nR1 in out {r1}\nR2 out 0 {r2}\n.op")
    _last.append(d)
    return (f"Divider simulated: v(out) = {d['op']['v(out)']:.6g} V, "
            f"i(v1) = {d['op']['i(v1)']:.6g} A")


tools.register_tool(tools.ExtraTool(
    schema={"type": "function", "function": {
        "name": "measure_divider",
        "description": "Simulates a 12 V divider R1 (top) and R2 (bottom).",
        "parameters": {"type": "object", "properties": {
            "r1": {"type": "string"}, "r2": {"type": "string"}},
            "required": ["r1", "r2"]}}},
    run=measure_divider, simulations=lambda: list(_last),
    hint="To measure a 12 V divider, call 'measure_divider' (demo tool)."))

_reviewed: list[dict] = []


def review(data: dict) -> str | None:
    """Sends the first answer back once."""
    _reviewed.append(data)
    if len(_reviewed) == 1:
        return "Review: check the current once more and deliver the answer again."
    return None


def main() -> int:
    settings = config.load(None)
    agent.configure(settings)
    provider = agent.make_provider(settings)
    args = argparse.Namespace(think="no", mode="native", max_steps=6, show_thinking=False,
                              batch_kind="single")
    ch = C.CHALLENGES["0"]
    r = agent.run_once(provider, ch.message(), ch, args, "challenge0_demo", None,
                       heading=" · extension demo", review=review)
    return 0 if r["state"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
