"""
Fake Open WebUI to test the agent without a real server.

It imitates what Open WebUI 0.10.2 does with /api/chat/completions when the model comes
from Ollama: it converts the request to Ollama's format (and, like the real one, parses
again with json.loads the tool_call arguments we send back) and converts the reply to the
OpenAI format (tool_calls with id, arguments as JSON text, reasoning_content and usage
with prompt_tokens). The "model" follows a fixed script per scenario.

Direct use (to play by hand):
    python3 tests/fake_server.py challenge2 18080
    OWUI_URL=http://127.0.0.1:18080 OWUI_API_KEY=sk-test python3 spice_agent.py --challenge 2
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    if scenario == "challenge5":
        return {"content": "It is not possible.\n```json\n"
                '{"feasible": false, "reason": "scales with the input", "alternative": "LDO"}\n```'}
    raise ValueError(f"unknown scenario: {scenario}")


# --- HTTP server -----------------------------------------------------------------------
def make_server(scenario: str, port: int = 0) -> ThreadingHTTPServer:
    loaded = {"yes": scenario != "cold"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                self._send(401, {"detail": "Your session has expired or the token is invalid."})
                return False
            return True

        def do_GET(self):
            if not self._authorized():
                return
            routes = {
                "/api/models": {"data": [{"id": MODEL}, {"id": MODEL_38},
                                         {"id": "nomic-embed-text"}]},
                "/ollama/api/version": {"version": "0.31.2"},
                "/ollama/api/ps": {"models": [{"name": MODEL, "model": MODEL,
                                               "size_vram": 26_400_000_000, "context_length": 40960,
                                               "expires_at": "2026-09-28T11:05:00+02:00"}]
                                   if loaded["yes"] else []},
            }
            if self.path in routes:
                return self._send(200, routes[self.path])
            self._send(404, {"detail": "Not Found"})

        def do_POST(self):
            if not self._authorized():
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/ollama/api/show":
                if body.get("model") not in CARDS:
                    return self._send(404, {"detail": "model not found"})
                return self._send(200, CARDS[body["model"]])
            if self.path != "/api/chat/completions":
                return self._send(404, {"detail": "Not Found"})
            if body.get("model") not in CARDS:
                return self._send(404, {"detail": "Model not found"})
            loaded["yes"] = True
            try:
                payload = to_ollama(body)
                msg = model_reply(scenario, payload)
            except Exception as e:  # noqa: BLE001 - returned as a 500, like the real one
                return self._send(500, {"detail": f"{type(e).__name__}: {e}"})
            self._send(200, to_openai(body["model"], msg, len(json.dumps(payload)) // 3))

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def start_in_background(scenario: str) -> tuple[ThreadingHTTPServer, str]:
    srv = make_server(scenario)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    sc = sys.argv[1] if len(sys.argv) > 1 else "challenge2"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 18080
    print(f"Fake Open WebUI at http://127.0.0.1:{port} (scenario {sc}, key {KEY})")
    make_server(sc, port).serve_forever()
