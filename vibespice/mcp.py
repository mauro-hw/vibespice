# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
vibespice as an MCP server: the tools, with no model and no API key.

A chat app (Claude Desktop, Claude Code, Codex, Gemini CLI...) starts `vibespice mcp` and
talks to it over standard input and output. The app's model, paid by the user's own
subscription, runs the loop; vibespice only runs the tools on this computer, with the
same safety limits as `vibespice run`.

Both generations of the protocol are served, request by request:
  - modern (2026-07-28): every request carries its version in _meta; no handshake.
  - legacy (2025-11-25 and earlier): an `initialize` handshake, then plain requests.
Only tools are offered. stdout carries protocol messages and nothing else; the rest goes
to stderr.

Standard library only.
"""
from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import sys
from pathlib import Path

from . import SOURCE_URL, __version__
from . import tools as hs

MODERN = ["2026-07-28"]
LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]   # newest first

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = \
    -32700, -32600, -32601, -32602, -32603
UNSUPPORTED_VERSION = -32022

VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"

SERVER_INFO = {"name": "vibespice", "title": "vibespice", "version": __version__,
               "websiteUrl": SOURCE_URL}
CAPABILITIES = {"tools": {"listChanged": False}}
CACHE = {"ttlMs": 3_600_000, "cacheScope": "public"}     # the tools only change with a new version

INSTRUCTIONS = f"""vibespice runs a real SPICE simulator (ngspice) on the user's computer. Use it to design and check analog circuits.
- Work iteratively: propose a circuit, simulate it, compare with the goal and adjust until it is met.
- Your memory of the E series and your mental arithmetic are not reliable: take standard values from standard_values and do arithmetic with calculate. Circuit numbers must come from the simulator; never make up results.
- Compare several candidates in one call: calculate accepts several expressions separated by ';', and one netlist can contain several independent circuits (with different nodes).
- If a tool returns an error, read it, fix the problem and try again.
- Netlists: no title line and no .control blocks; ground is node 0; k = kilo, MEG = mega, m = milli ('M' is milli too!). The DC operating point is always printed; for .tran/.ac/.dc use .meas. A negative i(V1) means the source is delivering current.
- Before delivering a design, simulate exactly that design and check every requirement against the numbers from the tools. If one is not met, even by a small margin, keep searching or explain why it is impossible.
- Give the user the final netlist and the key results, with units.
vibespice is free software (AGPL-3.0-only): {SOURCE_URL}"""

# Names for the app to show (a tool without one shows its name)
TITLES = {"simulate": "Simulate with ngspice", "analyze_tolerances": "Analyze tolerances",
          "standard_values": "Standard values (E series)", "calculate": "Calculator"}
# They read and compute, nothing else: no files of the user change, nothing leaves the computer
ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
               "openWorldHint": False}


class RPCError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def tool_list() -> list[dict]:
    """The tools of `vibespice run`, in MCP form and always in the same order."""
    return [{"name": f["name"], "title": TITLES.get(f["name"], f["name"]),
             "description": f["description"], "inputSchema": f["parameters"],
             "annotations": ANNOTATIONS}
            for f in (s["function"] for s in hs.schemas())]


def call_tool(params: dict) -> dict:
    """An unknown tool is a protocol error; anything the tool itself rejects goes back to
    the model as text, so that it can fix it."""
    name, arguments = params.get("name"), params.get("arguments")
    if name not in [s["function"]["name"] for s in hs.schemas()]:
        raise RPCError(INVALID_PARAMS, f"Unknown tool: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RPCError(INVALID_PARAMS, "arguments must be an object")
    text = hs.run_tool(name, arguments)
    return {"content": [{"type": "text", "text": text}], "isError": text.startswith("ERROR")}


def modern(method: str, params: dict) -> dict:
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = meta.get(VERSION_KEY)
    if not isinstance(version, str):
        raise RPCError(INVALID_PARAMS, f"_meta needs {VERSION_KEY}")
    if version not in MODERN:
        raise RPCError(UNSUPPORTED_VERSION, "Unsupported protocol version",
                       {"supported": MODERN, "requested": version})
    if not isinstance(meta.get(CAPABILITIES_KEY), dict):
        raise RPCError(INVALID_PARAMS, f"_meta needs {CAPABILITIES_KEY}")
    if method == "server/discover":
        result = {"supportedVersions": MODERN, "capabilities": CAPABILITIES,
                  "instructions": INSTRUCTIONS, **CACHE}
    elif method == "tools/list":
        result = {"tools": tool_list(), **CACHE}
    elif method == "tools/call":
        result = call_tool(params)
    else:
        raise RPCError(METHOD_NOT_FOUND, f"Method not found: {method}")
    return {"resultType": "complete", **result, "_meta": {SERVER_INFO_KEY: SERVER_INFO}}


def legacy(method: str, params: dict) -> dict:
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"protocolVersion": asked if asked in LEGACY else LEGACY[0],
                "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_list()}
    if method == "tools/call":
        return call_tool(params)
    raise RPCError(METHOD_NOT_FOUND, f"Method not found: {method}")


def error(rid, code: int, message: str, data=None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": e}


def handle(line: bytes) -> dict | None:
    """One message in, at most one reply out (notifications and responses get none)."""
    try:
        msg = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return error(None, PARSE_ERROR, f"Parse error: {e}")
    if not isinstance(msg, dict):
        return error(None, INVALID_REQUEST, "Invalid request: one JSON object per line "
                                            "(batches are not supported)")
    rid, method = msg.get("id"), msg.get("method")
    if not isinstance(method, str):
        # A response to a request of ours (we send none) is ignored; anything else is wrong
        return None if "result" in msg or "error" in msg else \
            error(rid, INVALID_REQUEST, "Invalid request: method is missing")
    if "id" not in msg:
        return None                      # notifications: initialized, cancelled...
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return error(rid, INVALID_PARAMS, "params must be an object")
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    try:
        era = modern if VERSION_KEY in meta or method == "server/discover" else legacy
        return {"jsonrpc": "2.0", "id": rid, "result": era(method, params)}
    except RPCError as e:
        return error(rid, e.code, e.message, e.data)
    except Exception as e:  # noqa: BLE001 - a bug here must not drop the app's connection
        return error(rid, INTERNAL_ERROR, f"Internal error: {type(e).__name__}: {e}")


def serve(stdin=None, stdout=None) -> int:
    """Answers requests one after another until the client closes standard input."""
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    sys.stdout = sys.stderr              # a stray print must never corrupt the protocol
    print(f"vibespice {__version__} MCP server · ngspice: "
          f"{hs.ngspice_version() or f'NOT FOUND ({hs.NGSPICE})'}", file=sys.stderr)
    for line in inp:
        if not line.strip():
            continue
        reply = handle(line)
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()
    return 0


# ---------------------------------------------------------------------------
# In a terminal: how to add it to each chat app
# ---------------------------------------------------------------------------
def executable() -> str | None:
    """The installed command, with its full path: chat apps do not start it from a
    terminal, so ~/.local/bin may not be in their PATH."""
    if Path(sys.argv[0]).name == "vibespice":
        return os.path.abspath(sys.argv[0])
    return shutil.which("vibespice")


def in_wsl() -> bool:
    return "microsoft" in platform.uname().release.lower()


SETUP = """vibespice mcp is started by your chat app, not by you. It lends the app's model the
vibespice tools (simulate, analyze_tolerances, standard_values, calculate), with no API key:
the model comes with your Claude, ChatGPT or Google account.
{warnings}
Add it to the app you use, once:

