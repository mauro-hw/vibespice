# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
Fake LLM server to test the agent without a real one. It speaks three dialects at once:

- Open WebUI 0.10.2 (/api/chat/completions, /ollama/api/*): it converts the request to
  Ollama's format (and, like the real one, parses again with json.loads the tool_call
  arguments we send back) and the reply to the OpenAI format;
- an OpenAI-compatible API (/v1/chat/completions, /v1/models);
- the Claude API (/v1/messages, /v1/models), checking what the real one rejects: an edited
  earlier turn, tool results split across messages, sampling parameters, a disabled thinking
  on a model that always reasons, fallbacks without their beta header...

The "model" follows a fixed script per scenario, the same in every dialect.

Direct use (to play by hand):
    python3 tests/fake_server.py challenge2 18080
    VIBESPICE_URL=http://127.0.0.1:18080 VIBESPICE_API_KEY=sk-test \
        VIBESPICE_MODEL=qwen3:32b python3 -m vibespice bench 2
    VIBESPICE_PROVIDER=anthropic VIBESPICE_URL=http://127.0.0.1:18080 \
        VIBESPICE_API_KEY=sk-test VIBESPICE_MODEL=claude-opus-5-5 python3 -m vibespice bench 2
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

KEY = "sk-test"
MODEL = "qwen3:32b"
MODEL_38 = "qwen3.8:27b-q8_0"
# What /ollama/api/show returns for each model (what matters to the agent)
CARDS = {
    MODEL: {"capabilities": ["completion", "tools", "thinking"],
            "thinking": {"values": [False, True], "default": True},
            "details": {"parameter_size": "32.8B", "quantization_level": "Q4_K_M"},
            "model_info": {"qwen3.context_length": 40960}},
    MODEL_38: {"capabilities": ["completion", "vision", "tools", "thinking"],
               "thinking": {"values": [False, "low", "medium", "xhigh"], "default": "medium"},
               "details": {"parameter_size": "27.3B", "quantization_level": "Q8_0"},
               "model_info": {"qwen35.context_length": 262144}},
}
# OpenAI-compatible API
OPENAI_MODELS = ["gpt-test", "qwen3:8b"]
# Claude API: what /v1/models/{id} returns (what matters to the agent)
_EFFORT = {"supported": True, **{e: {"supported": True}
                                 for e in ("low", "medium", "high", "xhigh", "max")}}
CLAUDE = {
    "claude-opus-5-5": {"display_name": "Claude Opus 5.5", "max_input_tokens": 1000000,
                        "max_tokens": 128000, "capabilities": {
                            "thinking": {"supported": True, "types": {
                                "enabled": {"supported": False},
                                "adaptive": {"supported": True}}},
                            "effort": _EFFORT}},
    "claude-haiku-4-5": {"display_name": "Claude Haiku 4.5", "max_input_tokens": 200000,
                         "max_tokens": 64000, "capabilities": {
                             "thinking": {"supported": True, "types": {
                                 "enabled": {"supported": True},
                                 "adaptive": {"supported": False}}},
                             "effort": {"supported": False}}},
}
CLAUDE_ALWAYS_THINKS = {"claude-opus-5-5"}
CLAUDE_FALLBACKS = {"claude-opus-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
NET = "V1 in 0 DC 12\nR1 in out 14k\nR2 out 0 10k\n.op"
NET18 = "V1 in 0 DC 12\nR1 in out 18k\nR2 out 0 13k\n.op"


# --- Conversion like Open WebUI's ------------------------------------------------------
def to_ollama(body: dict) -> dict:
    messages = []
    for m in body.get("messages", []):
        new = {"role": m["role"]}
        if m.get("tool_calls"):
            new["tool_calls"] = [{
                "id": tc.get("id"),
                "function": {"name": tc["function"]["name"],
                             # like Open WebUI: fails if the JSON is broken
                             "arguments": json.loads(tc["function"]["arguments"])}}
                for tc in m["tool_calls"]]
            new["content"] = ""
        else:
            new["content"] = m.get("content", "")
            if m.get("tool_call_id"):
                new["tool_call_id"] = m["tool_call_id"]
        messages.append(new)
    payload = {"model": body.get("model"), "messages": messages,
               "stream": body.get("stream", False)}
    if "tools" in body:
        payload["tools"] = body["tools"]
    options = dict(body.get("options") or {})
    if "think" in options:
        payload["think"] = options.pop("think")
    if options:
        payload["options"] = options
    return payload


def to_openai(model: str, msg: dict, input_tokens: int) -> dict:
    tool_calls = [{"index": i, "id": f"call_{uuid.uuid4()}", "type": "function",
                   "function": {"name": tc["function"]["name"],
                                "arguments": json.dumps(tc["function"]["arguments"])}}
                  for i, tc in enumerate(msg.get("tool_calls") or [])]
    message = {"role": "assistant", "content": msg.get("content", "")}
    if msg.get("thinking"):
        message["reasoning_content"] = msg["thinking"]
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": f"{model}-{uuid.uuid4()}", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": 150,
                      "total_tokens": input_tokens + 150, "response_token/s": 30.0,
                      "prompt_eval_count": input_tokens, "eval_count": 150}}


def to_openai_v1(model: str, msg: dict, input_tokens: int) -> dict:
    """A plain OpenAI chat completion (reasoning in 'reasoning', like OpenRouter)."""
    resp = to_openai(model, msg, input_tokens)
    message = resp["choices"][0]["message"]
    if "reasoning_content" in message:
        message["reasoning"] = message.pop("reasoning_content")
    if msg.get("refusal"):
        resp["choices"][0]["finish_reason"] = "content_filter"
    resp["usage"] = {"prompt_tokens": input_tokens, "completion_tokens": 150,
                     "total_tokens": input_tokens + 150,
                     "prompt_tokens_details": {"cached_tokens": 0}}
    return resp


def _pieces(text: str, n: int = 3) -> list[str]:
    size = max(1, -(-len(text) // n))
    return [text[i:i + size] for i in range(0, len(text), size)]


def to_chunks(resp: dict, usage_chunk: bool | None) -> list[dict]:
    """A chat completion as the chunks of a stream: reasoning and text in pieces, the
    arguments of each tool call split in two, then the finish reason. The usage goes in the
    last chunk (Open WebUI), in a chunk of its own (OpenAI with include_usage: True) or nowhere
    (None)."""
    msg = resp["choices"][0]["message"]
    base = {"id": resp["id"], "object": "chat.completion.chunk", "model": resp["model"]}

    def chunk(delta: dict, finish=None) -> dict:
        return dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": finish}])
    out = [chunk({"role": "assistant", "content": ""})]
    for key in ("reasoning_content", "reasoning"):
        out += [chunk({key: p}) for p in _pieces(msg.get(key) or "")]
    out += [chunk({"content": p}) for p in _pieces(msg.get("content") or "")]
    for tc in msg.get("tool_calls") or []:
        args = tc["function"]["arguments"]
        out.append(chunk({"tool_calls": [{"index": tc["index"], "id": tc["id"], "type": "function",
                                          "function": {"name": tc["function"]["name"],
                                                       "arguments": args[:len(args) // 2]}}]}))
        out.append(chunk({"tool_calls": [{"index": tc["index"],
                                          "function": {"arguments": args[len(args) // 2:]}}]}))
    last = chunk({}, resp["choices"][0]["finish_reason"])
    if usage_chunk is False:
        last["usage"] = resp["usage"]
    out.append(last)
    if usage_chunk:
        out.append(dict(base, choices=[], usage=resp["usage"]))
    return out


def signature(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def to_anthropic(model: str, msg: dict, total: int, cached: int) -> dict:
    blocks = []
    if msg.get("fallback"):
        blocks.append({"type": "fallback", "from": {"model": model},
                       "to": {"model": "claude-opus-5"}})
    if msg.get("thinking"):
        blocks.append({"type": "thinking", "thinking": msg["thinking"],
                       "signature": signature(msg["thinking"])})
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        blocks.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:16]}",
                       "name": call["function"]["name"], "input": call["function"]["arguments"]})
    stop = "refusal" if msg.get("refusal") else "tool_use" \
        if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    usage = {"input_tokens": 12, "cache_read_input_tokens": cached,
             "cache_creation_input_tokens": max(0, total - cached - 12), "output_tokens": 150}
    if msg.get("fallback"):
        usage["iterations"] = [{"type": "message"}, {"type": "fallback_message"}]
    return {"id": f"msg_{uuid.uuid4().hex[:16]}", "type": "message", "role": "assistant",
            "model": "claude-opus-5" if msg.get("fallback") else model, "content": blocks,
            "stop_reason": stop,
            "stop_details": {"type": "refusal", "category": "cyber",
                             "explanation": "scripted refusal"} if msg.get("refusal") else None,
            "usage": usage}


def from_openai_v1(body: dict) -> dict:
    """An OpenAI-compatible request as the Ollama-like payload the script reads."""
    payload = to_ollama({k: v for k, v in body.items() if k not in ("options",)})
    payload["think"] = body.get("reasoning_effort")
    return payload


def from_anthropic(body: dict) -> dict:
    """A Claude API request as the Ollama-like payload the script reads."""
    messages = [{"role": "system", "content": body["system"]}] if body.get("system") else []
    for m in body["messages"]:
        content = m["content"]
        if not isinstance(content, str):
            content = "\n".join(b.get("text", "") if b.get("type") == "text"
                                else b.get("content", "") if b.get("type") == "tool_result"
                                else "" for b in content)
        messages.append({"role": m["role"], "content": content})
    payload = {"model": body["model"], "messages": messages,
               "think": (body.get("output_config") or {}).get("effort")
               or (body.get("thinking") or {}).get("type") in ("adaptive", "enabled")}
    if body.get("tools"):
        payload["tools"] = body["tools"]
    return payload


def check_anthropic(body: dict, headers, sent: dict) -> str:
    """What the real Claude API would reject (and what vibespice must always send)."""
    model = body.get("model")
    if not isinstance(body.get("max_tokens"), int):
        return "max_tokens: field required"
    for k in ("temperature", "top_p", "top_k"):
        if k in body:
            return f"{k}: not supported for this model"
    if body.get("cache_control") != {"type": "ephemeral"}:
        return "(fake) vibespice must ask for automatic prompt caching"
    thinking = body.get("thinking") or {}
    if model in CLAUDE_ALWAYS_THINKS and thinking.get("type") in ("disabled", "enabled"):
        return f"thinking.type: {thinking['type']} is not supported for {model}"
    effort = (body.get("output_config") or {}).get("effort")
    if effort and not CLAUDE[model]["capabilities"]["effort"].get(effort, {}).get("supported"):
        return f"output_config.effort: {effort} is not supported for {model}"
    beta = FALLBACK_BETA in (headers.get("anthropic-beta") or "")
    if model in CLAUDE_FALLBACKS and (body.get("fallbacks") != "default" or not beta):
        return "(fake) vibespice must send fallbacks: default with its beta header"
    if model not in CLAUDE_FALLBACKS and ("fallbacks" in body or beta):
        return f"fallbacks: not available for {model}"
    for t in body.get("tools") or []:
        if set(t) != {"name", "description", "input_schema"}:
            return f"tools: unexpected fields {sorted(t)}"
    messages = body.get("messages") or []
    if not messages or messages[0]["role"] != "user":
        return "messages: the first message must use the user role"
    last = -1
    for i, m in enumerate(messages):
        if m["role"] != "assistant":
            continue
        key = json.dumps(m["content"], sort_keys=True)
        if key not in sent or sent[key] <= last:
            return f"messages.{i}: an earlier assistant turn was modified or reordered"
        last = sent[key]
        uses = [b["id"] for b in m["content"] if b.get("type") == "tool_use"]
        if uses:
            nxt = messages[i + 1]["content"] if i + 1 < len(messages) else ""
            results = [b.get("tool_use_id") for b in nxt if isinstance(b, dict)
                       and b.get("type") == "tool_result"] if isinstance(nxt, list) else []
            if sorted(results) != sorted(uses):
                return (f"messages.{i + 1}: tool_use ids {uses} need their tool_result "
                        "blocks in the next message")
    return ""


# --- Scripted "model" ------------------------------------------------------------------
def tc(name: str, args: dict) -> dict:
    return {"function": {"name": name, "arguments": args}}


_MODEL_2N3904 = (".model Q2N3904 NPN(IS=6.734f XTI=3 EG=1.11 VAF=74.03 BF=416.4 NE=1.259 "
                 "ISE=6.734f IKF=66.78m XTB=1.5 BR=.7371 NC=2 ISC=0 IKR=0 RC=1 CJC=3.638p "
                 "MJC=.3085 VJC=.75 FC=.5 CJE=4.493p MJE=.2593 VJE=.75 TR=239.5n TF=301.2p "
                 "ITF=.4 VTF=4 XTF=2 RB=10)")


def model_reply(scenario: str, payload: dict) -> dict:
    msgs = payload["messages"]
    n = sum(1 for m in msgs if m["role"] == "assistant")
    last = msgs[-1].get("content", "")
    thinking = "Reasoning about the divider..." if payload.get("think") else None
    if scenario == "challenge2":
        script = [
            {"thinking": thinking, "tool_calls": [tc("simulate", {"netlist": NET})]},
            {"thinking": thinking, "tool_calls": [tc("calculate", {"expression": "2*0.5833*t"})]},
            {"thinking": thinking, "content": "Trying t = 1.709 %.", "tool_calls": [
                tc("analyze_tolerances", {"netlist": NET, "tolerances": {"R1": 1.709, "R2": 1.709},
                                          "output": "v(out)", "method": "corners",
                                          "target_value": 5, "error_limit_pct": 2})]},
            {"thinking": thinking, "content": "The maximum tolerance is 1.71 %.\n\n```json\n"
             '{"max_tolerance_pct": 1.71, "commercial_tolerance_pct": 1}\n```'},
        ]
        return script[min(n, len(script) - 1)]
    if scenario == "text":
        assert "tools" not in payload, "'tools' must not be sent in text mode"
        if n == 0:
            return {"content": "Simulating first.\n<tool_call>\n" + json.dumps(
                {"name": "simulate", "arguments": {"netlist": NET18}}) + "\n</tool_call>"}
        assert "<tool_response>" in last, "missing <tool_response>"
        return {"content": "<think>thinking</think>Vout = 5.0323 V.\n```json\n"
                '{"vout_V": 5.0323, "current_mA": -0.3871}\n```'}
    if scenario == "leak":       # native mode, but the call arrives as text
        if n == 0:
            return {"content": "<tool_call>\n" + json.dumps(
                {"name": "simulate", "arguments": {"netlist": NET18}}) + "\n</tool_call>"}
        return {"content": '```json\n{"vout_V": "5.0323", "current_mA": "0,3871",}\n```'}
    if scenario == "nojson":
        if n == 0:
            return {"tool_calls": [tc("simulate", {"netlist": NET18})]}
        if "is missing" not in last:
            return {"content": "The output is 5.03 V and the current 0.39 mA."}
        return {"content": 'Sorry: {"vout_V": 5.03226, "current_mA": 0.387}'}
    if scenario == "loop":
        if "repeated the same call" in last:
            return {"tool_calls": [tc("simulate", {"netlist": NET18})]}
        if "Simulation run" in last:
            return {"content": '```json\n{"vout_V": 5.0323, "current_mA": 0.3871}\n```'}
        return {"tool_calls": [tc("calculate", {"expression": "12*13/31"})]}
    if scenario == "empty":
        if n == 0:
            return {"content": "", "thinking": "just thinking"}
        return {"content": '```json\n{"vout_V": 5.0323, "current_mA": 0.3871}\n```'}
    if scenario == "errors":
        if n == 0:
            return {"tool_calls": [tc("simulate", {"netlist": "Divider test\n" + NET18})]}
        if n == 1:
            return {"tool_calls": [tc("analyze_tolerances", {"netlist": NET, "tolerances": {"R9": 1},
                                                             "output": "v(out)"})]}
        if n == 2:
            return {"tool_calls": [tc("simulate", {"netlist": NET18})]}
        return {"content": '```json\n{"vout_V": 5.0323, "current_mA": 0.3871}\n```'}
    if scenario == "fail":       # wrong answer: the verification must fail it
        return {"content": '```json\n{"max_tolerance_pct": 2.0, "commercial_tolerance_pct": 2}\n```'}
    if scenario == "refusal":
        return {"refusal": True, "content": ""}
    if scenario in ("busy", "nokey", "fallback"):   # errors and dialect details: see the server
        if n == 0:
            return {"thinking": thinking, "tool_calls": [tc("simulate", {"netlist": NET18})],
                    "fallback": scenario == "fallback"}
        return {"content": 'Vout = 5.0323 V.\n```json\n{"vout_V": 5.03226, '
                           '"current_mA": 0.387097}\n```'}
    if scenario == "cold":       # the model is not loaded until the first request
        if n == 0:
            return {"tool_calls": [tc("simulate", {"netlist": NET18})]}
        return {"content": 'Vout = 5.0323 V.\n```json\n{"vout_V": 5.03226, "current_mA": 0.387097}\n```'}
    if scenario == "challenge6":  # RC filter: .ac + .meas and C in farads
        if n == 0:
            return {"thinking": thinking, "tool_calls": [tc("simulate", {"netlist": (
                "V1 in 0 AC 1\nRa in m 4.7k\nRb m out 120\nC1 out 0 33n\n"
                ".ac dec 100 10 100k\n.meas ac fc WHEN vdb(out)=-3.0103")})]}
        assert "fc = 1000.6" in last, "the .meas is missing from the result"
        return {"content": 'fc = 1000.6 Hz.\n```json\n{"Ra_ohm": 4700, "Rb_ohm": 120, '
                           '"C_F": 3.3e-8, "fc_Hz": 1000.6, "error_pct": 0.06}\n```'}
    if scenario == "invents":    # challenge 6: makes up fc, the .meas fails, fixed with the hint
        if n == 0:
            return {"content": '```json\n{"Ra_ohm": 12000, "Rb_ohm": 3900, "C_F": 1e-8, '
                               '"fc_Hz": 1000, "error_pct": 0}\n```'}
        if n == 1:
            assert "do not come from any successful simulation" in last, "origin notice missing"
            return {"tool_calls": [tc("simulate", {"netlist": (
                "V1 in 0 AC 1\nRa in m 12k\nRb m out 3.9k\nC1 out 0 10n\n.ac dec 10 1k 1k\n"
                ".meas ac fc when mag(v(out)/v(in))=0.7071")})]}
        if n == 2:
            assert "vm(out)" in last and "single point" in last, ".ac hints missing"
            assert "fc = 0" not in last, "false measurement"
            return {"tool_calls": [tc("simulate", {"netlist": (
                "V1 in 0 AC 1\nRa in m 12k\nRb m out 3.9k\nC1 out 0 10n\n.ac dec 100 10 100k\n"
                ".meas ac fc WHEN vm(out)=0.7071068")})]}
        return {"content": '```json\n{"Ra_ohm": 12000, "Rb_ohm": 3900, "C_F": 1e-8, '
                           '"fc_Hz": 1001, "error_pct": 0.1}\n```'}
    if scenario == "challenge7":  # transistor: Ic from the "Transistors" block and derived change
        net = ("VCC vcc 0 DC 12\nR1 vcc b 4.7k\nR2 b 0 7.5k\nRC vcc c 240\nRE e 0 6.8k\n"
               "Q1 c b e Q2N3904\n" + _MODEL_2N3904 + "\n.op")
        json7 = ('```json\n{"R1_ohm": 4700, "R2_ohm": 7500, "RC_ohm": 240, "RE_ohm": 6800, '
                 '"ic_mA": 0.9777, "vce_V": 5.071, "ic_change_pct": %s}\n```')
        if n == 0:
            return {"tool_calls": [tc("simulate", {"netlist": net})]}
        if n == 1:
            assert "Q1: Ic = 977.7 µA" in last and "active region" in last, "Q1 data missing"
            assert "Q1 (NPN): collector → node c · base → node b · emitter → node e" in last \
                and "Warnings:" not in last, "connections missing or extra warnings"
            return {"content": json7 % "-0.8"}          # the change, estimated mentally
        if n == 2:
            assert "ic_change_pct is the % change of ic between two simulations" in last, \
                "derived-value notice missing"
            return {"tool_calls": [tc("simulate", {"netlist": net.replace("BF=416.4", "BF=100")})]}
        assert "Q1: Ic = 967.9 µA" in last, "the BF = 100 simulation is missing"
        return {"content": json7 % "-1.01"}
    if scenario == "qwen38":     # another model: it gets its reasoning level and its sampling
        assert payload["model"] == MODEL_38, "model"
        if payload["think"] is False:                   # --check: sampling without reasoning
            assert payload["options"]["temperature"] == 0.7 \
                and payload["options"]["presence_penalty"] == 1.5, "sampling without reasoning"
            if "tools" in payload:
                return {"tool_calls": [tc("calculate", {"expression": "1234*5678"})]}
            return {"content": "OK"}
        assert payload["think"] == "low", "reasoning level"
        assert payload["options"]["temperature"] == 1.0 \
            and payload["options"]["presence_penalty"] == 0.0, "qwen3.8 sampling"
        if n == 0:
            return {"thinking": "Thinking a little.",
                    "tool_calls": [tc("simulate", {"netlist": NET18})]}
        return {"content": '```json\n{"vout_V": 5.0323, "current_mA": 0.3871}\n```'}
    if scenario == "runaway":    # ignores max_tokens, like the real one: the client must cut it
        if n == 0:
            return {"thinking": "Let me think again about it. " * 300, "content": ""}
        if n == 1:
            assert last.startswith("Your reply was empty"), "empty-reply nudge after the cut"
            return {"tool_calls": [tc("simulate", {"netlist": NET18})]}
        return {"content": '```json\n{"vout_V": 5.03226, "current_mA": 0.387097}\n```'}
    if scenario == "slow":       # each reply takes about 3 s (see _stream and do_POST)
        if n == 0:
            return {"thinking": "Thinking slowly about the divider.",
                    "tool_calls": [tc("simulate", {"netlist": NET18})]}
        return {"content": 'Vout = 5.0323 V.\n```json\n{"vout_V": 5.03226, "current_mA": 0.387097}\n```'}
    if scenario == "challenge5":
        return {"content": "It is not possible.\n```json\n"
                '{"feasible": false, "reason": "scales with the input", "alternative": "LDO"}\n```'}
    raise ValueError(f"unknown scenario: {scenario}")


# --- HTTP server -----------------------------------------------------------------------
def error_body(dialect: str, kind: str, message: str) -> dict:
    if dialect == "anthropic":
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message, "type": kind}}


def make_server(scenario: str, port: int = 0) -> ThreadingHTTPServer:
    # sent: every Claude reply (its content, as JSON) → order, to check the history
    state = {"loaded": scenario != "cold", "chats": 0, "sent": {}, "prev": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj, headers: dict | None = None) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _stream(self, resp: dict, usage_chunk: bool | None) -> None:
            """Server-sent events, one chunk at a time (in 'slow', 0.4 s apart)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                for ev in to_chunks(resp, usage_chunk):
                    if scenario == "slow":
                        time.sleep(0.4)
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                self._send(401, {"detail": "Your session has expired or the token is invalid."})
                return False
            return True

        def _busy(self, dialect: str) -> bool:
            """Scenario 'busy': the first chat gets a 429 and the second a 529 (503 outside
            the Claude API), both with retry-after: 0; then it answers."""
            if scenario != "busy":
                return False
            with lock:
                state["chats"] += 1
                n = state["chats"]
            if n == 1:
                self._send(429, error_body(dialect, "rate_limit_error", "Too many requests"),
                           {"retry-after": "0"})
                return True
            if n == 2:
                self._send(529 if dialect == "anthropic" else 503,
                           error_body(dialect, "overloaded_error", "Overloaded"),
                           {"retry-after": "0"})
                return True
            return False

        # --- OpenAI-compatible API ------------------------------------------------------
        def _openai_authorized(self) -> bool:
            auth = self.headers.get("Authorization")
            if (auth is not None) if scenario == "nokey" else auth != f"Bearer {KEY}":
                self._send(401, error_body("openai", "invalid_request_error",
                                           "Incorrect API key provided"))
                return False
            return True

        def _openai_chat(self, body: dict) -> None:
            if not self._openai_authorized():
                return
            for k in ("options", "think", "top_k"):
                if k in body:
                    return self._send(400, error_body(
                        "openai", "invalid_request_error",
                        f"Unrecognized request argument supplied: {k}"))
            if body.get("model") not in OPENAI_MODELS:
                return self._send(404, error_body(
                    "openai", "invalid_request_error",
                    f"The model `{body.get('model')}` does not exist"))
            if self._busy("openai"):
                return
            try:
                payload = from_openai_v1(body)
                msg = model_reply(scenario, payload)
            except Exception as e:  # noqa: BLE001 - returned as a 500, like the real one
                return self._send(500, error_body("openai", "server_error",
                                                  f"{type(e).__name__}: {e}"))
            resp = to_openai_v1(body["model"], msg, len(json.dumps(payload)) // 3)
            if body.get("stream"):
                return self._stream(resp, bool((body.get("stream_options") or {})
                                               .get("include_usage")) or None)
            self._send(200, resp)

        # --- Claude API -----------------------------------------------------------------
        def _anthropic_authorized(self) -> bool:
            if self.headers.get("x-api-key") != KEY:
                self._send(401, error_body("anthropic", "authentication_error",
                                           "invalid x-api-key"))
                return False
            if self.headers.get("anthropic-version") != "2023-06-01":
                self._send(400, error_body("anthropic", "invalid_request_error",
                                           "anthropic-version: header required"))
                return False
            return True

        def _anthropic_models(self, path: str) -> None:
            if not self._anthropic_authorized():
                return
            if path == "/v1/models":
                return self._send(200, {"data": [{"type": "model", "id": k,
                                                  "display_name": v["display_name"]}
                                                 for k, v in CLAUDE.items()],
                                        "has_more": False})
            model = path.rsplit("/", 1)[-1]
            if model not in CLAUDE:
                return self._send(404, error_body("anthropic", "not_found_error",
                                                  f"model: {model}"))
            self._send(200, {"type": "model", "id": model, **CLAUDE[model]})

        def _anthropic_chat(self, body: dict) -> None:
            if not self._anthropic_authorized():
                return
            if body.get("model") not in CLAUDE:
                return self._send(404, error_body("anthropic", "not_found_error",
                                                  f"model: {body.get('model')}"))
            with lock:
                problem = check_anthropic(body, self.headers, state["sent"])
            if problem:
                return self._send(400, error_body("anthropic", "invalid_request_error",
                                                  problem))
            if self._busy("anthropic"):
                return
            try:
                msg = model_reply(scenario, from_anthropic(body))
            except Exception as e:  # noqa: BLE001 - returned as a 500, like the real one
                return self._send(500, error_body("anthropic", "api_error",
                                                  f"{type(e).__name__}: {e}"))
            total = len(json.dumps(body)) // 3
            with lock:
                cached, state["prev"] = min(state["prev"], total), total
                resp = to_anthropic(body["model"], msg, total, cached)
                state["sent"][json.dumps(resp["content"], sort_keys=True)] = len(state["sent"])
            self._send(200, resp)

        # --- Routing --------------------------------------------------------------------
        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith("/v1/models"):
                if self.headers.get("anthropic-version") or self.headers.get("x-api-key"):
                    return self._anthropic_models(path)
                if not self._openai_authorized():
                    return
                return self._send(200, {"object": "list", "data": [
                    {"id": m, "object": "model"} for m in OPENAI_MODELS]})
            if not self._authorized():
                return
            routes = {
                "/api/models": {"data": [{"id": MODEL}, {"id": MODEL_38},
                                         {"id": "nomic-embed-text"}]},
                "/ollama/api/version": {"version": "0.31.2"},
                "/ollama/api/ps": {"models": [{"name": MODEL, "model": MODEL,
                                               "size_vram": 26_400_000_000, "context_length": 40960,
                                               "expires_at": "2026-09-28T11:05:00+02:00"}]
                                   if state["loaded"] else []},
            }
            if path in routes:
                return self._send(200, routes[path])
            self._send(404, {"detail": "Not Found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if path == "/v1/messages":
                return self._anthropic_chat(body)
            if path == "/v1/chat/completions":
                return self._openai_chat(body)
            if not self._authorized():
                return
            if path == "/ollama/api/show":
                if body.get("model") not in CARDS:
                    return self._send(404, {"detail": "model not found"})
                return self._send(200, CARDS[body["model"]])
            if path != "/api/chat/completions":
                return self._send(404, {"detail": "Not Found"})
            if body.get("model") not in CARDS:
                return self._send(404, {"detail": "Model not found"})
            if self._busy("openwebui"):
                return
            state["loaded"] = True
            try:
                payload = to_ollama(body)
                msg = model_reply(scenario, payload)
            except Exception as e:  # noqa: BLE001 - returned as a 500, like the real one
                return self._send(500, {"detail": f"{type(e).__name__}: {e}"})
            resp = to_openai(body["model"], msg, len(json.dumps(payload)) // 3)
            if body.get("stream"):
                return self._stream(resp, False)
            if scenario == "slow":              # the same reply, in one piece and late
                time.sleep(3)
            try:
                self._send(200, resp)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def start_in_background(scenario: str) -> tuple[ThreadingHTTPServer, str]:
    srv = make_server(scenario)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    sc = sys.argv[1] if len(sys.argv) > 1 else "challenge2"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 18080
    print(f"Fake LLM server at http://127.0.0.1:{port} (scenario {sc}, key {KEY})")
    make_server(sc, port).serve_forever()
