#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
End-to-end tests without a real server.

Runs the self-test and then runs the agent against a fake Open WebUI in several scenarios
(native calls, calls written as text, forgotten JSON, loops, empty replies, netlist
errors, wrong answer...), free tasks, the configuration file and the log analysis. It
never reads your configuration or your logs: everything goes to a temporary folder.

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

# Providers other than Open WebUI ({url} is the fake server; None removes the variable)
CLAUDE = {"VIBESPICE_PROVIDER": "anthropic", "VIBESPICE_URL": "{url}",
          "VIBESPICE_MODEL": "claude-opus-5-5"}
HAIKU = dict(CLAUDE, VIBESPICE_MODEL="claude-haiku-4-5")
OPENAI = {"VIBESPICE_PROVIDER": "openai", "VIBESPICE_URL": "{url}/v1",
          "VIBESPICE_MODEL": "gpt-test"}

# (scenario, agent arguments, texts that must appear in the output — or must not, with a
# leading "!" — and, optionally, environment variables for that run)
CASES = [
    ("challenge2", ["check"], ["All good", "native mode works"]),
    ("challenge2", ["bench", "2", "--show-thinking"], ["Result: PASS", "💭"]),
    ("text", ["bench", "0", "--mode", "text"], ["Result: PASS"]),
    ("leak", ["bench", "0"], ["Result: PASS"]),
    ("nojson", ["bench", "0"], ["JSON block missing", "Result: PASS"]),
    ("loop", ["bench", "0"], ["loop detected", "Result: PASS"]),
    ("empty", ["bench", "0", "--think", "no"],
     ["empty reply", "Used no tools", "results without origin in its simulations",
      "❌ The results it reports come from its own simulations", "FAIL"]),
    ("errors", ["bench", "0"], ["(2 with errors)", "Result: PASS"]),
    ("cold", ["bench", "0"], ["server context: 40960 tokens", "Result: PASS"]),
    ("fail", ["bench", "2"], ["Result: FAIL"]),
    ("challenge5", ["bench", "5", "--repeat", "2"],
     ["2/2 PASS", "_challenge5_rep1.md", "_challenge5_rep2.md", "⏱ 1/2 runs in 0 min",
      "· ≈ 0 min left"]),
    ("challenge2", ["bench", "0", "--model", "does-not-exist"], ["HTTP 404"]),
    ("challenge5", ["bench", "5", "--repeat", "3", "--time-limit", "1h"],
     ["Time limit: 60 min", "end ≈", "3/3 PASS"]),
    ("challenge6", ["bench", "6", "--repeat", "200", "--time-limit", "1s"],
     ["Time limit: 0 min", "⏱ Stopped by time after"]),
    ("invents", ["bench", "6"], ["results without origin in its simulations",
                                 "(1 with errors)",
                                 "✅ The results it reports come from its own simulations",
                                 "Result: PASS"]),
    ("challenge6", ["bench", "6"], ["the best possible!", "Result: PASS"]),
    ("challenge7", ["bench", "7"],
     ["results without origin in its simulations (ic_change_pct = -0.8)",
      "the best possible!", "✅ The results it reports come from its own simulations",
      "Result: PASS"]),
    ("qwen38", ["bench", "0", "--model", "qwen3.8:27b-q8_0", "--think", "low"],
     ["reasoning: low", "Result: PASS"]),
    ("qwen38", ["check", "--model", "qwen3.8:27b-q8_0"],
     ["reasoning (--think): no, low, medium, xhigh · default medium",
      "agent profile 'qwen3.8'", "native mode works", "All good"]),
    ("challenge2", ["bench", "0", "--think", "xhigh"],
     ["qwen3:32b does not support --think xhigh. Options: yes, no."]),
    # Free tasks: no verification; the task can come from a file
    ("cold", ["run", "Design a 12 V to 5 V divider"],
     ["═══ Free task ═══", "simulate×1", "Result: UNVERIFIED"]),
    ("cold", ["run", "--file", "task.txt"], ["Free task", "Result: UNVERIFIED"]),
    ("cold", ["run"], ["Give the task in quotes or with --file"]),
    # bench without IDs lists the challenges; an unknown ID fails before connecting
    ("challenge2", ["bench"], ["Available challenges", "Warm-up"]),
    ("challenge2", ["bench", "9"], ["No challenge '9'"]),
    ("busy", ["bench", "0"], ["HTTP 429", "retrying in 0 s (1/4)", "HTTP 503", "(2/4)",
                              "Result: PASS"]),
    # Claude API: history kept as it came, tool results together, effort, caching, fallbacks
    ("challenge2", ["check"],
     ["2. Claude API", "Claude Opus 5.5: context 1000000",
      "reasoning (--think): yes, low, medium, high, xhigh, max · yes = high",
      "fallback model Anthropic recommends", "native mode works", "All good"], CLAUDE),
    ("challenge7", ["bench", "7"],
     ["anthropic · model claude-opus-5-5 · reasoning: high", "cached)", "Result: PASS",
      "!possible context truncation", "!context:"], CLAUDE),
    ("invents", ["bench", "6", "--think", "medium"], ["reasoning: medium", "Result: PASS"],
     CLAUDE),
    ("challenge6", ["bench", "6", "--show-thinking"], ["💭", "Result: PASS"], CLAUDE),
    ("busy", ["bench", "0", "--think", "low"],
     ["HTTP 429", "retrying in 0 s (1/4)", "HTTP 529", "(2/4)", "Result: PASS"], CLAUDE),
    ("refusal", ["bench", "0"],
     ["the model declined to answer (category: cyber)", "state: refusal", "Result: FAIL"],
     CLAUDE),
    ("fallback", ["run", "Design a 12 V to 5 V divider"],
     ["claude-opus-5-5 declined; claude-opus-5 continued", "Result: UNVERIFIED"], CLAUDE),
    ("challenge2", ["bench", "0", "--think", "no"], ["claude-opus-5-5 always reasons"], CLAUDE),
    ("challenge2", ["bench", "0", "--num-ctx", "8192"],
     ["--num-ctx only applies to Open WebUI"], CLAUDE),
    ("challenge2", ["status"], ["status is only available with Open WebUI"], CLAUDE),
    ("cold", ["bench", "0"], ["reasoning: yes", "Result: PASS"], HAIKU),
    ("challenge2", ["bench", "0", "--think", "low"],
     ["claude-haiku-4-5 does not support --think low. Options: yes, no."], HAIKU),
    ("challenge2", ["check"], ["HTTP 401", "Claude Console"],
     dict(CLAUDE, VIBESPICE_API_KEY="sk-ant-wrong")),
    ("cold", ["check"], ["2. Claude API", "ANTHROPIC_API_KEY", "All good"],
     dict(CLAUDE, VIBESPICE_API_KEY=None, ANTHROPIC_API_KEY="sk-test")),
    # Any OpenAI-compatible API
    ("challenge2", ["check"], ["2. OpenAI-compatible API", "native mode works", "All good"],
     OPENAI),
    ("challenge7", ["bench", "7"], ["openai · model gpt-test", "Result: PASS"], OPENAI),
    ("challenge2", ["bench", "2", "--think", "high", "--show-thinking"],
     ["reasoning: high", "💭", "Result: PASS"], OPENAI),
    ("busy", ["bench", "0"], ["HTTP 429", "HTTP 503", "Result: PASS"], OPENAI),
    ("challenge2", ["bench", "0", "--think", "no"], ["no standard way to turn reasoning off"],
     OPENAI),
    ("nokey", ["bench", "0"], ["Result: PASS"], dict(OPENAI, VIBESPICE_API_KEY=None)),
    ("challenge2", ["bench", "0", "--model", "nope"], ["HTTP 404", "does not exist"], OPENAI),
]


