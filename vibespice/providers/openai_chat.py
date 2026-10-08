# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
Providers that speak the OpenAI chat format: Open WebUI (which forwards to Ollama, with its
reasoning levels, loaded models and model card) and any OpenAI-compatible API (OpenAI,
OpenRouter, Ollama's /v1, vLLM, LM Studio, llama.cpp...).
"""
from __future__ import annotations

import json
import uuid

from ..console import shorten
from .base import (APIError, Conversation, Provider, Reply, ToolCall, Usage, error_detail,
                   profile_name, sampling, split_thinking)


def think_value(think) -> bool | str:
    """--think → Ollama's 'think' field: yes/no, or the level as is (low, medium…)."""
    return {"yes": True, "no": False}.get(think, think)


class OpenAIConversation(Conversation):
    def __init__(self, system: str | None, task: str, tools: list[dict] | None):
        self.messages = ([{"role": "system", "content": system}] if system else []) \
            + [{"role": "user", "content": task}]
        self.tools = tools

    def add_reply(self, reply: Reply) -> None:
        msg = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            msg["tool_calls"] = [{"id": t.id, "type": "function",
                                  "function": {"name": t.name, "arguments": t.wire or "{}"}}
                                 for t in reply.tool_calls]
        self.messages.append(msg)

    def add_tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        for call, text, _ in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                  "content": text})

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


def parse_chat(resp: dict) -> Reply:
    try:
        msg = resp["choices"][0]["message"]
        finish = resp["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        raise APIError(f"Unexpected reply from the server: {shorten(json.dumps(resp), 300)}") \
            from None
    raw = msg.get("content") or ""
    content, thought = split_thinking(raw)
    reasoning = "\n".join(x for x in (msg.get("reasoning_content") or msg.get("reasoning")
                                      or "", thought) if x).strip()
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        a = fn.get("arguments", {})
        if isinstance(a, str):
            try:
                obj = json.loads(a) if a.strip() else {}
            except json.JSONDecodeError:
                obj, a = a, "{}"     # Open WebUI parses it again: never send a broken one back
        else:
            obj, a = a, json.dumps(a, ensure_ascii=False)
        calls.append(ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                              name=fn.get("name", "?"), args=obj, wire=a))
    u = resp.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    usage = Usage(input=int(u.get("prompt_tokens") or u.get("prompt_eval_count") or 0),
                  output=int(u.get("completion_tokens") or u.get("eval_count") or 0),
                  cache_read=int(details.get("cached_tokens") or 0),
                  speed=u.get("response_token/s"), raw=u)
    stop = {"stop": "end", "tool_calls": "tool_use", "length": "max_tokens",
            "content_filter": "refusal"}.get(finish, "tool_use" if calls else "end")
    return Reply(content=content, reasoning=reasoning, tool_calls=calls, usage=usage,
                 stop=stop, raw_text=raw, raw=resp)


