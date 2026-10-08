# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
Configuration and where things are saved, always outside the code folder.

- Configuration: ~/.config/vibespice/config.toml (or $XDG_CONFIG_HOME/vibespice/, or the
  file in VIBESPICE_CONFIG). One profile per server or API (provider anthropic, openai or
  openwebui); `vibespice init` creates it.
- Logs: ~/.local/share/vibespice/logs (or $XDG_DATA_HOME/vibespice/logs), logs_dir in the
  file, or VIBESPICE_LOGS.

Environment variables take priority over the file. Loading never fails: what is missing
or wrong goes to Settings.problems, so the commands that do not need a server (selftest,
analyze, the list of challenges) still work.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .console import tilde
from .providers import CLASSES

DEFAULT_TIMEOUT = 900.0          # seconds per model reply
EXAMPLE_KEY = "sk-ant-paste-your-key-here"

# Environment variable → key of the profile (or of the file, for ngspice and logs_dir)
ENV = {
    "VIBESPICE_PROVIDER": "provider",
    "VIBESPICE_URL": "url",
    "VIBESPICE_API_KEY": "api_key",
    "VIBESPICE_MODEL": "model",
    "VIBESPICE_CA": "ca",
    "VIBESPICE_TIMEOUT": "timeout",
    "VIBESPICE_STREAM": "stream",
    "VIBESPICE_MAX_TOKENS": "max_tokens",
}
TOP_KEYS = {"default_profile", "logs_dir", "ngspice", "profiles"}
PROFILE_KEYS = {"provider", "url", "api_key", "model", "ca", "timeout", "max_tokens",
                "fallbacks", "stream"}
KEY_HELP = {
    "anthropic": "create one in the Claude Console (https://platform.claude.com) and paste "
                 "it (it starts with sk-ant-), or set ANTHROPIC_API_KEY",
    "openwebui": "create one in Open WebUI, Settings > Account > API Keys (it starts with sk-)",
}

HEADER = """# VibeSPICE configuration.
# Keep this file private: it can hold API keys (chmod 600). Change it with any text editor,
# or run "vibespice init" again to add another model or API.
# Environment variables take priority over it: VIBESPICE_PROFILE, VIBESPICE_PROVIDER,
# VIBESPICE_URL, VIBESPICE_API_KEY, VIBESPICE_MODEL, VIBESPICE_LOGS...

# Profile used when you don't pass --profile
default_profile = "{default}"

# Optional: where the logs go (default: ~/.local/share/vibespice/logs)
# logs_dir = "~/vibespice-logs"

# Optional: the ngspice executable, if it is not in your PATH
# ngspice = "/usr/local/bin/ngspice"

# One profile per server or API. provider is "anthropic" (the Claude API), "openai" (any
# OpenAI-compatible API) or "openwebui".
"""

# Commented examples, by profile name: the file shows the ones it doesn't use
EXAMPLES = {
    "claude": """# The Claude API. Create a key in the Claude Console (https://platform.claude.com); the API
# is paid per use, separately from a Claude Pro or Max subscription. If api_key is empty,
# ANTHROPIC_API_KEY is used.
# [profiles.claude]
# provider = "anthropic"
# api_key = "sk-ant-..."
# model = "claude-opus-5-5"
""",
    "openai": """# OpenAI or any OpenAI-compatible API. url is the API's base (it usually ends in /v1). If
# api_key is empty, OPENAI_API_KEY is used; local servers need no key.
# [profiles.openai]
# provider = "openai"
# url = "https://api.openai.com/v1"
# api_key = "sk-..."
# model = ""                      # an id the API lists ("vibespice check" shows them)
""",
    "openrouter": """# OpenRouter: one key for many models
# [profiles.openrouter]
# provider = "openai"
# url = "https://openrouter.ai/api/v1"
# api_key = "sk-or-..."
# model = ""
""",
    "local": """# Ollama on this computer, through its OpenAI-compatible API (no key)
# [profiles.local]
# provider = "openai"
# url = "http://localhost:11434/v1"
# model = ""                      # a name from "ollama list"
""",
    "office": """# Open WebUI (Settings > Account > API Keys; an admin key also shows the loaded models)
# [profiles.office]
# provider = "openwebui"
# url = "http://localhost:3000"
# api_key = "sk-..."
# model = ""
""",
}

OPTIONAL = """# Optional in any profile:
# ca = "/path/to/your-ca.pem"     # CA certificate, for a server with its own HTTPS certificate
# timeout = 900                   # seconds to wait for each model reply (if it streams in: the
#                                 # longest pause allowed while it keeps coming)
# max_tokens = 16000              # cap on each reply (the Claude API needs one: 16000 by default;
#                                 # with streaming, VibeSPICE enforces it even if the server doesn't)
# fallbacks = false               # Claude: don't hand a declined request to a fallback model
# stream = false                  # Open WebUI and OpenAI-compatible APIs: the whole reply at once
#                                 # instead of in pieces (pieces keep proxies from cutting it)
"""

