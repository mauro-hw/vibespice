# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
What every provider shares: the normalized reply, the conversation interface and HTTP with
retries (urllib, standard library only).

The agent loop only talks to a Provider and the Conversation it creates. The conversation
keeps the messages in the provider's own format and only ever appends to them: Claude
rejects a history whose earlier turns were edited, and the other APIs do not mind.

Replies can stream in (server-sent events): then timeout is the longest silence between two
pieces, not the whole reply, so a long reply that keeps coming is never cut, neither here nor
by a proxy in front of the server.
"""
from __future__ import annotations

import http.client
import json
import random
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..console import YELLOW, c

RETRIES = 4                  # after the first attempt, for 429, 5xx and dropped connections
MAX_WAIT = 120.0             # seconds, even if the server asks for more
RETRYABLE = {429, 500, 502, 503, 504, 529}

# Sampling recommended by each family's vendor, (with reasoning, without it), matched by the
# start of the model name. A model not listed here keeps its server's defaults. The cloud
# APIs of Anthropic and OpenAI decide their own sampling and never get these.
PROFILES = {
    "qwen3.8": ({"temperature": 1.0, "top_p": 0.95, "top_k": 20, "presence_penalty": 0.0},
                {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}),
    "qwen3:": ({"temperature": 0.6, "top_p": 0.95, "top_k": 20},
               {"temperature": 0.7, "top_p": 0.8, "top_k": 20}),
}


def profile_name(model: str) -> str | None:
    return next((prefix for prefix in PROFILES if model.startswith(prefix)), None)


def sampling(model: str, thinks: bool) -> dict:
    """The vendor's sampling for this model's family ({} if the agent does not know it)."""
    name = profile_name(model)
    return dict(PROFILES[name][0 if thinks else 1]) if name else {}


class APIError(RuntimeError):
    pass


class Retryable(Exception):
    def __init__(self, text: str, retry_after: float | None = None):
        super().__init__(text)
        self.retry_after = retry_after


@dataclass
class ToolCall:
    id: str
    name: str
    args: object                 # dict, or the raw text if it was not valid JSON
    wire: str = ""               # what to send back as arguments (OpenAI format)


@dataclass
class Usage:
    input: int = 0               # every input token of the request, cached ones included
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    speed: float | None = None   # output tokens per second, if the server says
    raw: dict = field(default_factory=dict)


@dataclass
class Reply:
    content: str                 # visible text, without reasoning
    reasoning: str               # reasoning, if the API returns it
    tool_calls: list[ToolCall]
    usage: Usage
    stop: str = "end"            # end, tool_use, max_tokens, refusal...
    raw_text: str = ""           # the text exactly as it came (for the log)
    notes: list[str] = field(default_factory=list)   # served by another model, refusal reason...
    raw: dict = field(default_factory=dict)


class Conversation:
    """The messages of one run, in the provider's format. Append-only."""

    def add_reply(self, reply: Reply) -> None:
        raise NotImplementedError

    def add_tool_results(self, results: list[tuple[ToolCall, str, bool]]) -> None:
        """(call, result text, is it an error) for every call of the last reply, together."""
        raise NotImplementedError

    def add_user(self, text: str) -> None:
        raise NotImplementedError


def tool_schemas(tools: list[dict] | None) -> list[dict]:
    """The tools as JSON Schema without the OpenAI wrapper: [{name, description, parameters}]."""
    return [t["function"] for t in tools or []]


