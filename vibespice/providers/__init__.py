# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
The servers and APIs VibeSPICE can use (the provider of a profile):

- openwebui  Open WebUI, which forwards to Ollama (reasoning levels, loaded models)
- openai     any OpenAI-compatible API: OpenAI, OpenRouter, Ollama's /v1, vLLM, LM Studio...
- anthropic  the Claude API
"""
from __future__ import annotations

from .anthropic import AnthropicProvider
from .base import (PROFILES, APIError, Conversation, Provider, Reply, ToolCall, Usage,
                   profile_name, split_thinking)
from .openai_chat import OpenAIProvider, OpenWebUIProvider

CLASSES: dict[str, type[Provider]] = {
    p.name: p for p in (OpenWebUIProvider, OpenAIProvider, AnthropicProvider)}

__all__ = ["CLASSES", "PROFILES", "APIError", "Conversation", "Provider", "Reply", "ToolCall",
           "Usage", "profile_name", "split_thinking"]
