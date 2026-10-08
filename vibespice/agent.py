# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
Iterative LLM + ngspice agent.

The model (on your server or a cloud API) thinks and decides; this program, on your
computer, runs ngspice and sends the results back, in a loop, until it gives a final
answer. That answer is then verified independently (by re-simulating) and everything is
logged.

This module holds the agent loop, the logs and the verification of one run. The servers
and APIs live in providers/, the commands in cli.py.

Standard library only (tested with Python 3.11 to 3.14).
"""
from __future__ import annotations

import csv
import importlib.metadata
import json
import math
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import __version__, config
from . import challenges as C
from . import tools as hs
from .providers import CLASSES, APIError, Provider
from .console import BLUE, BOLD, GREEN, GREY, RED, YELLOW, Heartbeat, c, fmt_dur, fmt_tok, \
    shorten, tilde

DIR = Path(__file__).resolve().parent     # the package (for the git commit of the code)
LOGS = config.default_logs_dir()          # set by configure()


def configure(settings: config.Settings) -> None:
    """Applies the parts of the configuration that do not need a server: the logs folder
    and the ngspice executable."""
    global LOGS
    LOGS = settings.logs
    hs.NGSPICE = settings.ngspice


# ---------------------------------------------------------------------------
# The provider: the server or API that runs the model (providers/)
# ---------------------------------------------------------------------------
def make_provider(settings: config.Settings, model: str | None = None,
                  need_model: bool = True) -> Provider:
    """The provider of the profile in use. Raises APIError, explained, if the configuration
    is incomplete or (with need_model) there is no model."""
    if settings.problems:
        raise APIError("Incomplete configuration.\n" + config.diagnosis(settings))
    model = model or settings.model
    if need_model and not model:
        raise APIError("No model chosen: set model in your profile or pass --model. "
                       "'vibespice check' lists the models on the server.")
    return CLASSES[settings.provider](settings.url, settings.api_key, model, settings.ca,
                                      settings.timeout, settings.max_tokens, settings.fallbacks,
                                      stream=settings.stream)


# ---------------------------------------------------------------------------
# Reading the model's replies
# ---------------------------------------------------------------------------
_RE_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)


def calls_from_text(text: str) -> list[dict]:
    """Calls written as text <tool_call>{...}</tool_call> (Qwen's native format)."""
    out = []
    for block in _RE_TOOL_CALL.findall(text):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            try:
                d, _ = json.JSONDecoder().raw_decode(block)
            except json.JSONDecodeError:
                out.append({"name": "?", "args": None, "error": block[:200]})
                continue
        name = d.get("name") or "?"
        args = d.get("arguments", {})
        out.append({"name": name, "args": args})
    return out


def extract_final_json(text: str) -> dict | None:
    def load(s: str):
        s = re.sub(r",\s*([}\]])", r"\1", s)          # trailing commas
        s = re.sub(r"(?m)//[^\n\"]*$", "", s)          # // comments
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else None
        except json.JSONDecodeError:
            return None
    for b in reversed(re.findall(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", text, re.S)):
        d = load(b)
        if d is not None:
            return d
    dec, last, i = json.JSONDecoder(), None, 0
    while (i := text.find("{", i)) != -1:
        try:
            d, end = dec.raw_decode(text[i:])
            if isinstance(d, dict) and d:
                last = d
            i += max(end, 1)
        except json.JSONDecodeError:
            i += 1
    return last


def call_summary(name: str, args) -> str:
    if not isinstance(args, dict):
        return f"{name}(unreadable arguments)"
    if name == "simulate":
        n = len([l for l in str(args.get("netlist", "")).splitlines() if l.strip()])
        return f"simulate(netlist of {n} lines)"
    if name == "analyze_tolerances":
        tol = args.get("tolerances") or {}
        tols = ", ".join(f"{k} {v} %" for k, v in tol.items()) if isinstance(tol, dict) else "?"
        extra = f", {args.get('samples')} samples, {args.get('distribution', 'gaussian')}" \
            if str(args.get("method", "")).startswith("mont") else ""
        return f"analyze_tolerances({args.get('method', 'corners')}{extra}; {tols}; " \
               f"{args.get('output')})"
    if name == "standard_values":
        return f"standard_values({args.get('value', '')} in {args.get('series')})"
    if name == "calculate":
        return f"calculate({shorten(str(args.get('expression', '')), 70)})"
    return f"{name}({shorten(json.dumps(args, ensure_ascii=False), 70)})"


# ---------------------------------------------------------------------------
# Logs on disk
# ---------------------------------------------------------------------------
class RunLog:
    def __init__(self, label: str):
        folder = LOGS
        folder.mkdir(parents=True, exist_ok=True)
        base = f"{datetime.now():%Y%m%d-%H%M%S}_{re.sub(r'[^A-Za-z0-9_-]+', '_', label)}"
        name, n = base, 1
        while (folder / f"{name}.md").exists() or (folder / f"{name}.json").exists():
            n += 1                           # two runs in the same second
            name = f"{base}_{n}"
        self.md = folder / f"{name}.md"
        self.js = folder / f"{name}.json"
        self.data: dict = {"events": []}

    def md_add(self, text: str) -> None:
        with self.md.open("a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n\n")

    def event(self, **kw) -> None:
        self.data["events"].append(kw)

    def save_json(self) -> None:
        self.js.write_text(json.dumps(self.data, ensure_ascii=False, indent=1, default=str),
                           encoding="utf-8")


def block(text: str, language: str = "") -> str:
    text = str(text).replace("```", "ˋˋˋ")
    return f"```{language}\n{text.strip()}\n```"


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an analog electronics engineer with access to a real SPICE simulator (ngspice) through tools.

How to work:
- Work iteratively: propose, simulate, compare with the goal and adjust until it is met.
- Your memory of the E series and your mental arithmetic are NOT reliable. Circuit numbers must come from the simulator or from 'calculate', and standard values from 'standard_values': check it before treating a value as standard. Do not make up results.
- Compare several candidates in a single call: 'calculate' accepts several expressions separated by ';', and one netlist can contain several independent circuits (with different nodes).
- If a tool returns an error, read it, fix the problem and try again.

Netlists for ngspice:
- No title line and no .control blocks (the tool adds them).
- Ground is node 0. Suffixes: k = kilo, MEG = mega, m = milli ('M' is milli too!).
- The DC operating point is always printed; for .tran/.ac/.dc use .meas.
- A negative i(V1) means the source is delivering current.

When you finish:
- If you deliver a design, simulate exactly that design and check every requirement of the task against the numbers from the tools.
- If a requirement is not met, even by a small margin, do not deliver that design: keep searching. If you conclude it is impossible, say so clearly and explain why; do not force an answer.
- Summarize the reasoning and the key results, with units.
- If the task asks for a ```json block, end with it."""


_BEFORE_HINTS = "- If a tool returns an error"     # extra tools' hints go before this line


TEXT_MODE_PROMPT = """

# Tools
To use a tool, write exactly one block like this (you can write several):
<tool_call>
{"name": "tool_name", "arguments": {...}}
</tool_call>
Then STOP and wait: you will receive the result inside <tool_response>. Never write the results yourself.

Available tools (JSON Schema):
"""


def pct_changes(quantity: str, quantities: list[dict[str, float]]) -> list[float]:
    """|% change| between every pair of values of the same quantity, in both directions.

    'ic' compares the Ic of any transistor, in different simulations or in the same one (two
    circuits in one netlist), but never different quantities: among hundreds of numbers, some
    pair would match by chance."""
    vals = list(dict.fromkeys(v for m in quantities[-300:] for k, v in m.items()
                              if (k == quantity or k.endswith("." + quantity))
                              and v and math.isfinite(v)))
    return [abs((b - a) / a) * 100 for a in vals for b in vals if a != b]


def without_origin(data: dict, measured: dict[str, float], numbers: list[float],
                   derived: dict[str, str] | None = None,
                   quantities: list[dict[str, float]] | None = None) -> list[str]:
    """Result fields of the JSON that do not come from any of the model's simulations.

    Measured fields must match (±0.5 %) a simulated value; derived ones, the % change of the
    same quantity between two simulations (±1 %, or ±0.1 points: rounding)."""
    missing = []
    for field, scale in measured.items():
        if field not in data:
            continue
        try:
            x = abs(C.num(data, field)) * scale
        except (KeyError, ValueError, TypeError):
            continue                      # broken format: the verifier already flags it
        if not any(abs(n - x) <= 0.005 * x + 1e-15 for n in numbers):
            missing.append(f"{field} = {data[field]}")
    for field, quantity in (derived or {}).items():
        if field not in data:
            continue
        try:
            x = abs(C.num(data, field))
        except (KeyError, ValueError, TypeError):
            continue
        if not any(abs(c - x) <= max(0.01 * c, 0.1)
                   for c in pct_changes(quantity, quantities or [])):
            missing.append(f"{field} = {data[field]}")
    return missing


def run_agent(provider: Provider, task: str, args, log: RunLog, wants_json: bool,
              num_ctx: int | None, measured: dict | None = None,
              derived: dict | None = None, review=None) -> dict:
    """review(data) -> text or None: a program built on VibeSPICE can check the final JSON
    and, with a text, send it back to the model in the same conversation (twice at most)."""
    native = args.mode == "native"
    offered = hs.schemas()
    system = SYSTEM_PROMPT
    hints = "".join(f"- {h}\n" for h in hs.hints())
    if hints:
        system = system.replace(_BEFORE_HINTS, hints + _BEFORE_HINTS, 1)
    if not native:
        system += TEXT_MODE_PROMPT + json.dumps(
            [e["function"] for e in offered], ensure_ascii=False, indent=1)
    conv = provider.conversation(system, task, offered if native else None)
    options = provider.request_extras(args.think, num_ctx)
    log.data.update(system=system, task=task, options=options, mode=args.mode)
    log.md_add("## System prompt\n\n" + block(system))
    log.md_add("## Task\n\n" + block(task))

    st = {"steps": 0, "calls": 0, "tool_errors": 0, "tool_use": {},
          "max_input_tokens": 0, "input_tokens": 0, "cache_read_tokens": 0,
          "output_tokens": 0, "llm_seconds": 0.0,
          "final": "", "json": None, "state": "max_steps", "warnings": [],
          "context": num_ctx}
    t_start = time.perf_counter()
    last_signatures: list[str] = []
    nudges = {"json": 0, "empty": 0, "loop": 0, "origin": 0, "review": 0}
    numbers: list[float] = []          # values the model got from 'simulate' (or extra tools)
    quantities: list[dict] = []        # the same, named and per simulation (derived)
    prev_tokens = 0

    for step in range(1, args.max_steps + 1):
        st["steps"] = step
        t0 = time.perf_counter()
        with Heartbeat(f"{provider.model} thinking (step {step})") as beat:
            provider.progress = beat.progress
            try:
                reply = provider.chat(conv, args.think, num_ctx)
            finally:
                provider.progress = None
        dt = time.perf_counter() - t0
        st["llm_seconds"] += dt
        content, reasoning, usage = reply.content, reply.reasoning, reply.usage
        t_in, t_out = usage.input, usage.output
        st["max_input_tokens"] = max(st["max_input_tokens"], t_in)
        st["input_tokens"] += t_in
        st["cache_read_tokens"] += usage.cache_read
        st["output_tokens"] += t_out

        # If the model was not loaded at the start, it is now: read the context the server
        # loaded it with (read-only; the options sent do not change)
        if step == 1 and not st["context"]:
            st["context"] = provider.loaded_context()
            if st["context"]:
                print(c(f"    server context: {st['context']} tokens", GREY))
                log.md_add(f"Context the server loaded the model with: {st['context']} tokens")

        # Is the context being truncated?
        ctx = st["context"]
        if ctx and t_in >= 0.95 * ctx:
            st["warnings"].append(f"step {step}: context almost full ({t_in}/{ctx} tokens)")
        if prev_tokens and t_in and t_in < prev_tokens * 0.9:
            st["warnings"].append(f"step {step}: input dropped from {prev_tokens} to {t_in} "
                                  "tokens (possible context truncation)")
        prev_tokens = max(prev_tokens, t_in)

        # Tool calls: native or written as text
        calls = reply.tool_calls
        text_calls = [] if calls else calls_from_text(content)
        visible = _RE_TOOL_CALL.sub("", content).strip() if text_calls else content

        cached = f" ({fmt_tok(usage.cache_read)} cached)" if usage.cache_read else ""
        line = (f"[{step}] model {fmt_dur(dt)} · input {fmt_tok(t_in)} tok{cached} · output "
                f"{fmt_tok(t_out)} tok" + (f" ({usage.speed} tok/s)"
                                           if usage.speed not in (None, "N/A") else ""))
        print(c(line, GREY))
        if args.show_thinking and reasoning:
            print(c("    💭 " + shorten(reasoning, 1500).replace("\n", "\n       "), GREY))
        if visible and (calls or text_calls):
            print("    " + shorten(visible, 300).replace("\n", "\n    "))

        log.md_add(f"## Step {step}\n\n{fmt_dur(dt)} · input {t_in} tok"
                   + (f" ({usage.cache_read} cached)" if usage.cache_read else "")
                   + f" · output {t_out} tok")
        for n in reply.notes:
            print(c(f"    ⚠ {n}", YELLOW))
            log.md_add(f"> ⚠ {n}")
        if reasoning:
            log.md_add("<details><summary>Reasoning</summary>\n\n" + block(reasoning)
                       + "\n\n</details>")
        if visible:
            log.md_add(visible)
        log.event(step=step, seconds=dt, usage=usage.raw, reasoning=reasoning,
                  content=reply.raw_text,
                  tool_calls=[{"id": t.id, "name": t.name, "arguments": t.args} for t in calls])
        if reply.stop == "max_tokens":
            st["warnings"].append(f"step {step}: the reply was cut at max_tokens (raise "
                                  "max_tokens in the profile)")
        if reply.stop == "refusal":
            st.update(final=visible, state="refusal")
            break

        every = [{"call": t, "name": t.name, "args": t.args} for t in calls] or text_calls
        if every:
            results, replies = [], []
            for l in every:
                st["calls"] += 1
                st["tool_use"][l["name"]] = st["tool_use"].get(l["name"], 0) + 1
                if l.get("error"):
                    result = ("ERROR: I could not read your call; it must be valid JSON with "
                              "\"name\" and \"arguments\".")
                else:
                    with Heartbeat(f"running {l['name']}"):
                        result = hs.run_tool(l["name"], l["args"])
                failed = (result.startswith("ERROR") or "= ERROR" in result
                          or result.startswith("The simulation produced NO")
                          or "Measurements that FAILED" in result)
                st["tool_errors"] += failed
                if l["name"] == "simulate" and isinstance(l["args"], dict) \
                        and not result.startswith("ERROR"):
                    try:        # what the model actually saw (0.01 s: it is re-run)
                        d = hs.simulate_data(l["args"].get("netlist", ""))
                        numbers += hs.numbers_of(d)
                        quantities.append(hs.quantities_of(d))
                    except (hs.NetlistError, hs.SimulationError):
                        pass
                for d in hs.simulations_of(l["name"]):     # what an extra tool simulated
                    numbers += hs.numbers_of(d)
                    quantities.append(hs.quantities_of(d))
                print("    " + c("→ " + call_summary(l["name"], l["args"]), BLUE))
                first = [x for x in result.splitlines() if x.strip()][:3]
                color = RED if failed else GREY
                print(c("    ← " + shorten(" | ".join(s.strip() for s in first), 160), color))
                md_key = "netlist" if isinstance(l["args"], dict) and "netlist" in l["args"] \
                    else None
                md_args = l["args"][md_key] if md_key else None
                others = {k: v for k, v in l["args"].items() if k != md_key} \
                    if isinstance(l["args"], dict) else l["args"]
                log.md_add(f"**Tool:** `{l['name']}`"
                           + (("\n\n" + block(md_args, "spice")) if md_args else "")
                           + (("\n\n" + block(json.dumps(others, ensure_ascii=False, indent=1),
                                              "json")) if others else "")
                           + "\n\n**Result:**\n\n" + block(result))
                log.event(step=step, tool=l["name"], arguments=l["args"], result=result)
                if calls:
                    results.append((l["call"], result, result.startswith("ERROR")))
                else:
                    replies.append(f"<tool_response>\n{result}\n</tool_response>")
            conv.add_reply(reply)
            if calls:
                conv.add_tool_results(results)
            else:
                conv.add_user("\n".join(replies))

            # Loop detector: the same call with the same arguments 3 times
            last_signatures.append(json.dumps([(l["name"], l["args"]) for l in every],
                                              sort_keys=True, ensure_ascii=False, default=str))
            if len(last_signatures) >= 3 and len(set(last_signatures[-3:])) == 1 \
                    and nudges["loop"] < 2:
                nudges["loop"] += 1
                notice = ("You have repeated the same call several times with the same result. "
                          "Change approach or, if you already have what you need, give the "
                          "final answer.")
                conv.add_user(notice)
                print(c("    ⚠ loop detected: asking it to change approach", YELLOW))
                log.md_add(f"> ⚠ {notice}")
            continue

        # No calls: final answer (or nearly)
        if not visible:
            if nudges["empty"] < 2:
                nudges["empty"] += 1
                conv.add_reply(reply)
                conv.add_user("Your reply was empty. Continue: use a tool or give the final "
                              "answer.")
                print(c("    ⚠ empty reply, asking it to continue", YELLOW))
                continue
            st["state"] = "empty"
            break
        data = extract_final_json(visible) if wants_json else None
        if wants_json and data is None and nudges["json"] < 1:
            nudges["json"] += 1
            conv.add_reply(reply)
            conv.add_user("The final ```json block with the requested fields is missing. "
                          "Repeat your final answer including it; there is no need to simulate "
                          "again.")
            print(c("    ⚠ JSON block missing, reminding it", YELLOW))
            continue
        # The results it reports must come from its own simulations
        missing = without_origin(data, measured or {}, numbers, derived, quantities) \
            if data and (measured or derived) else []
        if missing and nudges["origin"] < 2:
            nudges["origin"] += 1
            notice = ("These values in your JSON do not come from any successful simulation of "
                      f"yours: {', '.join(missing)}. Do not make up results: measure them with "
                      "'simulate' (if a .meas fails, read the warning and fix it) and deliver "
                      "the answer again with the measured values.")
            for field, quantity in (derived or {}).items():
                if any(f.startswith(f"{field} = ") for f in missing):
                    notice += (f" {field} is the % change of {quantity} between two simulations "
                               "of yours: simulate the two cases the task compares and compute "
                               "it from the values they return.")
            conv.add_reply(reply)
            conv.add_user(notice)
            print(c(f"    ⚠ results without origin in its simulations ({', '.join(missing)}): "
                    "asking it to measure them", YELLOW))
            log.md_add(f"> ⚠ {notice}")
            continue
        st["invented"] = missing
        notice = review(data) if data and review is not None else None
        if notice and nudges["review"] < 2:
            nudges["review"] += 1
            conv.add_reply(reply)
            conv.add_user(notice)
            print(c(f"    ⟳ {shorten(notice, 200)}", BLUE))
            log.md_add(f"> ⟳ {notice}")
            continue
        st.update(final=visible, json=data, state="ok")
        break

    st["seconds"] = time.perf_counter() - t_start
    st["nudges"] = dict(nudges)
    return st


# ---------------------------------------------------------------------------
# Verification, summary and CSV
# ---------------------------------------------------------------------------
def verify(challenge, st: dict) -> tuple[str, list[tuple[bool, str]]]:
    if challenge is None or challenge.verify is None:
        return "UNVERIFIED", []
    if st["state"] != "ok":
        return "FAIL", [(False, f"Did not finish (state: {st['state']}).")]
    if not st["json"]:
        return "FAIL", [(False, "Did not deliver the final JSON block.")]
    res = challenge.verify(st["json"])
    if challenge.measured or challenge.derived:
        inv = st.get("invented") or []
        res = res + [(not inv, "The results it reports come from its own simulations"
                      + (f" (not: {', '.join(inv)})" if inv else ""))]
    return ("PASS" if res and all(ok for ok, _ in res) else "FAIL"), res


_CODE: str | None = None


def code_version() -> str:
    """Code commit: 'a1b2c3d' (with '+changes' if there are uncommitted changes) when it runs
    from a clone; 'a1b2c3d (installed)' when pip installed it from git; else 'no git'."""
    global _CODE
    if _CODE is None:
        source = (DIR.parent / "pyproject.toml").exists()
        _CODE = (git_commit() if source else installed_commit()) or "no git"
    return _CODE


def git_commit() -> str | None:
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=DIR, text=True,
                           capture_output=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=DIR, text=True, capture_output=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return (h + ("+changes" if dirty.strip() else "")) if h else None


def installed_commit() -> str | None:
    """The commit pip installed this copy from (direct_url.json, PEP 610), if it knows it."""
    try:
        text = importlib.metadata.distribution("vibespice").read_text("direct_url.json")
        commit = (json.loads(text or "{}").get("vcs_info") or {}).get("commit_id") or ""
    except (importlib.metadata.PackageNotFoundError, ValueError, OSError, AttributeError):
        return None
    return f"{commit[:7]} (installed)" if commit else None


def append_csv(row: dict) -> None:
    path = LOGS / "summary.csv"
    if path.exists():       # different columns: keep the old file aside and start another
        with path.open(encoding="utf-8", newline="") as f:
            header = next(csv.reader(f), [])
        if header != list(row):
            old = path.with_name(f"summary_until_{datetime.now():%Y%m%d-%H%M%S}.csv")
            path.rename(old)
            print(c(f"summary.csv had other columns: saved as {old.name}", GREY))
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow({k: (f"{v:.1f}" if isinstance(v, float) else v) for k, v in row.items()})


def run_once(provider: Provider, task, challenge, args, label, num_ctx, n_rep=None,
             heading: str | None = None, review=None) -> dict:
    """One run, verified and logged. heading replaces " · repetition N" after the title;
    review goes to run_agent."""
    log = RunLog(label + (f"_rep{n_rep}" if n_rep else ""))
    tools = [e["function"]["name"] for e in hs.schemas()]
    batch = getattr(args, "batch_kind", "single")
    log.data.update(provider=provider.name, model=provider.model, think=args.think,
                    num_ctx=num_ctx,
                    challenge=challenge.id if challenge else None,
                    start=datetime.now().isoformat(), version=__version__,
                    code=code_version(), batch=batch, tools=tools)
    log.md_add(f"# {challenge.title if challenge else 'Free task'}\n\n"
               f"- Date: {datetime.now():%Y-%m-%d %H:%M}\n- Provider: {provider.label} · "
               f"Model: `{provider.model}`\n"
               f"- Reasoning: {args.think} · Tool mode: {args.mode} · "
               f"Context: {num_ctx or 'server default'}\n- Tools: "
               + ", ".join(tools) + f"\n- Version: {__version__} · Code: {code_version()} · "
               f"Batch: {batch}")
    title = challenge.title if challenge else "Free task"
    rep = heading or (f" · repetition {n_rep}" if n_rep else "")
    print(c(f"\n═══ {title}{rep} ═══", BOLD))
    print(c(f"{provider.name} · model {provider.model} · reasoning: {args.think} · tools: "
            f"{args.mode}" + (f" · context: {num_ctx or 'the server default'}"
                              if provider.supports_num_ctx else ""), GREY))
    final_state, st = "ERROR", None
    try:
        st = run_agent(provider, task, args, log, wants_json=challenge is not None,
                       num_ctx=num_ctx, measured=challenge.measured if challenge else None,
                       derived=challenge.derived if challenge else None, review=review)
        print(c("─" * 60, GREY))
        print(st["final"] or c("(no final answer)", RED))
        print(c("─" * 60, GREY))
        final_state, res = verify(challenge, st)
        log.md_add("## Final answer\n\n" + (st["final"] or "(none)"))
        if res:
            print(c("Independent verification (re-simulated with ngspice):", BOLD))
            md_lines = []
            for ok, text in res:
                print(("  ✅ " if ok else "  ❌ ") + text)
                md_lines.append(("- ✅ " if ok else "- ❌ ") + text)
            log.md_add("## Verification\n\n" + "\n".join(md_lines))
        color = GREEN if final_state == "PASS" else RED if final_state == "FAIL" else YELLOW
        uses = ", ".join(f"{k}×{v}" for k, v in st["tool_use"].items()) or "none"
        if st["tool_errors"]:
            uses += f" ({st['tool_errors']} with errors)"
        print(c(f"Result: {final_state}", color + ";1") +
              f"  ({st['steps']} steps · tools: {uses} · {fmt_dur(st['seconds'])} · "
              f"max input {fmt_tok(st['max_input_tokens'])} tok · total input "
              f"{fmt_tok(st['input_tokens'])} tok"
              + (f" ({fmt_tok(st['cache_read_tokens'])} cached)" if st["cache_read_tokens"]
                 else "")
              + f" · total output {fmt_tok(st['output_tokens'])} tok)")
        if not st["tool_use"]:
            print(c("  ⚠ Used no tools: its numbers do not come from the simulator.", YELLOW))
        for a in st["warnings"]:
            print(c(f"  ⚠ {a}", YELLOW))
        summary = {k: v for k, v in st.items() if k != "final"}
        log.md_add(f"## Result: {final_state}\n\n```\n"
                   f"{json.dumps(summary, ensure_ascii=False, indent=1, default=str)}\n```")
    except APIError as e:
        print(c(f"API ERROR: {e}", RED))
        log.md_add(f"## API ERROR\n\n{e}")
    except KeyboardInterrupt:
        print(c("\nInterrupted by the user.", YELLOW))
        log.md_add("## Interrupted by the user")
        final_state = "INTERRUPTED"
        raise
    finally:
        log.data.update(result=final_state, summary=st)
        log.save_json()
        print(c(f"Log: {tilde(log.md)}", GREY))
        append_csv({
            "date": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "challenge": challenge.id if challenge else "free",
            "provider": provider.name, "model": provider.model, "think": args.think,
            "mode": args.mode,
            "context": (st or {}).get("context") or num_ctx or "", "result": final_state,
            "steps": st["steps"] if st else "", "calls": st["calls"] if st else "",
            "tool_errors": st["tool_errors"] if st else "",
            "seconds": float(st["seconds"]) if st else "",
            "max_input_tokens": st["max_input_tokens"] if st else "",
            "input_tokens": st["input_tokens"] if st else "",
            "cache_read_tokens": st["cache_read_tokens"] if st else "",
            "output_tokens": st["output_tokens"] if st else "",
            "log": log.md.name, "batch": batch, "version": __version__,
            "code": code_version(),
        })
    return {"state": final_state, "st": st}

