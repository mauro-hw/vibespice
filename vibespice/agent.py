# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Mauro Rodriguez Blasco
"""
Iterative LLM + ngspice agent, through Open WebUI.

The model (on your server) thinks and decides; this program, on your computer, runs
ngspice and sends the results back, in a loop, until it gives a final answer. That
answer is then verified independently (by re-simulating) and everything is logged.

This module holds the configuration, the Open WebUI client, the agent loop, the logs and
the verification of one run. The commands live in cli.py.

Standard library only (tested with Python 3.11 to 3.14).
"""
from __future__ import annotations

import csv
import json
import math
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__, config
from . import challenges as C
from . import tools as hs
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
# Open WebUI client (OpenAI-compatible API)
# ---------------------------------------------------------------------------
class APIError(RuntimeError):
    pass


class OWUIClient:
    def __init__(self, url: str, key: str, model: str, ca: str = "", timeout: float = 900):
        self.url = url.rstrip("/")
        self.key = key
        self.model = model
        self.timeout = timeout
        self.ctx = ssl.create_default_context(cafile=ca) if ca else None

    def _request(self, method: str, path: str, body=None, timeout: float | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout,
                                        context=self.ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            hint = ""
            if e.code == 401:
                hint = (" → Key not recognized: check that api_key is copied correctly "
                        "(it starts with sk-) and that you haven't regenerated it.")
            elif e.code == 403:
                hint = (" → No permission: is 'Enable API Keys' turned on in Admin > Settings > "
                        "Authentication? Is 'API Key Endpoint Restrictions' turned off? If the "
                        "key is not an admin key, some queries (status) are not allowed.")
            elif e.code == 404:
                hint = " → Path or model not found. Check url and model in your profile."
            raise APIError(f"HTTP {e.code} at {path}: {detail}{hint}") from None
        except urllib.error.URLError as e:
            raise APIError(f"Can't connect to {self.url} ({e.reason}). Are the URL and port "
                           "right? Can this machine reach the server?") from None
        except TimeoutError:
            raise APIError(f"No reply within {timeout or self.timeout:.0f} s "
                           "(raise timeout in your profile if the model thinks for long).") from None
        except json.JSONDecodeError:
            raise APIError(f"Non-JSON reply at {path}. Does url point to Open WebUI?") \
                from None

    def models(self) -> list[str]:
        data = self._request("GET", "/api/models", timeout=30)
        return [m.get("id", "?") for m in data.get("data", [])]

    def ollama_ps(self) -> list[dict]:
        """Models loaded in Ollama (needs an admin key)."""
        return self._request("GET", "/ollama/api/ps", timeout=30).get("models", [])

    def ollama_version(self) -> str:
        return str(self._request("GET", "/ollama/api/version", timeout=30).get("version", "?"))

    def model_card(self) -> dict:
        """The model's card in Ollama (/api/show): reasoning levels, parameters,
        capabilities... Does not load it on the GPU. Needs an admin key."""
        return self._request("POST", "/ollama/api/show", {"model": self.model}, timeout=30)

    def chat(self, messages, tools=None, options=None) -> dict:
        body = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        if options:
            body["options"] = options
        return self._request("POST", "/api/chat/completions", body)


def make_client(settings: config.Settings, model: str | None = None,
                need_model: bool = True) -> OWUIClient:
    """The client for the configured server. Raises APIError, explained, if the
    configuration is incomplete or (with need_model) there is no model."""
    if settings.problems:
        raise APIError("Incomplete configuration.\n" + config.diagnosis(settings))
    model = model or settings.model
    if need_model and not model:
        raise APIError("No model chosen: set model in your profile or pass --model. "
                       "'vibespice check' lists the models on the server.")
    return OWUIClient(settings.url, settings.api_key, model, settings.ca, settings.timeout)


def loaded_context(client: OWUIClient) -> int | None:
    """num_ctx the server has the model loaded with right now (if it can be known)."""
    try:
        for m in client.ollama_ps():
            if client.model in (m.get("name"), m.get("model")):
                return int(m.get("context_length") or 0) or None
    except (APIError, ValueError, TypeError):
        return None
    return None


# ---------------------------------------------------------------------------
# Reading the model's replies
# ---------------------------------------------------------------------------
_RE_THINK = re.compile(r"<think>(.*?)</think>", re.S)
_RE_DETAILS = re.compile(r'<details type="reasoning".*?>(.*?)</details>', re.S)
_RE_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)