class Provider:
    """A server or API that runs the model. Subclasses fill in the request and its parsing."""
    name = ""
    label = ""
    default_url = ""              # used when the profile has no url
    key_env = ""                  # environment variable with the key, if the profile has none
    key_required = True
    details_title = "Model"
    supports_num_ctx = False      # --num-ctx and the context the server has loaded
    supports_status = False       # vibespice status
    quick_think: str | None = None   # the cheapest reasoning, for the tests in `check`

    def __init__(self, url: str, key: str, model: str, ca: str = "", timeout: float = 900.0,
                 max_tokens: int | None = None, fallbacks: bool = True, stream: bool = True):
        self.url = url.rstrip("/")
        self.key = key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self.stream = stream          # replies in pieces, where the provider supports it
        self.progress = None          # progress(reasoning_chars, text_chars) while one streams
        self.ctx = ssl.create_default_context(cafile=ca) if ca else None

    # --- What each provider defines -----------------------------------------------------
    def headers(self, path: str) -> dict:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    def hint(self, status: int, path: str) -> str:
        """Advice for an HTTP error, appended to its message."""
        return ""

    def conversation(self, system: str | None, task: str,
                     tools: list[dict] | None) -> Conversation:
        raise NotImplementedError

    def chat(self, conv: Conversation, think, num_ctx: int | None = None) -> Reply:
        raise NotImplementedError

    def request_extras(self, think, num_ctx: int | None = None) -> dict:
        """What goes in every request besides the messages and tools (written to the log)."""
        return {}

    def models(self) -> list[str]:
        raise NotImplementedError

    def resolve_think(self, requested: str) -> tuple[str | None, str]:
        """Checks --think against what the model supports. Returns (value, error)."""
        return requested, ""

    def details(self) -> list[str]:
        """Lines about the server and the model for `check` (may raise APIError)."""
        return []

    def loaded_context(self) -> int | None:
        return None

    def loaded(self) -> list[dict]:
        raise APIError(f"The {self.name} provider cannot say which models are loaded.")

    # --- HTTP ---------------------------------------------------------------------------
    def request(self, method: str, path: str, body=None, timeout: float | None = None):
        """JSON request with retries for 429, 5xx and dropped connections."""
        for attempt in range(RETRIES + 1):
            try:
                return self._once(method, path, body, timeout)
            except Retryable as e:
                if attempt == RETRIES:
                    raise APIError(f"{e} (after {RETRIES + 1} attempts)") from None
                wait = e.retry_after if e.retry_after is not None \
                    else min(MAX_WAIT, 2.0 ** (attempt + 1)) * random.uniform(1.0, 1.25)
                wait = min(MAX_WAIT, max(0.0, wait))
                print(c(f"    ⚠ {e}: retrying in {wait:.0f} s ({attempt + 1}/{RETRIES})",
                        YELLOW))
                time.sleep(wait)
        raise AssertionError("unreachable")

    def stream_events(self, path: str, body: dict):
        """POST whose reply is a stream of server-sent events: yields each 'data:' event as a
        dict. It retries like request() only until the stream starts; after that, timeout is
        the longest silence allowed between two pieces."""
        for attempt in range(RETRIES + 1):
            try:
                r = self._open("POST", path, body, None, "text/event-stream")
                break
            except Retryable as e:
                if attempt == RETRIES:
                    raise APIError(f"{e} (after {RETRIES + 1} attempts)") from None
                wait = e.retry_after if e.retry_after is not None \
                    else min(MAX_WAIT, 2.0 ** (attempt + 1)) * random.uniform(1.0, 1.25)
                wait = min(MAX_WAIT, max(0.0, wait))
                print(c(f"    ⚠ {e}: retrying in {wait:.0f} s ({attempt + 1}/{RETRIES})",
                        YELLOW))
                time.sleep(wait)
        with r:
            try:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        yield event
            except TimeoutError:
                raise APIError(f"The reply stopped coming for {self.timeout:.0f} s (raise "
                               "timeout in your profile if the server pauses for long).") \
                    from None
            except (ConnectionResetError, ConnectionAbortedError, http.client.IncompleteRead):
                raise APIError(f"The connection with {self.url} dropped while the reply was "
                               "coming in.") from None

    def _once(self, method: str, path: str, body, timeout: float | None):
        try:
            with self._open(method, path, body, timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (ConnectionResetError, ConnectionAbortedError):
            raise Retryable(f"connection dropped by {self.url}") from None
        except TimeoutError:
            raise APIError(f"No reply within {timeout or self.timeout:.0f} s (raise timeout in "
                           "your profile if the model thinks for long).") from None
        except json.JSONDecodeError:
            raise APIError(f"Non-JSON reply at {path}. Does url point to a {self.label} "
                           "server?") from None

    def _open(self, method: str, path: str, body, timeout: float | None,
              accept: str = "application/json"):
        """The open response (the caller closes it), or Retryable / APIError."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": accept,
                     **self.headers(path)})
        wait = timeout or self.timeout
        try:
            return urllib.request.urlopen(req, timeout=wait, context=self.ctx)
        except urllib.error.HTTPError as e:
            detail = error_detail(e.read().decode("utf-8", "replace"))
            text = f"HTTP {e.code} at {path}: {detail}"
            if e.code in RETRYABLE:
                raise Retryable(text, retry_after(e.headers.get("retry-after"))) from None
            hint = self.hint(e.code, path)
            raise APIError(text + (f" → {hint}" if hint else "")) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (ConnectionResetError, ConnectionAbortedError)):
                raise Retryable(f"connection dropped by {self.url}") from None
            raise APIError(f"Can't connect to {self.url} ({e.reason}). Are the URL and port "
                           "right? Can this machine reach the server?") from None
        except (ConnectionResetError, ConnectionAbortedError):
            raise Retryable(f"connection dropped by {self.url}") from None
        except TimeoutError:
            raise APIError(f"No reply within {wait:.0f} s (raise timeout in your profile if "
                           "the model thinks for long).") from None


def error_detail(text: str) -> str:
    """The message of an error body: OpenAI and Anthropic ({"error": {...}}), Open WebUI
    ({"detail": ...}) or plain text."""
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return text[:600]
    if isinstance(d, dict):
        err = d.get("error")
        if isinstance(err, dict) and err.get("message"):
            kind = f"{err['type']}: " if err.get("type") else ""
            return (kind + str(err["message"]))[:600]
        if d.get("detail"):
            return str(d["detail"])[:600]
    return text[:600]


def retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


_RE_THINK = re.compile(r"<think>(.*?)</think>", re.S)
_RE_DETAILS = re.compile(r'<details type="reasoning".*?>(.*?)</details>', re.S)


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