def vibespice(args: list[str], env: dict, cwd: Path, timeout: float = 300) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "vibespice", *args], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def report(name: str, ok: bool, output: str = "", missing=()) -> int:
    print(("✅" if ok else "❌") + " " + name)
    if not ok:
        print((f"   missing: {list(missing)}\n" if missing else "")
              + "\n".join("   | " + l for l in output.splitlines()[-25:]))
    return 0 if ok else 1


def config_tests(tmp: Path, env: dict) -> int:
    """The configuration file: profiles, VIBESPICE_PROFILE, environment over file, problems
    explained, init and the logs folder."""
    failures = 0
    cfg, logs = tmp / "cfg", tmp / "cfg-logs"
    base = {k: v for k, v in env.items() if k not in ("VIBESPICE_API_KEY", "VIBESPICE_MODEL")}
    base["XDG_CONFIG_HOME"] = str(cfg)
    srv, url = fs.start_in_background("cold")
    try:
        (cfg / "vibespice").mkdir(parents=True)
        (cfg / "vibespice" / "config.toml").write_text(
            f'default_profile = "broken"\nlogs_dir = "{logs}"\n\n'
            f'[profiles.broken]\nurl = "{url}"\n\n'
            f'[profiles.lab]\nprovider = "openwebui"\nurl = "{url}"\n'
            f'api_key = "{fs.KEY}"\nmodel = "{fs.MODEL}"\n', encoding="utf-8")
        cases = [
            (["bench", "0", "--profile", "lab"], {}, 0, ["Result: PASS"]),
            (["bench", "0"], {"VIBESPICE_PROFILE": "lab"}, 0, ["Result: PASS"]),
            (["bench", "0", "--profile", "lab"], {"VIBESPICE_MODEL": "does-not-exist"}, 0,
             ["HTTP 404"]),
            (["bench", "0"], {}, 2, ["profile 'broken'", "api_key is missing"]),
            (["check", "--profile", "nope"], {}, 2,
             ["There is no profile 'nope'", "Profiles: broken, lab"]),
        ]
        for args, extra, code, expected in cases:
            rc, out = vibespice(args, dict(base, **extra), tmp)
            missing = [e for e in expected if e not in out]
            failures += report(f"config     {' '.join(args)} {extra or ''}".rstrip(),
                               rc == code and not missing and "Traceback" not in out,
                               out, missing)
    finally:
        srv.shutdown()
    failures += report("config     logs_dir: the runs go to the folder in the file",
                       len(list(logs.glob("*_challenge0*.md"))) == 3
                       and (logs / "summary.csv").exists())

    # Without a file: what to do; init creates it (private) and never overwrites it
    empty = dict(base, XDG_CONFIG_HOME=str(tmp / "empty"))
    rc, out = vibespice(["check"], empty, tmp)
    failures += report("config     check without a file explains it",
                       rc == 2 and "vibespice init" in out and "url is missing" in out, out)
    fresh = dict(base, XDG_CONFIG_HOME=str(tmp / "fresh"))
    path = tmp / "fresh" / "vibespice" / "config.toml"
    rc1, out1 = vibespice(["init"], fresh, tmp)
    rc2, out2 = vibespice(["init"], fresh, tmp)
    rc3, out3 = vibespice(["check"], fresh, tmp)
    out = out1 + out2 + out3
    failures += report("config     init creates the file (600), never overwrites it",
                       rc1 == 0 and "Created" in out1 and path.exists()
                       and path.stat().st_mode & 0o777 == 0o600
                       and "already exists" in out2
                       and rc3 == 2 and "api_key still has the example value" in out3, out)
    bad = tmp / "bad.toml"
    bad.write_text("url = \n", encoding="utf-8")
    rc, out = vibespice(["check"], dict(base, VIBESPICE_CONFIG=str(bad)), tmp)
    failures += report("config     a broken file is explained",
                       rc == 2 and "is not valid TOML" in out, out)
    return failures


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vibespice_tests_"))
    (tmp / "task.txt").write_text("Design a divider that gives 5 V from 12 V.\n",
                                  encoding="utf-8")
    # Never the developer's configuration, logs or keys: their own folders, no VIBESPICE_*
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIBESPICE_")
           and k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    env.update(NO_COLOR="1", VIBESPICE_NO_NOTIFY="1", PYTHONPATH=str(ROOT),
               XDG_CONFIG_HOME=str(tmp / "config"), XDG_DATA_HOME=str(tmp / "data"),
               VIBESPICE_API_KEY=fs.KEY, VIBESPICE_MODEL=fs.MODEL,
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    failures = 0

    rc, out = vibespice(["selftest"], env, tmp)
    failures += report("selftest", rc == 0 and "Self-test passed" in out, out)
    rc, out = vibespice(["--version"], env, tmp, timeout=60)
    failures += report("--version", rc == 0 and out.startswith("vibespice ")
                       and "AGPL-3.0-only" in out and "NO WARRANTY" in out, out)

    for scenario, args, expected, *extra in CASES:
        srv, url = fs.start_in_background(scenario)
        run_env = dict(env, VIBESPICE_URL=url)
        for k, v in (extra[0] if extra else {}).items():
            if v is None:
                run_env.pop(k, None)
            else:
                run_env[k] = v.format(url=url)
        try:
            rc, out = vibespice(args, run_env, tmp)
        finally:
            srv.shutdown()
        missing = [e for e in expected if (e[1:] in out if e.startswith("!") else e not in out)]
        name = (run_env.get("VIBESPICE_PROVIDER") or "")[:9]
        failures += report(f"{scenario:10s} {name + ' ' if name else ''}{' '.join(args)}",
                           not missing and "Traceback" not in out, out, missing)

    # The log analysis must understand what the tests left behind
    rc, out = vibespice(["analyze", "--by-version"], env, tmp, timeout=120)
    expected = ["runs", "Q sat/cutoff", "code", "Failing criteria", "simulate",
                "Code versions: 0.1"]
    failures += report("analyze", rc == 0 and all(e in out for e in expected)
                       and "Traceback" not in out, out)

    failures += config_tests(tmp, env)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nAll tests passed." if not failures else f"\n{failures} test(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