def split_thinking(text: str) -> tuple[str, str]:
    """Separates the reasoning (<think>…</think>) from the visible content."""
    thought = []
    for rx in (_RE_THINK, _RE_DETAILS):
        thought += rx.findall(text)
        text = rx.sub("", text)
    if "</think>" in text:                      # sometimes the opening tag is missing
        before, text = text.split("</think>", 1)
        thought.append(before)
    return text.strip(), "\n".join(p.strip() for p in thought if p.strip())


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


TEXT_MODE_PROMPT = """

# Tools
To use a tool, write exactly one block like this (you can write several):
<tool_call>
{"name": "tool_name", "arguments": {...}}
</tool_call>
Then STOP and wait: you will receive the result inside <tool_response>. Never write the results yourself.

Available tools (JSON Schema):
"""


# Sampling recommended by each family's vendor, (with reasoning, without it), matched by the
# start of the model name. A model not listed here uses the parameters of its Modelfile:
# only 'think' is sent.
PROFILES = {
    "qwen3.8": ({"temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 0.0},
                {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}),
    "qwen3:": ({"temperature": 0.6, "top_p": 0.95, "top_k": 20},
               {"temperature": 0.7, "top_p": 0.8, "top_k": 20}),
}


def think_value(think: str) -> bool | str:
    """--think → Ollama's 'think' field: yes/no, or the level as is (low, medium…)."""
    return {"yes": True, "no": False}.get(think, think)


def model_options(model: str, think: bool | str, num_ctx: int | None) -> dict:
    profile = next((p for prefix, p in PROFILES.items() if model.startswith(prefix)), None)
    op = {"think": think, **(profile[0 if think is not False else 1] if profile else {})}
    if num_ctx:
        op["num_ctx"] = int(num_ctx)
    return op


def resolve_think(client: OWUIClient, requested: str) -> tuple[str | None, str]:
    """Checks --think against the levels the model declares. Returns (value, error).

    'yes' becomes the model's default level if it has levels (qwen3.8: medium), so that the
    logs say which level it reasoned with."""
    try:
        card = client.model_card()
    except APIError:
        return requested, ""        # without an admin key it can't be checked: sent as is
    info = card.get("thinking") or {}
    values = info.get("values") or []
    levels = [v for v in values if isinstance(v, str)]
    if "thinking" not in (card.get("capabilities") or []) or not values:
        return ("no", "") if requested == "no" else \
            (None, f"{client.model} does not reason: use --think no.")
    if requested == "yes":
        return (info["default"] if isinstance(info.get("default"), str) else "yes"), ""
    if requested == "no" and False not in values:
        return None, f"{client.model} does not allow disabling reasoning."
    if requested == "no" or requested in levels:
        return requested, ""
    return None, (f"{client.model} does not support --think {requested}. Options: yes, no"
                  + "".join(f", {n}" for n in levels) + ".")


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


def run_agent(client: OWUIClient, task: str, args, log: RunLog, wants_json: bool,
              num_ctx: int | None, measured: dict | None = None,
              derived: dict | None = None) -> dict:
    native = args.mode == "native"
    offered = hs.schemas()
    system = SYSTEM_PROMPT
    if not native:
        system += TEXT_MODE_PROMPT + json.dumps(
            [e["function"] for e in offered], ensure_ascii=False, indent=1)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    options = model_options(client.model, think_value(args.think), num_ctx)
    tools = offered if native else None
    log.data.update(system=system, task=task, options=options, mode=args.mode)
    log.md_add("## System prompt\n\n" + block(system))
    log.md_add("## Task\n\n" + block(task))

    st = {"steps": 0, "calls": 0, "tool_errors": 0, "tool_use": {},
          "max_input_tokens": 0, "output_tokens": 0, "llm_seconds": 0.0,
          "final": "", "json": None, "state": "max_steps", "warnings": [],
          "context": num_ctx}
    t_start = time.perf_counter()
    last_signatures: list[str] = []
    nudges = {"json": 0, "empty": 0, "loop": 0, "origin": 0}
    numbers: list[float] = []          # values the model got from 'simulate'
    quantities: list[dict] = []        # the same, named and per simulation (derived)
    prev_tokens = 0

    for step in range(1, args.max_steps + 1):
        st["steps"] = step
        t0 = time.perf_counter()
        with Heartbeat(f"{client.model} thinking (step {step})"):
            resp = client.chat(messages, tools, options)
        dt = time.perf_counter() - t0
        st["llm_seconds"] += dt
        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise APIError(f"Unexpected reply from the server: {shorten(json.dumps(resp), 300)}")
        raw = msg.get("content") or ""
        content, thought = split_thinking(raw)
        reasoning = "\n".join(x for x in (msg.get("reasoning_content") or
                                          msg.get("reasoning") or "", thought) if x).strip()
        usage = resp.get("usage") or {}
        t_in = int(usage.get("prompt_tokens") or usage.get("prompt_eval_count") or 0)
        t_out = int(usage.get("completion_tokens") or usage.get("eval_count") or 0)
        speed = usage.get("response_token/s")
        st["max_input_tokens"] = max(st["max_input_tokens"], t_in)
        st["output_tokens"] += t_out

        # If the model was not loaded at the start, it is now: read the context the server
        # loaded it with (read-only; the options sent do not change)
        if step == 1 and not st["context"]:
            st["context"] = loaded_context(client)
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
        native_calls = msg.get("tool_calls") or []
        calls = []
        for tc in native_calls:
            fn = tc.get("function", {})
            a = fn.get("arguments", {})
            if isinstance(a, str):
                try:
                    a_obj = json.loads(a) if a.strip() else {}
                except json.JSONDecodeError:
                    # Open WebUI parses this JSON again: never send a broken one back
                    a_obj, a = a, "{}"
            else:
                a_obj, a = a, json.dumps(a, ensure_ascii=False)
            calls.append({"id": tc.get("id") or f"call_{step}_{len(calls)}",
                          "name": fn.get("name", "?"), "args": a_obj, "args_str": a})
        text_calls = [] if calls else calls_from_text(content)
        visible = _RE_TOOL_CALL.sub("", content).strip() if text_calls else content

        line = (f"[{step}] model {fmt_dur(dt)} · input {fmt_tok(t_in)} tok · output "
                f"{fmt_tok(t_out)} tok" + (f" ({speed} tok/s)" if speed not in (None, "N/A")
                                           else ""))
        print(c(line, GREY))
        if args.show_thinking and reasoning:
            print(c("    💭 " + shorten(reasoning, 1500).replace("\n", "\n       "), GREY))
        if visible and (calls or text_calls):
            print("    " + shorten(visible, 300).replace("\n", "\n    "))

        log.md_add(f"## Step {step}\n\n{fmt_dur(dt)} · input {t_in} tok · output {t_out} tok")
        if reasoning:
            log.md_add("<details><summary>Reasoning</summary>\n\n" + block(reasoning)
                       + "\n\n</details>")
        if visible:
            log.md_add(visible)
        log.event(step=step, seconds=dt, usage=usage, reasoning=reasoning,
                  content=raw, tool_calls=native_calls)

        every = calls or [dict(x, id=None, args_str=json.dumps(x["args"], ensure_ascii=False)
                               if x.get("args") is not None else "")
                          for x in text_calls]
        if every:
            if calls:
                messages.append({"role": "assistant", "content": content,
                                 "tool_calls": [{"id": l["id"], "type": "function",
                                                 "function": {"name": l["name"],
                                                              "arguments": l["args_str"]
                                                              if isinstance(l["args_str"], str)
                                                              and l["args_str"].strip()
                                                              else "{}"}}
                                                for l in calls]})
            else:
                messages.append({"role": "assistant", "content": content})
            replies = []
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
                    messages.append({"role": "tool", "tool_call_id": l["id"],
                                     "name": l["name"], "content": result})
                else:
                    replies.append(f"<tool_response>\n{result}\n</tool_response>")
            if replies:
                messages.append({"role": "user", "content": "\n".join(replies)})

            # Loop detector: the same call with the same arguments 3 times
            last_signatures.append(json.dumps([(l["name"], l["args"]) for l in every],
                                              sort_keys=True, ensure_ascii=False, default=str))
            if len(last_signatures) >= 3 and len(set(last_signatures[-3:])) == 1 \
                    and nudges["loop"] < 2:
                nudges["loop"] += 1
                notice = ("You have repeated the same call several times with the same result. "
                          "Change approach or, if you already have what you need, give the "
                          "final answer.")
                messages.append({"role": "user", "content": notice})
                print(c("    ⚠ loop detected: asking it to change approach", YELLOW))
                log.md_add(f"> ⚠ {notice}")
            continue

        # No calls: final answer (or nearly)
        if not visible:
            if nudges["empty"] < 2:
                nudges["empty"] += 1
                messages.append({"role": "assistant", "content": ""})
                messages.append({"role": "user", "content": "Your reply was empty. Continue: use "
                                 "a tool or give the final answer."})
                print(c("    ⚠ empty reply, asking it to continue", YELLOW))
                continue
            st["state"] = "empty"
            break
        data = extract_final_json(visible) if wants_json else None
        if wants_json and data is None and nudges["json"] < 1:
            nudges["json"] += 1
            messages.append({"role": "assistant", "content": visible})
            messages.append({"role": "user", "content": "The final ```json block with the "
                             "requested fields is missing. Repeat your final answer including "
                             "it; there is no need to simulate again."})
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
            messages.append({"role": "assistant", "content": visible})
            messages.append({"role": "user", "content": notice})
            print(c(f"    ⚠ results without origin in its simulations ({', '.join(missing)}): "
                    "asking it to measure them", YELLOW))
            log.md_add(f"> ⚠ {notice}")
            continue
        st["invented"] = missing
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
    """Code commit ('a1b2c3d', with '+changes' if there are uncommitted changes)."""
    global _CODE
    if _CODE is None:
        try:
            h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=DIR, text=True,
                               capture_output=True, timeout=5).stdout.strip()
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                   cwd=DIR, text=True, capture_output=True, timeout=5).stdout
            _CODE = (h + ("+changes" if dirty.strip() else "")) if h else "no git"
        except (OSError, subprocess.SubprocessError):
            _CODE = "no git"
    return _CODE


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