PLACEHOLDER_PROFILE = {"provider": "anthropic", "api_key": EXAMPLE_KEY, "model": "claude-opus-5-5"}
PLACEHOLDER_NOTE = """# The Claude API. Create a key in the Claude Console (https://platform.claude.com); the API
# is paid per use, separately from a Claude Pro or Max subscription.
# Paste your key (it starts with sk-ant-), or leave it empty and set ANTHROPIC_API_KEY.
"""


def profile_block(name: str, profile: dict, note: str = "") -> str:
    """A [profiles.NAME] table. Strings are written as JSON strings, which TOML accepts."""
    lines = [f"[profiles.{name}]"]
    for key in ("provider", "url", "api_key", "model", "ca", "timeout", "max_tokens",
                "fallbacks", "stream"):
        if key in profile:
            v = profile[key]
            lines.append(f"{key} = " + (("true" if v else "false") if isinstance(v, bool)
                                        else str(v) if isinstance(v, (int, float))
                                        else json.dumps(str(v))))
    return note + "\n".join(lines) + "\n"


def render(default: str, name: str, profile: dict, note: str = "") -> str:
    """A whole configuration file: the header, one profile and the commented examples."""
    examples = "\n".join(text for key, text in EXAMPLES.items() if key != name)
    return (HEADER.format(default=default) + "\n" + profile_block(name, profile, note) + "\n"
            + examples + "\n" + OPTIONAL)


TEMPLATE = render("claude", "claude", PLACEHOLDER_PROFILE, PLACEHOLDER_NOTE)


def _xdg(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable) or Path.home() / default) / "vibespice"


def config_file() -> Path:
    if os.environ.get("VIBESPICE_CONFIG"):
        return Path(os.environ["VIBESPICE_CONFIG"]).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "config.toml"


def default_logs_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "logs"


@dataclass
class Settings:
    file: Path
    file_exists: bool = False
    profile: str = ""                    # the profile in use ("" = only environment variables)
    profiles: list[str] = field(default_factory=list)
    provider: str = "openwebui"
    url: str = ""
    api_key: str = ""
    model: str = ""
    ca: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_tokens: int | None = None
    fallbacks: bool = True
    stream: bool = True
    ngspice: str = "ngspice"
    logs: Path = field(default_factory=default_logs_dir)
    from_env: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)     # they stop the commands that connect
    warnings: list[str] = field(default_factory=list)     # they don't


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def load(profile: str | None = None) -> Settings:
    """Reads the file and the environment. profile: the one asked for with --profile."""
    s = Settings(file=config_file())
    data: dict = {}
    if s.file.is_file():
        s.file_exists = True
        try:
            data = tomllib.loads(s.file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            s.problems.append(f"The file is not valid TOML: {e}")
        try:
            if s.file.stat().st_mode & 0o077:
                s.warnings.append(f"Other users of this computer can read it: chmod 600 "
                                  f"{tilde(s.file)}")
        except OSError:
            pass
    s.warnings += [f"Unknown setting '{k}' (a typo?)" for k in data if k not in TOP_KEYS]

    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    s.profiles = list(profiles)
    wanted = profile or os.environ.get("VIBESPICE_PROFILE") or _text(data.get("default_profile"))
    if not wanted and len(profiles) == 1:
        wanted = s.profiles[0]
    values: dict = {}
    if wanted:
        if wanted in profiles and isinstance(profiles[wanted], dict):
            s.profile = wanted
            values = dict(profiles[wanted])
            s.warnings += [f"Unknown setting '{k}' in profile '{wanted}' (a typo?)"
                           for k in values if k not in PROFILE_KEYS]
        else:
            s.problems.append(f"There is no profile '{wanted}' in the file. Profiles: "
                              + (", ".join(s.profiles) or "none"))
    elif len(profiles) > 1:
        s.problems.append(f"There are several profiles ({', '.join(s.profiles)}) and no "
                          "default_profile: set it in the file or pass --profile NAME")

    for variable, key in ENV.items():
        if os.environ.get(variable):
            values[key] = os.environ[variable]
            s.from_env.append(variable)
    s.provider = _text(values.get("provider")) or "openwebui"
    cls = CLASSES.get(s.provider)
    s.url = (_text(values.get("url")) or (cls.default_url if cls else "")).rstrip("/")
    s.api_key = _text(values.get("api_key"))
    if not s.api_key and cls and cls.key_env and os.environ.get(cls.key_env):
        s.api_key = os.environ[cls.key_env].strip()
        s.from_env.append(cls.key_env)
    s.model = _text(values.get("model"))
    s.ca = str(Path(_text(values["ca"])).expanduser()) if _text(values.get("ca")) else ""
    try:
        s.timeout = float(values.get("timeout") or DEFAULT_TIMEOUT)
        if s.timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        s.problems.append(f"timeout must be a number of seconds (now: {values.get('timeout')})")
    if values.get("max_tokens") not in (None, ""):
        try:
            s.max_tokens = int(values["max_tokens"])
            if s.max_tokens <= 0:
                raise ValueError
        except (TypeError, ValueError):
            s.problems.append(f"max_tokens must be a whole number (now: {values['max_tokens']})")
    if "fallbacks" in values:
        if isinstance(values["fallbacks"], bool):
            s.fallbacks = values["fallbacks"]
        else:
            s.problems.append(f"fallbacks must be true or false (now: {values['fallbacks']})")
    if "stream" in values:
        v = values["stream"]
        v = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False} \
            .get(v.strip().lower(), v) if isinstance(v, str) else v
        if isinstance(v, bool):
            s.stream = v
        else:
            s.problems.append(f"stream must be true or false (now: {values['stream']})")

    s.ngspice = os.environ.get("VIBESPICE_NGSPICE") or _text(data.get("ngspice")) or "ngspice"
    logs = os.environ.get("VIBESPICE_LOGS") or _text(data.get("logs_dir"))
    s.logs = Path(logs).expanduser() if logs else default_logs_dir()
    s.from_env += [v for v in ("VIBESPICE_NGSPICE", "VIBESPICE_LOGS") if os.environ.get(v)]

    if not cls:
        s.problems.append(f"Unknown provider '{s.provider}'. Supported: {', '.join(CLASSES)}")
    if not s.url:
        s.problems.append("url is missing: the address of your server, with http:// or https://")
    elif not s.url.startswith(("http://", "https://")):
        s.problems.append(f"url must start with http:// or https:// (now: {s.url})")
    if "paste-your-key" in s.api_key:
        s.problems.append("api_key still has the example value: paste your own key")
    elif not s.api_key and cls and cls.key_required:
        s.problems.append(f"api_key is missing: {KEY_HELP.get(s.provider, 'paste your key')}")
    return s


