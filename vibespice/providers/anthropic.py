# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
The Claude API (Anthropic's Messages API), over plain HTTP.

What makes it different from the OpenAI format:
- the assistant's turns go back exactly as they came, thinking blocks included: the API
  rejects a history whose earlier turns were edited;
- all the tool results of one turn go together in a single user message;
- reasoning is set with the effort level (--think low … max), and there are no sampling
  parameters;
- automatic prompt caching, so each step pays little for the conversation it repeats;
- if the model declines (stop reason "refusal"), the fallback model Anthropic recommends
  continues, on the models that offer it.
"""
from __future__ import annotations

from .base import APIError, Conversation, Provider, Reply, ToolCall, Usage, tool_schemas

VERSION = "2023-06-01"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
ALWAYS_THINKS = ("claude-opus-5-5", "claude-fable-5", "claude-mythos-5")
THINKING_OFF = {"claude-sonnet-5-5": {"type": "between_tools"}}   # default: {"type": "disabled"}


class AnthropicConversation(Conversation):
    def __init__(self, system: str | None, task: str, tools: list[dict] | None):
        self.system = system
        self.tools = [{"name": t["name"], "description": t.get("description", ""),
                       "input_schema": t["parameters"]} for t in tool_schemas(tools)]
        self.messages = [{"role": "user", "content": task}]

    def add_reply(self, reply: Reply) -> None:
        blocks = reply.raw.get("content") or []
        if blocks:                                   # exactly as it came
            self.messages.append({"role": "assistant", "content": blocks})

    def add_tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": text,
             **({"is_error": True} if error else {})} for call, text, error in results]})

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


class AnthropicProvider(Provider):
    name = "anthropic"
    label = "Claude API"
    default_url = "https://api.anthropic.com"
    key_env = "ANTHROPIC_API_KEY"
    key_required = True
    quick_think = "low"
    details_title = "Model (Claude API)"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._info: dict | None = None

    @property
    def uses_fallbacks(self) -> bool:
        return self.fallbacks and self.model in FALLBACK_MODELS

    def headers(self, path: str) -> dict:
        h = {"x-api-key": self.key, "anthropic-version": VERSION}
        if path == "/v1/messages" and self.uses_fallbacks:
            h["anthropic-beta"] = FALLBACK_BETA
        return h

    def hint(self, status, path):
        return {
            401: "Key not recognized: check api_key in your profile (or ANTHROPIC_API_KEY). "
                 "Keys are created in the Claude Console and start with sk-ant-.",
            402: "No credit: add credit in the Claude Console (Billing). A Claude Pro or Max "
                 "subscription does not include the API.",
            403: "This key has no permission for that model or feature.",
            404: "Model not found: check model in your profile; vibespice check lists the "
                 "models of your account.",
            413: "The conversation is too large for a single request.",
        }.get(status, "")

    def conversation(self, system, task, tools):
        return AnthropicConversation(system, task, tools)

    # --- Model and reasoning ------------------------------------------------------------
    def model_info(self) -> dict:
        if self._info is None:
            self._info = self.request("GET", f"/v1/models/{self.model}", timeout=30)
        return self._info

    def _caps(self) -> dict:
        try:
            return self.model_info().get("capabilities") or {}
        except APIError:
            return {}

    def _levels(self, caps: dict) -> list[str]:
        effort = caps.get("effort") or {}
        return [e for e in EFFORTS if (effort.get(e) or {}).get("supported")]

    def _adaptive(self, caps: dict) -> bool:
        types = (caps.get("thinking") or {}).get("types") or {}
        return bool((types.get("adaptive") or {}).get("supported"))

    def resolve_think(self, requested: str) -> tuple[str | None, str]:
        """'yes' becomes effort 'high' on the models with effort levels, so that the logs say
        which level it reasoned with."""
        caps = self._caps()
        levels = self._levels(caps)
        if requested == "yes":
            return ("high" if "high" in levels else "yes"), ""
        if requested == "no":
            if self.model.startswith(ALWAYS_THINKS):
                return None, (f"{self.model} always reasons: use --think low for the least "
                              "reasoning.")
            return "no", ""
        if requested in EFFORTS and (not caps or requested in levels):
            return requested, ""
        options = ["yes"] + ([] if self.model.startswith(ALWAYS_THINKS) else ["no"]) \
            + (levels if caps else list(EFFORTS))
        return None, f"{self.model} does not support --think {requested}. Options: " \
            + ", ".join(options) + "."

    def thinking(self, think) -> dict:
        if think in EFFORTS:
            return {"thinking": {"type": "adaptive", "display": "summarized"},
                    "output_config": {"effort": think}}
        if think == "no":
            return {"thinking": THINKING_OFF.get(self.model, {"type": "disabled"})}
        # yes: the model's default; if it reasons adaptively, ask for a readable summary
        return {"thinking": {"type": "adaptive", "display": "summarized"}} \
            if self._adaptive(self._caps()) else {}

    def request_extras(self, think, num_ctx=None) -> dict:
        extras = {"max_tokens": self.max_tokens or DEFAULT_MAX_TOKENS,
                  "cache_control": {"type": "ephemeral"}, **self.thinking(think)}
        if self.uses_fallbacks:
            extras["fallbacks"] = "default"
        return extras

    # --- Requests -----------------------------------------------------------------------
    def chat(self, conv: AnthropicConversation, think, num_ctx=None) -> Reply:
        body = {"model": self.model, "messages": conv.messages}
        if conv.system:
            body["system"] = conv.system
        if conv.tools:
            body["tools"] = conv.tools
        body.update(self.request_extras(think))
        return self.parse(self.request("POST", "/v1/messages", body))

    def parse(self, resp: dict) -> Reply:
        blocks = resp.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        reasoning = "\n".join(b["thinking"] for b in blocks
                              if b.get("type") == "thinking" and b.get("thinking")).strip()
        calls = [ToolCall(id=b.get("id", ""), name=b.get("name", "?"),
                          args=b["input"] if isinstance(b.get("input"), dict) else {})
                 for b in blocks if b.get("type") == "tool_use"]
        u = resp.get("usage") or {}
        read = int(u.get("cache_read_input_tokens") or 0)
        written = int(u.get("cache_creation_input_tokens") or 0)
        usage = Usage(input=int(u.get("input_tokens") or 0) + read + written,
                      output=int(u.get("output_tokens") or 0), cache_read=read,
                      cache_write=written, raw=u)
        stop = {"end_turn": "end", "stop_sequence": "end", "tool_use": "tool_use",
                "max_tokens": "max_tokens", "refusal": "refusal", "pause_turn": "pause"
                }.get(resp.get("stop_reason"), resp.get("stop_reason") or "end")
        notes = [f"{(b.get('from') or {}).get('model')} declined; "
                 f"{(b.get('to') or {}).get('model')} continued"
                 for b in blocks if b.get("type") == "fallback"]
        if not notes and any(i.get("type") == "fallback_message"
                             for i in u.get("iterations") or []):
            notes.append(f"served by the fallback model {resp.get('model')}")
        if stop == "refusal":
            d = resp.get("stop_details") or {}
            notes.append("the model declined to answer"
                         + (f" (category: {d['category']})" if d.get("category") else "")
                         + (f": {d['explanation']}" if d.get("explanation") else ""))
        return Reply(content=text.strip(), reasoning=reasoning, tool_calls=calls, usage=usage,
                     stop=stop, raw_text=text, notes=notes, raw=resp)

    def models(self) -> list[str]:
        data = self.request("GET", "/v1/models?limit=1000", timeout=30)
        return [m.get("id", "?") for m in data.get("data", [])]

    def details(self) -> list[str]:
        try:
            info = self.model_info()
        except APIError as e:
            return [f"(model details not available: {e})"]
        caps = info.get("capabilities") or {}
        levels = self._levels(caps)
        off = [] if self.model.startswith(ALWAYS_THINKS) else ["no"]
        lines = [f"{info.get('display_name', self.model)}: context "
                 f"{info.get('max_input_tokens', '?')} tokens · up to "
                 f"{info.get('max_tokens', '?')} output tokens (vibespice asks for "
                 f"{self.max_tokens or DEFAULT_MAX_TOKENS})",
                 "reasoning (--think): " + ", ".join(["yes"] + off + levels)
                 + (" · yes = high" if "high" in levels else ""),
                 "sampling: the API's own (the agent sends none)"]
        if self.uses_fallbacks:
            lines.append("if the model declines, the fallback model Anthropic recommends "
                         "continues (fallbacks: default)")
        return lines
