# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Mauro Rodriguez Blasco
"""
Configuration and where things are saved, always outside the code folder.

- Configuration: ~/.config/vibespice/config.toml (or $XDG_CONFIG_HOME/vibespice/, or the
  file in VIBESPICE_CONFIG). One profile per server or API; `vibespice init` creates it.
- Logs: ~/.local/share/vibespice/logs (or $XDG_DATA_HOME/vibespice/logs), logs_dir in the
  file, or VIBESPICE_LOGS.

Environment variables take priority over the file. Loading never fails: what is missing
or wrong goes to Settings.problems, so the commands that do not need a server (selftest,
analyze, the list of challenges) still work.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .console import tilde

PROVIDERS = ("openwebui",)
DEFAULT_TIMEOUT = 900.0          # seconds per model reply
EXAMPLE_KEY = "sk-paste-your-key-here"

# Environment variable → key of the profile (or of the file, for ngspice and logs_dir)
ENV = {
    "VIBESPICE_PROVIDER": "provider",
    "VIBESPICE_URL": "url",
    "VIBESPICE_API_KEY": "api_key",
    "VIBESPICE_MODEL": "model",
    "VIBESPICE_CA": "ca",
    "VIBESPICE_TIMEOUT": "timeout",
}
TOP_KEYS = {"default_profile", "logs_dir", "ngspice", "profiles"}
PROFILE_KEYS = {"provider", "url", "api_key", "model", "ca", "timeout"}

TEMPLATE = f'''# vibespice configuration.
# Keep this file private: it holds your API key (chmod 600).
# Environment variables take priority over it: VIBESPICE_PROFILE, VIBESPICE_URL,
# VIBESPICE_API_KEY, VIBESPICE_MODEL, VIBESPICE_LOGS...

# Profile used when you don't pass --profile
default_profile = "main"

# Optional: where the logs go (default: ~/.local/share/vibespice/logs)
# logs_dir = "~/vibespice-logs"

# Optional: the ngspice executable, if it is not in your PATH
# ngspice = "/usr/local/bin/ngspice"

# One profile per server or API. For now the only provider is "openwebui".
[profiles.main]
provider = "openwebui"
# Your Open WebUI server, with http:// or https://
url = "http://localhost:3000"
# Settings > Account > API Keys in Open WebUI (starts with sk-)
api_key = "{EXAMPLE_KEY}"
# The model id as the server lists it ("vibespice check" shows them)
model = ""
# Optional: CA certificate if your server uses its own HTTPS certificate
# ca = "/path/to/your-ca.pem"
# Optional: seconds to wait for each model reply
# timeout = 900

# Another server: copy the block with another name and use --profile NAME
# [profiles.other]
# provider = "openwebui"
# url = "https://llm.example.com"
# api_key = "sk-..."
# model = "qwen3:32b"
'''


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
    s.url = _text(values.get("url")).rstrip("/")
    s.api_key = _text(values.get("api_key"))
    s.model = _text(values.get("model"))
    s.ca = str(Path(_text(values["ca"])).expanduser()) if _text(values.get("ca")) else ""
    try:
        s.timeout = float(values.get("timeout") or DEFAULT_TIMEOUT)
        if s.timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        s.problems.append(f"timeout must be a number of seconds (now: {values.get('timeout')})")

    s.ngspice = os.environ.get("VIBESPICE_NGSPICE") or _text(data.get("ngspice")) or "ngspice"
    logs = os.environ.get("VIBESPICE_LOGS") or _text(data.get("logs_dir"))
    s.logs = Path(logs).expanduser() if logs else default_logs_dir()
    s.from_env += [v for v in ("VIBESPICE_NGSPICE", "VIBESPICE_LOGS") if os.environ.get(v)]

    if s.provider not in PROVIDERS:
        s.problems.append(f"Unknown provider '{s.provider}'. Supported: {', '.join(PROVIDERS)}")
    if not s.url:
        s.problems.append("url is missing: the address of your server, with http:// or https://")
    elif not s.url.startswith(("http://", "https://")):
        s.problems.append(f"url must start with http:// or https:// (now: {s.url})")
    if not s.api_key:
        s.problems.append("api_key is missing: create one in Open WebUI, Settings > Account > "
                          "API Keys (it starts with sk-)")
    elif s.api_key == EXAMPLE_KEY:
        s.problems.append("api_key still has the example value: paste your own key")
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


def init() -> tuple[Path, bool]:
    """Creates the configuration file from the template, private (600). Returns (file,
    created); it never overwrites an existing one."""
    path = config_file()
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(TEMPLATE)
    return path, True
