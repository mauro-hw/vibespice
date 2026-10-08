#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
End-to-end tests without a real server.

Runs the self-test and then runs the agent against a fake Open WebUI in several scenarios
(native calls, calls written as text, forgotten JSON, loops, empty replies, netlist
errors, wrong answer...), free tasks, the configuration file, the log analysis and the
MCP server. It never reads your configuration or your logs: everything goes to a temporary
folder.

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
sys.path.insert(0, str(ROOT))
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
    # Streamed replies (the default): each reply takes 3 s, longer than the timeout of 2 s,
    # but keeps coming; in one piece it times out. And the replies in one piece still work
    ("slow", ["bench", "0"], ["Result: PASS"], {"VIBESPICE_TIMEOUT": "2"}),
    ("slow", ["bench", "0"], ["API ERROR: No reply within 2 s"],
     {"VIBESPICE_TIMEOUT": "2", "VIBESPICE_STREAM": "false"}),
    ("challenge7", ["bench", "7"], ["the best possible!", "Result: PASS"],
     {"VIBESPICE_STREAM": "no"}),
    ("challenge2", ["check"], ["stream must be true or false"], {"VIBESPICE_STREAM": "maybe"}),
    # A server that ignores max_tokens: while streaming, the client cuts the reply itself
    ("runaway", ["bench", "0"], ["empty reply", "the reply was cut at max_tokens",
                                 "Result: PASS"], {"VIBESPICE_MAX_TOKENS": "300"}),
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
    ("challenge7", ["bench", "7"], ["openai · model gpt-test", "Result: PASS"],
     dict(OPENAI, VIBESPICE_STREAM="false")),
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


def wizard_tests(tmp: Path) -> int:
    """vibespice init in a terminal, run here with scripted answers against the fake
    server, with its own configuration folder and no key in the environment."""
    import tomllib

    from vibespice import config, wizard
    saved = dict(os.environ)
    for k in list(os.environ):
        if k.startswith("VIBESPICE_") or k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            del os.environ[k]
    os.environ["XDG_CONFIG_HOME"] = str(tmp / "wizard")
    failures = 0

    def init(answers, secrets, scenario="challenge2", urls=None) -> tuple[int, str]:
        srv, url = fs.start_in_background(scenario)
        a, k, lines = iter(answers), iter(secrets), []

        def ask(prompt):            # out of answers = end of input, as in a terminal
            lines.append(prompt)
            try:
                return next(a).format(url=url)
            except StopIteration:
                raise EOFError from None
        try:
            rc = wizard.run("vibespice", ask, lambda prompt: next(k), lines.append,
                            {n: u.format(url=url) for n, u in (urls or {}).items()})
        finally:
            srv.shutdown()
        return rc, "\n".join(lines)

    def profiles() -> dict:
        path = config.config_file()
        return tomllib.loads(path.read_text()) if path.exists() else {}

    try:
        # Claude: a wrong key, then the right one; Enter takes the suggested model
        rc, out = init(["1", "1", "", "n"], ["sk-ant-wrong", fs.KEY],
                       urls={"claude": "{url}"})
        d, path = profiles(), config.config_file()
        failures += report("init       new file: Claude, wrong key then the right one",
                           rc == 0 and "HTTP 401" in out and "Connected: 2 models" in out
                           and d.get("default_profile") == "claude"
                           and d["profiles"]["claude"]["model"] == "claude-opus-5-5"
                           and d["profiles"]["claude"]["api_key"] == fs.KEY
                           and path.stat().st_mode & 0o777 == 0o600, out)
        before = path.read_text()
        # Open WebUI: a typo in the model, then a number; becomes the default
        rc, out = init(["y", "6", "{url}", "qwen", "1", "y", "n"], [fs.KEY])
        d = profiles()
        failures += report("init       adds Open WebUI, suggests models, keeps the rest",
                           rc == 0 and "Did you mean: qwen3:32b" in out
                           and d.get("default_profile") == "openwebui"
                           and d["profiles"]["openwebui"]["model"] == "qwen3:32b"
                           and before.replace('default_profile = "claude"',
                                              'default_profile = "openwebui"')
                           in path.read_text(), out)
        # Ollama: no key; not the default
        rc, out = init(["y", "4", "qwen3:8b", "n", "n"], [], scenario="nokey",
                       urls={"local": "{url}/v1"})
        d = profiles()
        failures += report("init       Ollama without a key, not the default",
                           rc == 0 and "api_key" in d["profiles"]["local"]
                           and not d["profiles"]["local"]["api_key"]
                           and d["default_profile"] == "openwebui", out)
        # Unreachable server: saved anyway, with the model typed by hand
        rc, out = init(["y", "5", "http://127.0.0.1:9/v1", "2", "my-model", "n"], [""])
        d = profiles()
        failures += report("init       unreachable server, saved anyway",
                           rc == 0 and "Can't connect" in out
                           and d["profiles"]["custom"]["model"] == "my-model", out)
        # Cancelled halfway: nothing changes
        text = path.read_text()
        rc, out = init(["y"], [])
        failures += report("init       cancelled halfway, nothing saved",
                           rc == 1 and "nothing was saved" in out and path.read_text() == text,
                           out)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return failures