def run_once(client, task, challenge, args, label, num_ctx, n_rep=None) -> dict:
    log = RunLog(label + (f"_rep{n_rep}" if n_rep else ""))
    tools = [e["function"]["name"] for e in hs.schemas()]
    batch = getattr(args, "batch_kind", "single")
    log.data.update(model=client.model, think=args.think, num_ctx=num_ctx,
                    challenge=challenge.id if challenge else None,
                    start=datetime.now().isoformat(), version=__version__,
                    code=code_version(), batch=batch, tools=tools)
    log.md_add(f"# {challenge.title if challenge else 'Free task'}\n\n"
               f"- Date: {datetime.now():%Y-%m-%d %H:%M}\n- Model: `{client.model}`\n"
               f"- Reasoning: {args.think} · Tool mode: {args.mode} · "
               f"Context: {num_ctx or 'server default'}\n- Tools: "
               + ", ".join(tools) + f"\n- Version: {__version__} · Code: {code_version()} · "
               f"Batch: {batch}")
    title = challenge.title if challenge else "Free task"
    rep = f" · repetition {n_rep}" if n_rep else ""
    print(c(f"\n═══ {title}{rep} ═══", BOLD))
    print(c(f"model {client.model} · reasoning: {args.think} · tools: {args.mode}"
            f" · context: {num_ctx or 'the server default'}", GREY))
    final_state, st = "ERROR", None
    try:
        st = run_agent(client, task, args, log, wants_json=challenge is not None,
                       num_ctx=num_ctx, measured=challenge.measured if challenge else None,
                       derived=challenge.derived if challenge else None)
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
              f"max input {fmt_tok(st['max_input_tokens'])} tok · total output "
              f"{fmt_tok(st['output_tokens'])} tok)")
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
            "model": client.model, "think": args.think, "mode": args.mode,
            "context": (st or {}).get("context") or num_ctx or "", "result": final_state,
            "steps": st["steps"] if st else "", "calls": st["calls"] if st else "",
            "tool_errors": st["tool_errors"] if st else "",
            "seconds": float(st["seconds"]) if st else "",
            "max_input_tokens": st["max_input_tokens"] if st else "",
            "output_tokens": st["output_tokens"] if st else "",
            "log": log.md.name, "batch": batch, "version": __version__,
            "code": code_version(),
        })
    return {"state": final_state, "st": st}

