#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Mauro Rodriguez Blasco
"""
Iterative LLM + ngspice agent, through Open WebUI.

The model (on your server) thinks and decides; this script, on your computer, runs
ngspice and sends the results back, in a loop, until it gives a final answer. That
answer is then verified independently (by re-simulating) and everything is logged.

Quick use:
    python3 spice_agent.py --check            # connection, ngspice, model, tools
    python3 spice_agent.py --selftest         # local test without AI
    python3 spice_agent.py --list             # available challenges
    python3 spice_agent.py --challenge 2      # run a challenge
    python3 spice_agent.py --challenge 2 --repeat 5 --think no
    python3 spice_agent.py --task "Design a 24 V to 3.3 V divider with E12"

Standard library only (tested with Python 3.11 to 3.14).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import statistics
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

__version__ = "0.1.0.dev0"

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))

# ---------------------------------------------------------------------------
# Configuration: agent.conf (KEY=value) and environment variables (which take priority)
# ---------------------------------------------------------------------------
DEFAULT_CONF = {
    "OWUI_URL": "",            # e.g. http://localhost:3000
    "OWUI_API_KEY": "",        # Settings > Account > API Keys (starts with sk-)
    "OWUI_MODEL": "qwen3:32b",
    "OWUI_CA": "",             # CA certificate if your Open WebUI uses its own HTTPS
    "NGSPICE": "ngspice",
    "LLM_TIMEOUT": "900",      # seconds per model reply
}


CONF_FILE = DIR / "agent.conf"
_RE_CONF = re.compile(r"^(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*[=:]\s*(.*)$")


def load_config() -> dict:
    """Accepts KEY=value, KEY = value, KEY: value and 'export KEY=value'."""
    conf = dict(DEFAULT_CONF)
    if CONF_FILE.exists():
        text = CONF_FILE.read_text(encoding="utf-8-sig", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _RE_CONF.match(line)
            if m:
                conf[m.group(1)] = m.group(2).split(" #")[0].strip().strip('"').strip("'")
    for k in list(conf):
        if os.environ.get(k):
            conf[k] = os.environ[k]
    return conf


def config_diagnosis(conf: dict) -> str:
    """Explains why the configuration is missing."""
    lines = [f"Looking for the configuration in: {CONF_FILE}"]
    if not CONF_FILE.exists():
        others = sorted(p.name for p in DIR.glob("agent*"))
        lines.append("  ❌ That file does NOT exist. Create it with:  cp agent.conf.example "
                     "agent.conf  and fill in OWUI_URL and OWUI_API_KEY")
        if others:
            lines.append(f"     (the folder contains: {', '.join(others)})")
        return "\n".join(lines)
    lines.append("  ✅ the file exists")
    for key in ("OWUI_URL", "OWUI_API_KEY"):
        v = conf.get(key, "")
        if not v:
            lines.append(f"  ❌ {key} is empty or I can't find it. It must be a line "
                         f"without # in front, like this:  {key}=value")
        elif key == "OWUI_URL" and not v.startswith(("http://", "https://")):
            lines.append(f"  ❌ OWUI_URL must start with http:// or https:// (now: {v})")
        else:
            sample = v if key == "OWUI_URL" else f"{v[:5]}…{v[-3:]}"
            lines.append(f"  ✅ {key} = {sample}")
    return "\n".join(lines)


CONF = load_config()
os.environ["NGSPICE"] = CONF["NGSPICE"]   # before importing the tools

import spice_tools as hs   # noqa: E402
import challenges as C     # noqa: E402

# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


GREEN, RED, YELLOW, BLUE, GREY, BOLD = "32", "31", "33", "36", "90", "1"


def shorten(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n].rstrip() + " …"


def fmt_tok(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt_dur(s: float) -> str:
    return f"{s:.1f} s" if s < 90 else f"{int(s // 60)} min {int(s % 60)} s"


class Heartbeat:
    """While waiting (for the model or a tool), rewrites a line with the elapsed time every few
    seconds. Only in a terminal: it writes nothing to files or pipes."""

    def __init__(self, text: str, every: float = 5.0):
        self.text, self.every = text, every
        self.active = sys.stdout.isatty()
        self._stop = threading.Event()

    def __enter__(self):
        if self.active:
            self._t0 = time.perf_counter()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            line = f"    ⏳ {self.text}… {fmt_dur(time.perf_counter() - self._t0)}"
            print("\r" + c(line, GREY) + "\033[K", end="", flush=True)

    def __exit__(self, *exc) -> bool:
        if self.active:
            self._stop.set()
            self._thread.join()
            print("\r\033[K", end="", flush=True)
        return False


def notify_done(title: str, text: str) -> None:
    """Desktop notification when a batch finishes (if there is a desktop and notify-send)."""
    if os.environ.get("VIBESPICE_NO_NOTIFY") or not (os.environ.get("DISPLAY")
                                                     or os.environ.get("WAYLAND_DISPLAY")):
        return
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "vibespice", title, text],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def on_mains_power() -> bool:
    """Is the laptop plugged in? (A machine without a battery, like a Raspberry Pi, counts as yes.)"""
    has_battery = False
    for d in Path("/sys/class/power_supply").glob("*"):
        try:
            kind = (d / "type").read_text().strip()
            if kind == "Battery":
                has_battery = True
            elif kind in ("Mains", "USB") and (d / "online").read_text().strip() == "1":
                return True
        except OSError:
            continue
    return not has_battery


def keep_awake() -> str:
    """Stops the computer from suspending by itself while the agent works; says what it did.

    Idle suspend, always; lid-close suspend, only on mains power (on battery, a closed laptop
    still working could end up in a backpack). The screen still locks and you can suspend by
    hand. Each inhibitor is held by a process that ends with the agent (tail --pid), so it
    never stays behind even if the agent dies."""
    wait = ["tail", f"--pid={os.getpid()}", "-f", "/dev/null"]
    commands = []
    if "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "") \
            and shutil.which("gnome-session-inhibit"):
        commands.append(["gnome-session-inhibit", "--inhibit", "suspend", "--app-id", "vibespice",
                         "--reason", "vibespice batch running", *wait])
    elif shutil.which("systemd-inhibit"):
        commands.append(["systemd-inhibit", "--what=sleep", "--who=vibespice",
                         "--why=vibespice batch running", *wait])
    lid = on_mains_power() and shutil.which("systemd-inhibit")
    if lid:
        commands.append(["systemd-inhibit", "--what=handle-lid-switch", "--who=vibespice",
                         "--why=vibespice batch running", *wait])
    done = 0
    for command in commands:
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            done += 1
        except OSError:
            pass
    if not done:
        return ""
    return ("The computer will not suspend by itself until it finishes"
            + ("; with the charger plugged in, not when closing the lid either" if lid
               else "; on battery, closing the lid does suspend it") + ".")


# ---------------------------------------------------------------------------
# Open WebUI client (OpenAI-compatible API)
# ---------------------------------------------------------------------------
class APIError(RuntimeError):
    pass


class OWUIClient:
    def __init__(self, url: str, key: str, model: str, ca: str = "", timeout: float = 900):
        if not url or not key or not url.startswith(("http://", "https://")):
            raise APIError("Incomplete configuration.\n" + config_diagnosis(CONF))
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
                hint = (" → Key not recognized: check that OWUI_API_KEY is copied correctly "
                        "(it starts with sk-) and that you haven't regenerated it.")
            elif e.code == 403:
                hint = (" → No permission: is 'Enable API Keys' turned on in Admin > Settings > "
                        "Authentication? Is 'API Key Endpoint Restrictions' turned off? If the "
                        "key is not an admin key, some queries (--status) are not allowed.")
            elif e.code == 404:
                hint = " → Path or model not found. Check OWUI_URL and OWUI_MODEL."
            raise APIError(f"HTTP {e.code} at {path}: {detail}{hint}") from None
        except urllib.error.URLError as e:
            raise APIError(f"Can't connect to {self.url} ({e.reason}). Are the URL and port "
                           "right? Can this machine reach the server?") from None
        except TimeoutError:
            raise APIError(f"No reply within {timeout or self.timeout:.0f} s "
                           "(raise LLM_TIMEOUT if the model thinks for long).") from None
        except json.JSONDecodeError:
            raise APIError(f"Non-JSON reply at {path}. Does OWUI_URL point to Open WebUI?") \
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
        folder = DIR / "logs"
        folder.mkdir(exist_ok=True)
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
    path = DIR / "logs" / "summary.csv"
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
        print(c(f"Log: {log.md.relative_to(DIR)}", GREY))
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


# ---------------------------------------------------------------------------
# Time-limited batches and estimates
# ---------------------------------------------------------------------------
def estimate(done: int, total: int | None, t0: float, limit: float | None = None,
             name: str = "runs") -> str:
    """Progress line after each iteration: time spent and minutes left.

    Batches are measured with wall-clock time (time.time), not a monotonic clock: the latter
    does not advance while the computer is suspended, and the batch would run past its end."""
    elapsed = time.time() - t0
    avg = elapsed / done
    txt = (f"⏱ {done}" + (f"/{total}" if total else "") + f" {name} in {elapsed / 60:.0f} min "
           f"({avg / 60:.1f} min average)")
    left = total - done if total else None
    if limit:                             # only starts another one if it can finish it
        by_time = int(max(0.0, limit - elapsed) // avg) if avg else 0
        left = by_time if left is None else min(left, by_time)
    if left is not None:
        end = datetime.now() + timedelta(seconds=left * avg)
        txt += f" · ≈ {left * avg / 60:.0f} min left · end ≈ {end:%H:%M}"
    return txt


def fits_another(elapsed: float, avg: float, limit: float | None) -> bool:
    """Is there time for another iteration before the limit, given the average so far?"""
    return not limit or elapsed + avg <= limit


def limit_notice(limit: float, what: str) -> None:
    end = datetime.now() + timedelta(seconds=limit)
    print(c(f"⏱ Time limit: {limit / 60:.0f} min → finishes at the latest around "
            f"{end:%H:%M} (does not start {what} if there is no time to finish it)", GREY))


def history_seconds(challenge_id: str, model: str, think: str) -> list[float]:
    """Durations of similar earlier runs, taken from logs/summary*.csv."""
    out = []
    for path in sorted((DIR / "logs").glob("summary*.csv")):
        try:
            with path.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if (row.get("challenge") == challenge_id and row.get("model") == model
                            and row.get("think") == think
                            and row.get("result") not in ("ERROR", "INTERRUPTED")):
                        try:
                            out.append(float(row["seconds"]))
                        except (KeyError, ValueError, TypeError):
                            pass
        except OSError:
            pass
    return out


def estimate_before(ids: list[str], times: int | None, model: str, think: str,
                    limit: float | None) -> None:
    """Before starting: how long each iteration and the batch usually take, from your history."""
    medians, no_data = {}, []
    for i in ids:
        h = history_seconds(i, model, think)
        if h:
            medians[i] = statistics.median(h)
        else:
            no_data.append(i)
    if not medians:
        return
    txt = "⏱ Based on your history, each run takes ≈ " + ", ".join(
        f"{m / 60:.0f} min (challenge {i})" for i, m in medians.items())
    total = sum(medians.values()) * times if times and not no_data else None
    if limit:
        total = min(total, limit) if total else limit
    if total:
        end = datetime.now() + timedelta(seconds=total)
        txt += f" → this batch, ≈ {total / 60:.0f} min (end ≈ {end:%H:%M})"
    if no_data:
        txt += f" · no history for challenge {', '.join(no_data)}"
    print(c(txt, GREY))


def duration(text: str) -> float:
    """'2h', '90m', '1h30', '45' (minutes) or '30s' → seconds."""
    t = text.lower().replace(" ", "")
    m = re.fullmatch(r"(\d+)s", t)
    if m:
        s = float(m.group(1))
    else:
        m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)(?:m|min)?)?", t)
        s = (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) * 60.0 if m else 0
    if s <= 0:
        raise argparse.ArgumentTypeError(f"invalid duration: '{text}' (e.g. 2h, 90m or 1h30)")
    return s


# ---------------------------------------------------------------------------
# Auxiliary commands
# ---------------------------------------------------------------------------
def check(client: OWUIClient | None) -> int:
    failures = 0
    print(c("1. ngspice", BOLD))
    v = hs.ngspice_version()
    if v:
        r = hs.simulate_data("V1 in 0 DC 12\nR1 in out 14k\nR2 out 0 10k\n.op")
        ok = abs(r["op"].get("v(out)", 0) - 5.0) < 1e-6
        print(("  ✅ " if ok else "  ❌ ") + f"{v}; test divider: v(out) = "
              f"{r['op'].get('v(out)')} V")
        failures += not ok
    else:
        print(f"  ❌ '{hs.NGSPICE}' not found. Install ngspice (e.g. sudo apt install ngspice, "
              "sudo dnf install ngspice or brew install ngspice) or set its path in NGSPICE.")
        failures += 1
    if client is None:
        return failures + 1
    print(c("2. Open WebUI", BOLD))
    print(f"  URL: {client.url} · key: {client.key[:5]}…{client.key[-3:]}")
    try:
        mods = client.models()
        print(f"  ✅ connected; {len(mods)} models visible to this key")
        if client.model in mods:
            print(f"  ✅ model '{client.model}' is available")
        else:
            print(f"  ❌ '{client.model}' is not listed. Models: {', '.join(mods[:15])}")
            failures += 1
    except APIError as e:
        print(f"  ❌ {e}")
        return failures + 1
    print(c("3. Ollama (through Open WebUI, needs an admin key)", BOLD))
    try:
        print(f"  Ollama {client.ollama_version()}")
        loaded = client.ollama_ps()
        if not loaded:
            print("  No model loaded on the GPU right now.")
        for m in loaded:
            vram = (m.get("size_vram") or 0) / 1e9
            print(f"  loaded: {m.get('name')} · context {m.get('context_length', '?')} tokens · "
                  f"{vram:.1f} GB in VRAM · expires {str(m.get('expires_at', '?'))[:19]}")
    except APIError as e:
        print(f"  (not available: {shorten(str(e), 120)})")
    try:
        f = client.model_card()
        det, info = f.get("details") or {}, f.get("model_info") or {}
        ctx = next((v for k, v in info.items() if k.endswith(".context_length")), "?")
        cap = f.get("capabilities") or []
        th = f.get("thinking") or {}
        levels = ", ".join("no" if v is False else "yes" if v is True else str(v)
                           for v in th.get("values") or []) or "does not reason"
        default = th.get("default")
        print(f"  {client.model}: {det.get('parameter_size', '?')} "
              f"{det.get('quantization_level', '')} · native context {ctx}"
              f" · tools: {'yes' if 'tools' in cap else 'no'}"
              f" · images: {'yes' if 'vision' in cap else 'no'}")
        print(f"  reasoning (--think): {levels}"
              + (f" · default {'yes' if default is True else default}" if default is not None
                 else ""))
        profile = next((pr for pr in PROFILES if client.model.startswith(pr)), None)
        print("  sampling: " + (f"agent profile '{profile}'" if profile else
                                "the Modelfile's (the agent does not know this family)"))
    except APIError as e:
        print(f"  (model card not available: {shorten(str(e), 120)})")
    print(c("4. Chat test", BOLD))
    try:
        t0 = time.perf_counter()
        r = client.chat([{"role": "user", "content": "Reply with just the word: OK"}],
                        options=model_options(client.model, False, None))
        text, _ = split_thinking(r["choices"][0]["message"].get("content") or "")
        if text:
            print(f"  ✅ reply '{shorten(text, 60)}' in {fmt_dur(time.perf_counter() - t0)}")
        else:
            print("  ⚠ empty reply (the model answers but with no visible text)")
    except (APIError, KeyError, IndexError) as e:
        print(f"  ❌ {e}")
        return failures + 1
    print(c("5. Tool call test (native mode)", BOLD))
    try:
        t0 = time.perf_counter()
        r = client.chat([{"role": "user", "content": "What is 1234 * 5678? Use the calculate "
                          "tool; do not compute it yourself."}],
                        tools=[e for e in hs.SCHEMAS if e["function"]["name"] == "calculate"],
                        options=model_options(client.model, False, None))
        m = r["choices"][0]["message"]
        tc = m.get("tool_calls") or []
        text, _ = split_thinking(m.get("content") or "")
        if tc:
            print(f"  ✅ requested {tc[0]['function']['name']}"
                  f"({tc[0]['function'].get('arguments')}) in "
                  f"{fmt_dur(time.perf_counter() - t0)} → native mode works")
        elif calls_from_text(text):
            print("  ⚠ wrote the call as text instead of using tool_calls → the agent "
                  "understands it anyway, but also try --mode text")
        else:
            print(f"  ❌ did not call the tool; replied: '{shorten(text, 100)}'. "
                  "Try --mode text.")
            failures += 1
    except (APIError, KeyError, IndexError) as e:
        print(f"  ❌ {e}")
        failures += 1
    try:
        for m in client.ollama_ps():
            if client.model in (m.get("name"), m.get("model")):
                print(f"  The server has it loaded with a context of "
                      f"{m.get('context_length', '?')} tokens and "
                      f"{(m.get('size_vram') or 0) / 1e9:.1f} GB in VRAM.")
    except APIError:
        pass
    print(c("\nAll good." if not failures else f"\n{failures} check(s) with problems.",
            GREEN if not failures else RED))
    return failures


def selftest() -> int:
    """Tests tools and verifiers without AI (validates your ngspice installation)."""
    failures = 0

    def ok_if(cond: bool, text: str):
        nonlocal failures
        print(("  ✅ " if cond else "  ❌ ") + text)
        failures += not cond

    print(c("Tools", BOLD))
    ok_if(hs.ngspice_version() is not None, f"ngspice found: {hs.ngspice_version()}")
    d = hs.simulate_data("V1 in 0 DC 12\nR1 in out 18k\nR2 out 0 13k\n.op")
    ok_if(abs(d["op"].get("v(out)", 0) - 5.032258) < 1e-5,
          f"simulate: v(out) = {d['op'].get('v(out)')}")
    d = hs.simulate_data("V1 in 0 DC 0 PULSE(0 1 0 1n 1n 1 2)\nR1 in out 1k\nC1 out 0 1u\n"
                         ".tran 10u 5m\n.meas tran tau TRIG v(out) VAL=0.001 RISE=1 "
                         "TARG v(out) VAL=0.63212 RISE=1")
    ok_if(abs(d["meas"].get("tau", 0) - 1e-3) < 2e-5,
          f".meas in a transient: τ = {d['meas'].get('tau')} s (≈ 1 ms)")
    r = hs.tolerance_data("V1 in 0 DC 12\nR1 in out 14k\nR2 out 0 10k\n.op",
                          {"R1": 1, "R2": 1}, "v(out)", "corners", target_value=5)
    ok_if(abs(r["worst_error_pct"] - 1.1686) < 0.001,
          f"corners: worst case {r['worst_error_pct']:.4f} % (≈ 1.1686 %)")
    r = hs.tolerance_data("V1 in 0 DC 12\nR1 in out 14k\nR2 out 0 10k\n.op",
                          {"R1": 2, "R2": 2}, "v(out)", "montecarlo", samples=400,
                          distribution="uniform", target_value=5, error_limit_pct=2)
    ok_if(95 <= r["yield_pct"] <= 100, f"Monte Carlo: yield {r['yield_pct']:.1f} % (≈ 98 %)")
    r = hs.tolerance_data("V1 in 0 DC 0 PULSE(0 1 0 1n 1n 1 2)\nR1 in out 1k\nC1 out 0 1u\n"
                          ".tran 10u 5m\n.meas tran tau TRIG v(out) VAL=0.001 RISE=1 "
                          "TARG v(out) VAL=0.63212 RISE=1", {"R1": 5, "C1": 10}, "tau",
                          "corners")
    ok_if(abs(r["max"] / 1e-3 - 1.155) < 0.01,
          f"tolerances on a .meas: τ max {r['max'] * 1e3:.4f} ms (≈ 1.155 ms)")
    ok_if("11k" in hs.standard_values("11.1k", "E96"), "standard_values E96")
    ok_if(hs.calculate("12*10k/(14k+10k)").endswith("= 5"), "calculate with SPICE suffixes")
    s = hs.simulate("V1 a 0 DC 9\nR1 a b 7.2k\nR2 b 0 4.7k\nR3 b 0 14k\n.op")
    ok_if("R1 = 7.2k: not in any" in s and "R2 = 4.7k: E3, E6, E12, E24\n" in s
          and "R3 = 14k: E48, E96" in s, "simulate shows the E series of each value")
    d = hs.simulate_data("V1 a 0 AC 1\nR1 a b 1k\nC1 b 0 1u\n.ac dec 10 100 100\n"
                         ".meas ac f1 WHEN mag(v(b)/v(a))=0.7")
    ok_if("f1" not in d["meas"] and "f1" in d["meas_failed"]
          and any("vm(out)" in a for a in d["warnings"]),
          "a .meas that ngspice rejects gives no false value and comes with a syntax hint")
    d = hs.simulate_data("V1 a 0 AC 1\nR1 a b 1k\nC1 b 0 1u\n.ac dec 10 1 1k\n"
                         ".meas ac f1 WHEN vm(b)=0.7071068\n.meas ac f2 WHEN vm(b)=1/sqrt(2)\n.op")
    ok_if(abs(d["meas"].get("f1", 0) - 159.15) < 0.5 and "f2" in d["meas_failed"]
          and any(".op" in a for a in d["warnings"])
          and any("must be a number" in a for a in d["warnings"]),
          "an .op next to .ac does not break the .meas, and .meas errors come with advice")
    ok_if(hs.simulate("V1 a 0 1\n.control\nop\n.endc").startswith("ERROR"), "blocks .control")
    d = hs.simulate_data("VCC vcc 0 DC 12\nRB vcc b 4.7MEG\nRC vcc c 4.7k\nQ1 c b 0 QN\n"
                         "RB2 vcc b2 10k\nRC2 vcc c2 1k\nQ2 c2 b2 0 QN\n"
                         ".model QN NPN(IS=6.734f BF=416.4 VAF=74.03)\n.op")
    q1, q2 = d["devices"].get("q1", {}), d["devices"].get("q2", {})
    ok_if(q1.get("region") == "active" and q2.get("region") == "saturation" and not d["errors"]
          and abs(q1.get("vce", 0) - (12 - 4.7e3 * q1.get("ic", 0))) < 1e-3
          and 1e-3 * 0.99 <= q1.get("ic", 0) * 1 < 5e-3,
          "simulate gives Ic, Vce and region of each transistor (active / saturation)")
    # A real mistake seen in challenge 7: base and collector swapped, with numbered nodes
    s = hs.simulate("Vcc 1 0 DC 12\nR1 1 2 91k\nR2 2 0 18k\nRC 1 3 4.7k\nRE 4 0 1.2k\n"
                    f"Q1 2 3 4 Q2N3904\n{C.MODEL_2N3904}\n.op")
    good = hs.simulate("VCC vcc 0 DC 12\nR1 vcc b 91k\nR2 b 0 18k\nRC vcc c 4.7k\nRE e 0 1.2k\n"
                       f"Q1 c b e Q2N3904\n{C.MODEL_2N3904}\n.op\nRX e x 1k")
    ok_if("Q1 (NPN): collector → node 2 · base → node 3 · emitter → node 4" in s
          and "R1: node 1 (+ of Vcc) – node 2 (collector of Q1)" in s
          and "numbered nodes (1, 2, 3, 4)" in s and "Q1 is in saturation" in s
          and "swapped terminals" in s
          and "R1: node vcc (+ of VCC) – node b (base of Q1)" in good
          and "node x (floating" in good and "numbered" not in good
          and "saturation" not in good,
          "simulate describes the connections and warns about numbered nodes and saturation")
    n7 = ("VCC vcc 0 DC 12\nR1 vcc b 4.7k\nR2 b 0 7.5k\nRC vcc c 240\nRE e 0 6.8k\n"
          f"Q1 c b e Q2N3904\n{C.MODEL_2N3904}\n.op")
    m = [hs.quantities_of(hs.simulate_data(n)) for n in (n7, n7.replace("BF=416.4", "BF=100"))]
    der = {"ic_change_pct": "ic"}
    ok_if(not without_origin({"ic_change_pct": -1.01}, {}, [], der, m)
          and without_origin({"ic_change_pct": 7.8}, {}, [], der, m)
          and without_origin({"ic_change_pct": -1.01}, {}, [], der, m[:1]),
          "origin check: the Ic change must come from two of its own simulations")
    # Real case: both circuits (nominal BF and BF = 100) in one netlist. Done wrong: sources in
    # parallel and two .model with the same name. Done right: separate nodes and models
    q100 = C.MODEL_2N3904.replace("Q2N3904", "QB").replace("BF=416.4", "BF=100")
    bad = (n7.replace("\n.op", "") + "\nVCC2 vcc 0 DC 12\nR1B vcc b2 4.7k\nR2B b2 0 7.5k\n"
           "RC2 vcc c2 240\nRE2 e2 0 6.8k\nQ2 c2 b2 e2 Q2N3904\n"
           + C.MODEL_2N3904.replace("BF=416.4", "BF=100") + "\n.op")
    good = (n7.replace("\n.op", "") + "\nVCC2 vcc2 0 DC 12\nR1B vcc2 b2 4.7k\nR2B b2 0 7.5k\n"
            f"RC2 vcc2 c2 240\nRE2 e2 0 6.8k\nQ2 c2 b2 e2 QB\n{q100}\n.op")
    s = hs.simulate(bad)
    m2 = [hs.quantities_of(hs.simulate_data(good))]
    ok_if("Q1: no solution" in s and "VCC and VCC2 are voltage sources in parallel" in s
          and "There are 2 .model statements named q2n3904" in s and "active region" not in s
          and not without_origin({"ic_change_pct": -1.0}, {}, [], der, m2),
          "simulate warns about parallel sources and repeated models, and does not accept a "
          "solution with nan; the change is valid between two transistors of the same netlist")
    ok_if(not fits_another(40 * 60, 13 * 60, 45 * 60) and fits_another(30 * 60, 13 * 60, 45 * 60)
          and duration("1h30") == 5400 and duration("45") == 2700 and duration("30s") == 30,
          "time-limited batches: does not start an iteration that does not fit; durations")

    print(c("Challenge verifiers", BOLD))
    for cid, ch in C.CHALLENGES.items():
        if ch.verify is None:
            continue
        good = ch.verify(C.CORRECT_REFERENCES[cid])
        wrong = ch.verify(C.WRONG_REFERENCES[cid])
        ok_if(all(ok for ok, _ in good) and not all(ok for ok, _ in wrong),
              f"challenge {cid}: accepts the correct solution and rejects the wrong one")
        if not all(ok for ok, _ in good):
            for ok, t in good:
                print(("       ✅ " if ok else "       ❌ ") + t)
    print(c("\nSelf-test passed." if not failures else f"\n{failures} failure(s).",
            GREEN if not failures else RED))
    return failures


def list_challenges() -> None:
    print(c("Available challenges (python3 spice_agent.py --challenge N):", BOLD))
    for cid, ch in C.CHALLENGES.items():
        print(f"  {cid}  {ch.title}  " + c(f"[{', '.join(ch.tags)}]", GREY))
    print(c("\nSolutions are in challenges.py and in the README (the model never sees them).",
            GREY))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
MENU = [
    ("Check the connection and the tools", ["--check"]),
    ("Show the list of challenges", ["--list"]),
    ("Run a challenge once", ["--challenge", "{challenge}"]),
    ("Measure reliability: repetitions with a time limit",
     ["--challenge", "{challenge}", "--repeat", "{times}", "--time-limit", "{time}"]),
    ("Analyze the saved runs", None),
    ("Local test without AI (self-test)", ["--selftest"]),
]


def menu(ask=input) -> list[str] | None:
    """No arguments and in a terminal: guided menu that shows the command it will run."""
    print(c("vibespice — what do you want to do?", BOLD))
    for i, (text, _) in enumerate(MENU, 1):
        print(f"  {i}. {text}")
    print(c("  0. Quit   (all options: python3 spice_agent.py -h)", GREY))
    try:
        choice = ask("Option: ").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            return None
        text, template = MENU[int(choice) - 1]
        if template is None:
            subprocess.run([sys.executable, str(DIR / "analyze_logs.py")])
            return None
        values = {}
        if "{challenge}" in template:
            for cid, ch in C.CHALLENGES.items():
                print(c(f"     {cid}  {ch.title}", GREY))
            values["challenge"] = ask("Challenge [6]: ").strip() or "6"
        if "{times}" in template:
            values["times"] = ask("How many repetitions at most? [5]: ").strip() or "5"
        if "{time}" in template:
            values["time"] = ask("For how long? (e.g. 45m, 1h30) [45m]: ").strip() or "45m"
            duration(values["time"])
        argv = [a.format(**values) for a in template]
        print(c("About to run:  python3 spice_agent.py " + " ".join(argv), BLUE))
        print(c("(next time you can type it directly)", GREY))
        if ask("Go ahead? [Y/n]: ").strip().lower() in ("n", "no"):
            return None
        return argv
    except (EOFError, KeyboardInterrupt, argparse.ArgumentTypeError) as e:
        if isinstance(e, argparse.ArgumentTypeError):
            print(c(str(e), RED))
        return None


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty() and sys.stdout.isatty():
        argv = menu()
        if argv is None:
            return 0
    p = argparse.ArgumentParser(description="LLM + ngspice agent through Open WebUI")
    p.add_argument("--version", action="version", version=f"vibespice {__version__}")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="checks ngspice, the connection and the model")
    g.add_argument("--status", action="store_true", help="shows which models Ollama has loaded")
    g.add_argument("--selftest", action="store_true", help="local test without AI")
    g.add_argument("--list", action="store_true", help="lists the challenges")
    g.add_argument("--challenge", help="challenge id (see --list) or 'all'")
    g.add_argument("--task", help="free task as text")
    g.add_argument("--task-file", help="free task read from a file")
    p.add_argument("--repeat", type=int, default=1, help="repeats N times (measures reliability)")
    p.add_argument("--think", default="yes",
                   help="reasoning: yes, no or a level the model supports (qwen3.8: low, "
                        "medium, xhigh; with 'yes', the model's default level)")
    p.add_argument("--mode", choices=["native", "text"], default="native",
                   help="native = the API's tool_calls; text = <tool_call> inside the text")
    p.add_argument("--max-steps", type=int, default=20, help="cap on model replies")
    p.add_argument("--num-ctx", default="auto",
                   help="'auto' = the one the server already has loaded (avoids reloads); or a "
                        "number")
    p.add_argument("--model", help="model id in Open WebUI (default: OWUI_MODEL)")
    p.add_argument("--show-thinking", action="store_true", help="shows the reasoning")
    p.add_argument("--time-limit", type=duration, metavar="T",
                   help="maximum batch time, e.g. 2h, 90m or 1h30; does not start an iteration "
                        "that cannot finish in time")
    args = p.parse_args(argv)

    if args.list:
        list_challenges()
        return 0
    if args.selftest:
        return 1 if selftest() else 0

    try:
        client = OWUIClient(CONF["OWUI_URL"], CONF["OWUI_API_KEY"],
                            args.model or CONF["OWUI_MODEL"], CONF["OWUI_CA"],
                            float(CONF["LLM_TIMEOUT"]))
    except APIError as e:
        if args.check:
            check(None)
        print(c(str(e), RED))
        return 2

    if args.check:
        return 1 if check(client) else 0
    if args.status:
        try:
            for m in client.ollama_ps() or [{"name": "(no model loaded)"}]:
                print(f"{m.get('name')} · context {m.get('context_length', '-')} · "
                      f"VRAM {(m.get('size_vram') or 0) / 1e9:.1f} GB · expires "
                      f"{str(m.get('expires_at', '-'))[:19]}")
        except APIError as e:
            print(c(str(e), RED))
            return 2
        return 0

    if args.challenge:
        ids = list(C.CHALLENGES) if args.challenge == "all" else [args.challenge]
        for i in ids:
            if i not in C.CHALLENGES:
                print(c(f"No challenge '{i}'. Use --list.", RED))
                return 2
        jobs = [(C.CHALLENGES[i].message(), C.CHALLENGES[i], f"challenge{i}") for i in ids]
    elif args.task or args.task_file:
        text = args.task or Path(args.task_file).read_text(encoding="utf-8")
        jobs = [(text, None, "free")]
    else:
        p.print_help()
        return 0

    args.think, error = resolve_think(client, args.think)
    if error:
        print(c(error, RED))
        return 2
    args.batch_kind = ("free" if not args.challenge else "all" if args.challenge == "all"
                       else "repeat" if args.repeat > 1 else "single")
    if args.num_ctx == "auto":
        num_ctx = loaded_context(client)
    else:
        num_ctx = int(args.num_ctx)
        print(c(f"⚠ You are requesting num_ctx={num_ctx}. If other users of the server use a "
                "different value, Ollama will reload the model every time your requests "
                "alternate.", YELLOW))

    model = args.model or CONF["OWUI_MODEL"]
    awake = keep_awake()
    if awake:
        print(c(f"☕ {awake}", GREY))
    if args.challenge:
        estimate_before([ch.id for _, ch, _ in jobs], args.repeat, model, args.think,
                        args.time_limit)

    results = []
    total, t0, limit, stopped = len(jobs) * args.repeat, time.time(), args.time_limit, ""
    if limit:
        limit_notice(limit, "another run")
    try:
        for task, challenge, label in jobs:
            for n in range(1, args.repeat + 1):
                done, elapsed = len(results), time.time() - t0
                if limit and done and not fits_another(elapsed, elapsed / done, limit):
                    stopped = (f"after {done} of {total} runs (limit of {limit / 60:.0f} min, "
                               f"average {elapsed / done / 60:.1f} min)")
                    break
                r = run_once(client, task, challenge, args, label, num_ctx,
                             n if args.repeat > 1 else None)
                results.append((label, n, r))
                if total > 1:
                    print(c(estimate(len(results), total, t0, limit, "runs"), GREY))
            if stopped:
                break
    except KeyboardInterrupt:
        pass
    if stopped:
        print(c(f"\n⏱ Stopped by time {stopped}.", YELLOW))

    if len(results) > 1:
        print(c("\n═══ Summary ═══", BOLD))
        per_job: dict[str, list] = {}
        for label, n, r in results:
            per_job.setdefault(label, []).append(r)
        for label, items in per_job.items():
            passed = sum(1 for r in items if r["state"] == "PASS")
            times = [r["st"]["seconds"] for r in items if r["st"]]
            avg = sum(times) / len(times) if times else 0
            print(f"  {label}: {passed}/{len(items)} PASS · average time {fmt_dur(avg)} · "
                  + " ".join(("✅" if r["state"] == "PASS" else "❌") for r in items))
        print(c("Full history in logs/summary.csv", GREY))
    if len(results) > 1 or time.time() - t0 > 120:
        passed = sum(1 for _, _, r in results if r["state"] == "PASS")
        notify_done("vibespice: batch finished",
                    f"{passed}/{len(results)} PASS in {fmt_dur(time.time() - t0)}"
                    + (f" · stopped by time {stopped}" if stopped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