def mcp_tests(tmp: Path, env: dict) -> int:
    """vibespice mcp as a chat app sees it: both generations of the protocol, the tools,
    errors the model can fix, malformed input and, in a terminal, how to add it."""
    import json
    import pty

    modern = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
              "io.modelcontextprotocol/clientCapabilities": {}}
    divider = "V1 a 0 DC 12\nR1 a b 7k\nR2 b 0 5k\n.op"

    def call(name, arguments, meta=None) -> dict:
        params = {"name": name, "arguments": arguments}
        return dict(params, _meta=meta) if meta else params

    requests = {      # id → (method, params); plus notifications and junk, without an id
        1: ("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "tests", "version": "0"}}),
        2: ("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}}),
        3: ("ping", None),
        4: ("tools/list", {}),
        5: ("tools/call", call("simulate", {"netlist": divider})),
        6: ("tools/call", call("simulate", {"netlist": "V1 a 0 1\n.control\nshell ls\n.endc"})),
        7: ("tools/call", call("calculate", {"expression": "parallel(2.2k,4.7k)"})),
        8: ("tools/call", call("analyze_tolerances", {
            "netlist": divider, "tolerances": {"R1": 1, "R2": 1}, "output": "v(b)",
            "target_value": 5})),
        9: ("tools/call", call("nope", {})),
        10: ("tools/call", call("calculate", "1+1")),
        11: ("resources/list", {}),
        12: ("server/discover", {"_meta": modern}),
        13: ("tools/list", {"_meta": modern}),
        14: ("tools/call", call("simulate", {"netlist": divider}, modern)),
        15: ("tools/list", {"_meta": dict(modern, **{
            "io.modelcontextprotocol/protocolVersion": "2099-01-01"})}),
        16: ("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}),
        17: ("server/discover", {}),
    }
    lines = []
    for rid, (method, params) in requests.items():
        lines.append({"jsonrpc": "2.0", "id": rid, "method": method,
                      **({"params": params} if params is not None else {})})
        if rid == 1:
            lines.append({"jsonrpc": "2.0", "method": "notifications/initialized"})
    lines.append({"jsonrpc": "2.0", "id": 99, "result": {}})     # a response: no reply
    text = "\n".join(json.dumps(m) for m in lines) + "\nnot json\n[]\n"
    r = subprocess.run([sys.executable, "-m", "vibespice", "mcp"], cwd=tmp, env=env,
                       input=text, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    try:
        replies = [json.loads(l) for l in r.stdout.splitlines()]
    except json.JSONDecodeError:
        return report("mcp        stdout carries only JSON-RPC", False, out)
    got = {m.get("id"): m for m in replies if m.get("id") is not None}
    junk = [m for m in replies if m.get("id") is None]

    def result(rid) -> dict:
        return got.get(rid, {}).get("result") or {}

    def code(rid):
        return got.get(rid, {}).get("error", {}).get("code")

    def text_of(rid) -> str:
        return "".join(c.get("text", "") for c in result(rid).get("content", []))

    tools = result(4).get("tools", [])
    failures = report(
        "mcp        legacy: initialize, ping and the tool list",
        r.returncode == 0 and len(replies) == len(requests) + 2
        and result(1).get("protocolVersion") == "2025-06-18"
        and result(1).get("serverInfo", {}).get("websiteUrl", "").startswith("https://")
        and "ngspice" in result(1).get("instructions", "")
        and result(2).get("protocolVersion") == "2025-11-25"
        and got.get(3, {}).get("result") == {} and "resultType" not in result(4)
        and [t["name"] for t in tools] == ["simulate", "analyze_tolerances",
                                           "standard_values", "calculate"]
        and all(t["annotations"]["readOnlyHint"] and t["inputSchema"]["type"] == "object"
                for t in tools)
        and "MCP server · ngspice" in r.stderr, out)
    failures += report(
        "mcp        tools: results, errors for the model and protocol errors",
        result(5).get("isError") is False and "v(b) = 5 V" in text_of(5)
        and result(6).get("isError") is True and text_of(6).startswith("ERROR")
        and "= 1498.55" in text_of(7) and "CORNERS" in text_of(8)
        and code(9) == -32602 and code(10) == -32602 and code(11) == -32601, out)
    failures += report(
        "mcp        modern (2026-07-28): discover, versions and _meta",
        result(12).get("resultType") == "complete"
        and result(12).get("supportedVersions") == ["2026-07-28"]
        and result(12).get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {})
        .get("name") == "vibespice"
        and result(13).get("cacheScope") == "public" and len(result(13).get("tools", [])) == 4
        and result(14).get("resultType") == "complete" and "v(b) = 5 V" in text_of(14)
        and code(15) == -32022
        and got[15]["error"].get("data", {}).get("supported") == ["2026-07-28"]
        and code(16) == -32602 and code(17) == -32602, out)
    failures += report(
        "mcp        malformed input is answered, notifications are not",
        sorted(m["error"]["code"] for m in junk) == [-32700, -32600] and 99 not in got, out)

    # In a terminal it explains how to add it to each app, instead of waiting for JSON
    primary, secondary = pty.openpty()
    try:
        r = subprocess.run([sys.executable, "-m", "vibespice", "mcp"], cwd=tmp, env=env,
                           stdin=secondary, capture_output=True, text=True, timeout=60)
    finally:
        os.close(primary)
        os.close(secondary)
    out = r.stdout + r.stderr
    failures += report("mcp        in a terminal: how to add it to each app",
                       r.returncode == 0 and '"mcpServers"' in r.stdout
                       and "claude mcp add --scope user vibespice --" in r.stdout
                       and "codex mcp add vibespice --" in r.stdout
                       and "MCP server" not in out, out)
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
    failures += report("--version", rc == 0 and out.startswith("VibeSPICE ")
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
    failures += wizard_tests(tmp)
    failures += mcp_tests(tmp, env)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nAll tests passed." if not failures else f"\n{failures} test(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