Claude Desktop: Settings → Developer → Edit Config{where}. Paste this (or only the
"vibespice" part, if the file already has other servers), save, quit Claude completely
and open it again.
{desktop}

Claude Code:
  claude mcp add --scope user vibespice -- {command}

Codex (ChatGPT account):
  codex mcp add vibespice -- {command}

Gemini CLI (Google account):
  gemini mcp add --scope user vibespice {command}

Then ask for a circuit in a new chat. More in the README ("Without an API key"):
{source}"""


def setup_text(exe: str | None, ngspice: str | None, wsl: bool) -> str:
    warnings = []
    if not exe:
        warnings.append("⚠ The app needs vibespice installed as a command. Install it with\n"
                        f"  pipx install git+{SOURCE_URL}\n"
                        "  (from a copy of the code: pipx install -e .) and run vibespice "
                        "mcp again.")
        exe = "vibespice"
    if not ngspice:
        warnings.append("⚠ ngspice is not installed, or not in your PATH: install it first "
                        "(README, step 2).")
    if wsl:     # Claude Desktop runs on Windows and starts vibespice inside WSL
        desktop = {"command": "wsl.exe", "args": ["-e", exe, "mcp"]}
    else:       # its PATH is not the terminal's: the full path of ngspice too
        desktop = {"command": exe, "args": ["mcp"]}
        if ngspice:
            desktop["env"] = {"VIBESPICE_NGSPICE": ngspice}
    config = json.dumps({"mcpServers": {"vibespice": desktop}}, indent=2)
    return SETUP.format(warnings="".join("\n" + w + "\n" for w in warnings),
                        where=" (in Windows)" if wsl else "",
                        desktop="\n".join("  " + l for l in config.splitlines()),
                        command=shlex.join([exe, "mcp"]), source=SOURCE_URL)


def setup() -> int:
    print(setup_text(executable(), shutil.which(hs.NGSPICE), in_wsl()))
    return 0
