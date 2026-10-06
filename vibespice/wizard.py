# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
vibespice init, in a terminal: asks which API to use, the key and the model, checks the key
against the API and writes the configuration file. With a file already there, it adds
another profile to it without touching the rest. Nothing is saved until the end.
"""
from __future__ import annotations

import difflib
import getpass
import os
from dataclasses import dataclass

from . import agent, checks, config
from .console import BOLD, GREEN, GREY, RED, YELLOW, c, tilde
from .providers import CLASSES, APIError

LIST_LIMIT = 30          # longer model lists are searched by name instead of numbered


@dataclass
class Choice:
    label: str
    hint: str
    name: str            # profile name
    provider: str
    url: str | None      # None: ask for it
    key: str             # "required", "optional" or "none"
    key_hint: str = ""
    model: str = ""      # suggested model


CHOICES = [
    Choice("Claude API (Anthropic)", "a key from the Claude Console; paid per use", "claude",
           "anthropic", "", "required", "it starts with sk-ant-", "claude-opus-5-5"),
    Choice("OpenAI", "a key from your OpenAI account", "openai", "openai",
           "https://api.openai.com/v1", "required", "it starts with sk-"),
    Choice("OpenRouter", "one key for many models", "openrouter", "openai",
           "https://openrouter.ai/api/v1", "required", "it starts with sk-or-"),
    Choice("Ollama on this computer", "free, no key", "local", "openai",
           "http://localhost:11434/v1", "none"),
    Choice("Another OpenAI-compatible API", "LM Studio, vLLM, a company server…", "custom",
           "openai", None, "optional"),
    Choice("Open WebUI", "a key from Settings > Account > API Keys", "openwebui", "openwebui",
           None, "required", "it starts with sk-"),
]


class Cancelled(Exception):
    pass


def run(prog: str, ask=input, secret=getpass.getpass, out=print,
        base_urls: dict[str, str] | None = None) -> int:
    """The questions. ask, secret and out can be replaced (the tests do); base_urls replaces
    the fixed URL of a choice, by profile name."""
    try:
        return _run(prog, ask, secret, out, base_urls or {})
    except (Cancelled, EOFError, KeyboardInterrupt):
        out(c("\nCancelled: nothing was saved.", YELLOW))
        return 1


def _ask(ask, prompt: str, default: str = "") -> str:
    answer = ask(prompt + (f" [{default}]" if default else "") + ": ").strip()
    return answer or default


def _yes(ask, prompt: str, default: bool) -> bool:
    answer = ask(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer in ("y", "yes", "s", "si", "sí")


def _run(prog, ask, secret, out, base_urls) -> int:
    path = config.config_file()
    existing = path.exists()
    profiles: list[str] = []
    out(c("VibeSPICE — which model will design your circuits?", BOLD))
    if existing:
        profiles = config.load().profiles
        out(f"You already have a configuration file: {tilde(path)}"
            + (f" (profiles: {', '.join(profiles)})" if profiles else ""))
        if not _yes(ask, "Add another model or API to it?", False):
            out(c(f"Nothing changed. You can also edit it with any text editor, then run: "
                  f"{prog} check", GREY))
            return 0

    # 1. Which API
    out("\nWhich API will you use?")
    for i, ch in enumerate(CHOICES, 1):
        out(f"  {i}. {ch.label}  " + c(f"({ch.hint})", GREY))
    while True:
        answer = _ask(ask, "Option", "1")
        if answer.isdigit() and 1 <= int(answer) <= len(CHOICES):
            choice = CHOICES[int(answer) - 1]
            break
        out(c(f"Type a number from 1 to {len(CHOICES)}.", RED))
    cls = CLASSES[choice.provider]

    name = choice.name
    while name in profiles:
        name = _ask(ask, f"There is already a profile called '{name}'. Name for this one")
    profile: dict = {"provider": choice.provider}

    # 2. Where and with which key, checked against the API
    fixed = base_urls.get(choice.name, choice.url)      # None: ask for it
    url = ""
    while True:
        url = _url(choice, ask, out, url) if fixed is None else (fixed or cls.default_url)
        key = _key(choice, cls, secret, out)
        effective = key or (os.environ.get(cls.key_env, "") if cls.key_env else "")
        out(c("Checking the connection…", GREY))
        try:
            models = cls(url, effective, "", timeout=30).models()
            out(c(f"✅ Connected: {len(models)} models available.", GREEN))
            break
        except APIError as e:
            out(c(f"❌ {e}", RED))
            answer = _ask(ask, "1 = try again · 2 = save it anyway · 0 = cancel", "1")
            if answer == "0":
                raise Cancelled
            if answer == "2":
                models = []
                break
    if url != cls.default_url:
        profile["url"] = url
    profile["api_key"] = key

    # 3. Which model
    profile["model"] = _model(choice, models, ask, out)

    # 4. Save
    make_default = not existing or _yes(ask, f"Use '{name}' by default?", True)
    if existing:
        config.add_profile(name, profile, make_default)
        out(c(f"\n✅ Added the profile '{name}' to {tilde(path)}", GREEN))
    else:
        config.create(name, profile)
        out(c(f"\n✅ Created {tilde(path)} (only you can read it)", GREEN))
    use = "" if make_default else f" --profile {name}"
    out(f"Next:  {prog} run{use} \"Design a 12 V to 5 V divider with E24 resistors\"")

    if models and _yes(ask, "Check it now with a short chat and a tool call?", True):
        settings = config.load(name)
        agent.configure(settings)
        try:
            provider = agent.make_provider(settings, need_model=False)
        except APIError as e:
            out(c(str(e), RED))
            return 1
        out("")
        return 1 if checks.check(provider, settings) else 0
    return 0


def _url(choice: Choice, ask, out, previous: str) -> str:
    hint = (" (it usually ends in /v1)" if choice.provider == "openai" else "")
    while True:
        url = _ask(ask, f"\nThe API's address, with http:// or https://{hint}", previous)
        if url.startswith(("http://", "https://")):
            return url.rstrip("/")
        out(c("It must start with http:// or https://.", RED))


def _key(choice: Choice, cls, secret, out) -> str:
    """The key, hidden while typing. Empty if there is none or it comes from the
    environment."""
    if choice.key == "none":
        return ""
    env = os.environ.get(cls.key_env, "") if cls.key_env else ""
    if env:
        out(c(f"\n{cls.key_env} is set: press Enter to use it, or paste another key.", GREY))
    while True:
        prompt = ("\nPaste your API key" + (f" ({choice.key_hint})" if choice.key_hint else "")
                  + (", or press Enter if it needs none" if choice.key == "optional" else "")
                  + ". It won't show on screen: ")
        key = secret(prompt).strip()
        if key:
            out(c(f"Key: {key[:6]}…{key[-3:]}" if len(key) > 12 else "Key received.", GREY))
            return key
        if env or choice.key == "optional":
            return ""
        out(c("This API needs a key. Paste it, or press Ctrl+C to cancel.", RED))


def _model(choice: Choice, models: list[str], ask, out) -> str:
    default = choice.model if not models or choice.model in models else ""
    if not models:
        return _ask(ask, "Model name (as the API lists it)", default)
    numbered = len(models) <= LIST_LIMIT
    if numbered:
        out("\nModels:")
        for i, m in enumerate(models, 1):
            out(f"  {i}. {m}" + (c("  (suggested)", GREY) if m == default else ""))
    else:
        out(f"\n{len(models)} models: type the name of one (a part of it shows the matches).")
    if choice.provider == "anthropic":
        out(c("claude-opus-5-5 designs best; claude-sonnet-5-5 costs half.", GREY))
    while True:
        answer = _ask(ask, "Model" + (" (number or name)" if numbered else ""), default)
        if numbered and answer.isdigit() and 1 <= int(answer) <= len(models):
            return models[int(answer) - 1]
        if answer in models:
            return answer
        if not answer:
            continue
        close = [m for m in models if answer.lower() in m.lower()][:10] \
            or difflib.get_close_matches(answer, models, n=5)
        if close:
            out(c("Not in the list. Did you mean: " + ", ".join(close), YELLOW))
        elif _yes(ask, f"'{answer}' is not in the list. Use it anyway?", False):
            return answer