def collect(events, progress=None, limit: int | None = None) -> dict:
    """The chunks of a streamed chat completion, put back together as one non-streamed
    response (for parse_chat). progress(reasoning_chars, text_chars) after each chunk.

    limit: max_tokens, enforced here too, because some servers ignore it (an Open WebUI with
    Ollama ignored both max_tokens and num_predict, and one reply ran to 171 000 tokens). Past
    it, the stream is closed, which also stops the generation on the server, and the reply
    ends as if cut by the server (finish_reason 'length'). Tokens are estimated: the larger
    of the chunks received and the characters / 4."""
    content, reasoning, calls = [], [], {}
    finish, usage = None, None
    n_reasoning = n_content = pieces = 0
    for ev in events:
        if ev.get("error"):
            raise APIError("The server stopped the reply: " + error_detail(json.dumps(ev)))
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
                n_content += len(delta["content"])
            thought = delta.get("reasoning_content") or delta.get("reasoning")
            if thought:
                reasoning.append(thought)
                n_reasoning += len(thought)
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", len(calls)), {
                    "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                fn = tc.get("function") or {}
                slot["id"] = slot["id"] or tc.get("id")
                slot["function"]["name"] = slot["function"]["name"] or fn.get("name") or ""
                args = fn.get("arguments")
                if isinstance(args, dict):           # some servers send the whole object
                    slot["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
                elif args:
                    slot["function"]["arguments"] += args
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            pieces += 1
        if progress:
            progress(n_reasoning, n_content)
        if limit and max(pieces, (n_reasoning + n_content) // 4) > limit:
            events.close()
            finish = "length"
            break
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [calls[k] for k in sorted(calls)]
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage or {}}


class OpenAIChatProvider(Provider):
    chat_path = "/chat/completions"
    models_path = "/models"

    def conversation(self, system, task, tools):
        return OpenAIConversation(system, task, tools)

    def chat(self, conv: OpenAIConversation, think, num_ctx=None) -> Reply:
        body = {"model": self.model, "messages": conv.messages, "stream": self.stream}
        if conv.tools:
            body["tools"] = conv.tools
        body.update(self.request_extras(think, num_ctx))
        if not self.stream:
            return parse_chat(self.request("POST", self.chat_path, body))
        body.update(self.stream_extras())
        return parse_chat(collect(self.stream_events(self.chat_path, body), self.progress,
                                  self.max_tokens))

    def stream_extras(self) -> dict:
        """What a streamed request adds (OpenAI: ask for the usage in the last chunk)."""
        return {}

    def models(self) -> list[str]:
        data = self.request("GET", self.models_path, timeout=30)
        return [m.get("id", "?") for m in data.get("data", [])]


class OpenWebUIProvider(OpenAIChatProvider):
    name = "openwebui"
    label = "Open WebUI"
    key_required = True
    supports_num_ctx = True
    supports_status = True
    quick_think = "no"
    chat_path = "/api/chat/completions"
    models_path = "/api/models"
    details_title = "Ollama (through Open WebUI, needs an admin key)"

    def hint(self, status, path):
        return {
            401: "Key not recognized: check that api_key is copied correctly (it starts with "
                 "sk-) and that you haven't regenerated it.",
            403: "No permission: is 'Enable API Keys' turned on in Admin > Settings > "
                 "Authentication? Is 'API Key Endpoint Restrictions' turned off? If the key is "
                 "not an admin key, some queries (status) are not allowed.",
            404: "Path or model not found. Check url and model in your profile.",
        }.get(status, "")

    def request_extras(self, think, num_ctx=None) -> dict:
        t = think_value(think)
        options = {"think": t, **sampling(self.model, t is not False)}
        if num_ctx:
            options["num_ctx"] = int(num_ctx)
        extras = {"options": options}
        if self.max_tokens:
            extras["max_tokens"] = self.max_tokens
        return extras

    # Ollama, through Open WebUI (admin key)
    def loaded(self) -> list[dict]:
        return self.request("GET", "/ollama/api/ps", timeout=30).get("models", [])

    def ollama_version(self) -> str:
        return str(self.request("GET", "/ollama/api/version", timeout=30).get("version", "?"))

    def model_card(self) -> dict:
        """The model's card in Ollama (/api/show): reasoning levels, parameters,
        capabilities... Does not load it on the GPU."""
        return self.request("POST", "/ollama/api/show", {"model": self.model}, timeout=30)

    def loaded_context(self) -> int | None:
        """num_ctx the server has the model loaded with right now (if it can be known)."""
        try:
            for m in self.loaded():
                if self.model in (m.get("name"), m.get("model")):
                    return int(m.get("context_length") or 0) or None
        except (APIError, ValueError, TypeError):
            return None
        return None

    def resolve_think(self, requested: str) -> tuple[str | None, str]:
        """'yes' becomes the model's default level if it has levels (qwen3.8: medium), so
        that the logs say which level it reasoned with."""
        try:
            card = self.model_card()
        except APIError:
            return requested, ""        # without an admin key it can't be checked: sent as is
        info = card.get("thinking") or {}
        values = info.get("values") or []
        levels = [v for v in values if isinstance(v, str)]
        if "thinking" not in (card.get("capabilities") or []) or not values:
            return ("no", "") if requested == "no" else \
                (None, f"{self.model} does not reason: use --think no.")
        if requested == "yes":
            return (info["default"] if isinstance(info.get("default"), str) else "yes"), ""
        if requested == "no" and False not in values:
            return None, f"{self.model} does not allow disabling reasoning."
        if requested == "no" or requested in levels:
            return requested, ""
        return None, (f"{self.model} does not support --think {requested}. Options: yes, no"
                      + "".join(f", {n}" for n in levels) + ".")

    def details(self) -> list[str]:
        lines = []
        try:
            lines.append(f"Ollama {self.ollama_version()}")
            loaded = self.loaded()
            if not loaded:
                lines.append("No model loaded on the GPU right now.")
            for m in loaded:
                vram = (m.get("size_vram") or 0) / 1e9
                lines.append(f"loaded: {m.get('name')} · context {m.get('context_length', '?')} "
                             f"tokens · {vram:.1f} GB in VRAM · expires "
                             f"{str(m.get('expires_at', '?'))[:19]}")
        except APIError as e:
            lines.append(f"(not available: {shorten(str(e), 120)})")
        try:
            f = self.model_card()
            det, info = f.get("details") or {}, f.get("model_info") or {}
            ctx = next((v for k, v in info.items() if k.endswith(".context_length")), "?")
            cap = f.get("capabilities") or []
            th = f.get("thinking") or {}
            levels = ", ".join("no" if v is False else "yes" if v is True else str(v)
                               for v in th.get("values") or []) or "does not reason"
            default = th.get("default")
            lines.append(f"{self.model}: {det.get('parameter_size', '?')} "
                         f"{det.get('quantization_level', '')} · native context {ctx}"
                         f" · tools: {'yes' if 'tools' in cap else 'no'}"
                         f" · images: {'yes' if 'vision' in cap else 'no'}")
            lines.append(f"reasoning (--think): {levels}"
                         + (f" · default {'yes' if default is True else default}"
                            if default is not None else ""))
            profile = profile_name(self.model)
            lines.append("sampling: " + (f"agent profile '{profile}'" if profile else
                                         "the Modelfile's (the agent does not know this family)"))
        except APIError as e:
            lines.append(f"(model card not available: {shorten(str(e), 120)})")
        return lines


class OpenAIProvider(OpenAIChatProvider):
    """Any API with OpenAI's chat format. The base URL usually ends in /v1."""
    name = "openai"
    label = "OpenAI-compatible API"
    default_url = "https://api.openai.com/v1"
    key_env = "OPENAI_API_KEY"
    key_required = False          # local servers (Ollama, LM Studio) need none
    details_title = "Model (OpenAI-compatible API)"

    def hint(self, status, path):
        return {
            401: "Key not recognized: check api_key in your profile (or OPENAI_API_KEY).",
            402: "No credit or quota left on that account.",
            403: "The key has no permission for this model or endpoint.",
            404: "Path or model not found: url must be the API's base (it usually ends in "
                 "/v1) and model an id the server lists (vibespice check shows them).",
        }.get(status, "")

    def request_extras(self, think, num_ctx=None) -> dict:
        extras = {}
        if think not in (None, "yes", "no"):
            extras["reasoning_effort"] = think
        # Only the standard sampling fields: OpenAI rejects the others (top_k)
        extras.update({k: v for k, v in sampling(self.model, think != "no").items()
                       if k in ("temperature", "top_p", "presence_penalty")})
        if self.max_tokens:
            extras["max_tokens"] = self.max_tokens
        return extras

    def stream_extras(self) -> dict:
        return {"stream_options": {"include_usage": True}}

    def resolve_think(self, requested: str) -> tuple[str | None, str]:
        if requested == "no":
            return None, ("An OpenAI-compatible API has no standard way to turn reasoning off. "
                          "Use --think yes (the model's default) or a level the server accepts "
                          "as reasoning_effort, e.g. --think low.")
        return requested, ""

    def details(self) -> list[str]:
        profile = profile_name(self.model)
        return [f"{self.model}: an OpenAI-compatible API does not describe its models",
                "reasoning (--think): yes (the model's default) or a level the server accepts "
                "as reasoning_effort (low, medium, high…)",
                "sampling: " + (f"agent profile '{profile}' (temperature, top_p and "
                                "presence_penalty)" if profile else "the server's defaults")]