def diagnosis(s: Settings) -> str:
    """Explains what is missing, for when a command cannot connect."""
    lines = [f"Configuration: {tilde(s.file)}"]
    if s.file_exists:
        lines.append("  ✅ the file exists"
                     + (f" · profile '{s.profile}'" if s.profile else "")
                     + (f" (of: {', '.join(s.profiles)})" if len(s.profiles) > 1 else ""))
    else:
        lines.append("  ❌ That file does NOT exist. Create it with:  vibespice init")
    if s.from_env:
        lines.append(f"  Environment variables in use: {', '.join(s.from_env)}")
    lines += [f"  ❌ {p}" for p in s.problems]
    lines += [f"  ⚠ {w}" for w in s.warnings]
    return "\n".join(lines)


def _write_private(path: Path, text: str) -> None:
    """Writes the file readable only by its owner (600), replacing it in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def init() -> tuple[Path, bool]:
    """Creates the configuration file from the commented template, private (600). Returns
    (file, created); it never overwrites an existing one."""
    path = config_file()
    if path.exists():
        return path, False
    _write_private(path, TEMPLATE)
    return path, True


def create(name: str, profile: dict) -> Path:
    """Creates the configuration file with one profile, the default (it must not exist)."""
    path = config_file()
    if path.exists():
        raise FileExistsError(path)
    _write_private(path, render(name, name, profile))
    return path


def add_profile(name: str, profile: dict, make_default: bool) -> Path:
    """Appends a profile to the existing file and, if asked, makes it the default. The rest
    of the file stays as it is; if the result would not be valid, nothing is written."""
    path = config_file()
    text = path.read_text(encoding="utf-8-sig")
    new = text.rstrip("\n") + "\n\n" + profile_block(name, profile)
    if make_default:
        line = f'default_profile = "{name}"'
        new, n = re.subn(r"^default_profile\s*=.*$", line, new, count=1, flags=re.M)
        if not n:
            new = line + "\n" + new
    data = tomllib.loads(new)               # raises if something went wrong: nothing written
    if (data.get("profiles") or {}).get(name, {}).get("provider") != profile["provider"]:
        raise ValueError("the new profile could not be added")
    _write_private(path, new)
    return path
