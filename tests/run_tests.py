#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Mauro Rodriguez Blasco
"""
End-to-end tests without a real server.

Runs the self-test and then runs the agent against a fake Open WebUI in several scenarios
(native calls, calls written as text, forgotten JSON, loops, empty replies, netlist
errors, wrong answer...).

    python3 tests/run_tests.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_server as fs  # noqa: E402

# (scenario, agent arguments, texts that must appear in the output)
CASES = [
    ("challenge2", ["--check"], ["All good", "native mode works"]),
    ("challenge2", ["--challenge", "2", "--show-thinking"], ["Result: PASS", "💭"]),
    ("text", ["--challenge", "0", "--mode", "text"], ["Result: PASS"]),
    ("leak", ["--challenge", "0"], ["Result: PASS"]),
    ("nojson", ["--challenge", "0"], ["JSON block missing", "Result: PASS"]),
    ("loop", ["--challenge", "0"], ["loop detected", "Result: PASS"]),
    ("empty", ["--challenge", "0", "--think", "no"],
     ["empty reply", "Used no tools", "results without origin in its simulations",
      "❌ The results it reports come from its own simulations", "FAIL"]),
    ("errors", ["--challenge", "0"], ["(2 with errors)", "Result: PASS"]),
    ("cold", ["--challenge", "0"], ["server context: 40960 tokens", "Result: PASS"]),
    ("fail", ["--challenge", "2"], ["Result: FAIL"]),
    ("challenge5", ["--challenge", "5", "--repeat", "2"],
     ["2/2 PASS", "_challenge5_rep1.md", "_challenge5_rep2.md", "⏱ 1/2 runs in 0 min",
      "· ≈ 0 min left"]),
    ("challenge2", ["--challenge", "0", "--model", "does-not-exist"], ["HTTP 404"]),
    ("challenge5", ["--challenge", "5", "--repeat", "3", "--time-limit", "1h"],
     ["Time limit: 60 min", "end ≈", "3/3 PASS"]),
    ("challenge6", ["--challenge", "6", "--repeat", "200", "--time-limit", "1s"],
     ["Time limit: 0 min", "⏱ Stopped by time after"]),
    ("invents", ["--challenge", "6"], ["results without origin in its simulations",
                                       "(1 with errors)",
                                       "✅ The results it reports come from its own simulations",
                                       "Result: PASS"]),
    ("challenge6", ["--challenge", "6"], ["the best possible!", "Result: PASS"]),
    ("challenge7", ["--challenge", "7"],
     ["results without origin in its simulations (ic_change_pct = -0.8)",
      "the best possible!", "✅ The results it reports come from its own simulations",
      "Result: PASS"]),
    ("qwen38", ["--challenge", "0", "--model", "qwen3.8:27b-q8_0", "--think", "low"],
     ["reasoning: low", "Result: PASS"]),
    ("qwen38", ["--check", "--model", "qwen3.8:27b-q8_0"],
     ["reasoning (--think): no, low, medium, xhigh · default medium",
      "agent profile 'qwen3.8'", "native mode works", "All good"]),
    ("challenge2", ["--challenge", "0", "--think", "xhigh"],
     ["qwen3:32b does not support --think xhigh. Options: yes, no."]),
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vibespice_tests_"))
    shutil.copytree(ROOT / "vibespice", tmp / "vibespice",
                    ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ, NO_COLOR="1", VIBESPICE_NO_NOTIFY="1",
               OWUI_API_KEY=fs.KEY, OWUI_MODEL=fs.MODEL,
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    failures = 0

    r = subprocess.run([sys.executable, "-m", "vibespice", "--selftest"], cwd=tmp,
                       env=env, capture_output=True, text=True, timeout=300)
    ok = r.returncode == 0 and "Self-test passed" in r.stdout
    print(("✅" if ok else "❌") + " selftest")
    if not ok:
        failures += 1
        print(r.stdout[-1500:], r.stderr[-800:])

    r = subprocess.run([sys.executable, "-m", "vibespice", "--version"], cwd=tmp,
                       env=env, capture_output=True, text=True, timeout=60)
    ok = r.returncode == 0 and r.stdout.startswith("vibespice ")
    print(("✅" if ok else "❌") + " --version")
    failures += not ok

    for scenario, args, expected in CASES:
        srv, url = fs.start_in_background(scenario)
        try:
            r = subprocess.run([sys.executable, "-m", "vibespice", *args], cwd=tmp,
                               env=dict(env, OWUI_URL=url), capture_output=True,
                               text=True, timeout=300)
        finally:
            srv.shutdown()
        output = r.stdout + r.stderr
        missing = [e for e in expected if e not in output]
        ok = not missing and "Traceback" not in output
        print(("✅" if ok else "❌") + f" {scenario:10s} {' '.join(args)}")
        if not ok:
            failures += 1
            print(f"   missing: {missing}\n"
                  + "\n".join("   | " + l for l in output.splitlines()[-25:]))

    # The log analysis must understand what the tests left behind
    r = subprocess.run([sys.executable, "-m", "vibespice.analyze", "--by-version"], cwd=tmp,
                       env=env, capture_output=True, text=True, timeout=120)
    expected = ["runs", "Q sat/cutoff", "code", "Failing criteria", "simulate",
                "Code versions: 0.1"]
    ok = r.returncode == 0 and all(e in r.stdout for e in expected) and "Traceback" not in r.stderr
    print(("✅" if ok else "❌") + " analyze_logs.py")
    if not ok:
        failures += 1
        print(r.stdout[-1500:], r.stderr[-800:])

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nAll tests passed." if not failures else f"\n{failures} test(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
