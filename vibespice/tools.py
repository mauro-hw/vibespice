# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""
Tools the agent offers to the model: ngspice and a few helpers.

Standard library only. Every tool returns text meant to be read by the model
(clear, short and with units).

Tools:
  - simulate             runs a netlist in ngspice
  - analyze_tolerances   corners or Monte Carlo over a netlist
  - standard_values      nearest E3...E96 values to a given one
  - calculate            safe calculator (LLMs are bad at mental arithmetic)
"""
from __future__ import annotations

import ast
import itertools
import json
import math
import os
import random
import re
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

NGSPICE = "ngspice"       # the executable; the configuration can change it
SIM_TIMEOUT = 30          # seconds per simulation
MAX_SAMPLES = 5000        # Monte Carlo cap
MAX_CORNER_COMPONENTS = 10
THREADS = max(1, min(8, os.cpu_count() or 2))


class NetlistError(ValueError):
    """Netlist problem, explained to the model so it can fix it."""


class SimulationError(RuntimeError):
    """ngspice could not be run."""


# ---------------------------------------------------------------------------
# Values with SPICE suffixes, and formatting
# ---------------------------------------------------------------------------
_SUFFIXES = [("meg", 1e6), ("mil", 25.4e-6), ("t", 1e12), ("g", 1e9), ("k", 1e3),
             ("m", 1e-3), ("u", 1e-6), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12), ("f", 1e-15)]
_RE_VALUE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-zµΩ%]*)$", re.I)


def spice_value(text) -> float:
    """'18k' -> 18000.0, '1meg' -> 1e6, '10u' -> 1e-5. Careful: 'm' is milli, as in SPICE."""
    if isinstance(text, bool):
        raise ValueError("non-numeric value")
    if isinstance(text, (int, float)):
        return float(text)
    t = str(text).strip().replace(" ", "")
    if re.fullmatch(r"[+-]?\d+,\d+[a-zµΩ%]*", t, re.I):   # decimal comma (e.g. 0,3871)
        t = t.replace(",", ".")
    m = _RE_VALUE.match(t)
    if not m:
        raise ValueError(f"unrecognized value: {text!r}")
    num, suffix = float(m.group(1)), m.group(2).lower()
    for s, mult in _SUFFIXES:
        if suffix.startswith(s):
            return num * mult
    return num


_PREFIXES = [(1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
             (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f")]


def fmt_eng(x: float, unit: str = "", digits: int = 6) -> str:
    """Readable engineering format: 0.000387097 A -> '387.097 µA'."""
    if x == 0 or not math.isfinite(x):
        return f"{x:g} {unit}".strip()
    ax = abs(x)
    mult, prefix = 1e-15, "f"
    for m, p in _PREFIXES:
        if ax >= m * 0.9999995:
            mult, prefix = m, p
            break
    return f"{x / mult:.{digits}g} {prefix}{unit}".strip()


def fmt_spice(x: float) -> str:
    """Format to paste into a netlist: 14000 -> '14k', 1.5e6 -> '1.5MEG'."""
    for mult, suffix in [(1e9, "G"), (1e6, "MEG"), (1e3, "k"), (1.0, ""),
                         (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")]:
        if abs(x) >= mult * 0.9999995:
            return f"{x / mult:.4g}{suffix}"
    return f"{x:.4g}"


def fmt_pct(x: float) -> str:
    return f"{x:+.4g} %"


# ---------------------------------------------------------------------------
# E series (preferred values)
# ---------------------------------------------------------------------------
_E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
        3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
_E96 = [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30,
        1.33, 1.37, 1.40, 1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74,
        1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32,
        2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09,
        3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12,
        4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49,
        5.62, 5.76, 5.90, 6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32,
        7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66, 8.87, 9.09, 9.31, 9.53, 9.76]
SERIES = {
    "E3": _E24[::8], "E6": _E24[::4], "E12": _E24[::2], "E24": _E24,
    "E48": _E96[::2], "E96": _E96,
}


def in_series(value: float, series: str, rel: float = 1e-6) -> bool:
    """Is 'value' (any decade) a value of the series?"""
    if value <= 0 or not math.isfinite(value):
        return False
    mantissa = value / 10 ** math.floor(math.log10(value))
    mantissa = round(mantissa, 9)
    if mantissa >= 9.9999999:
        mantissa /= 10
    return any(abs(mantissa - v) <= rel * v or abs(mantissa - 10 * v) <= rel * v
               for v in SERIES[series])


def series_of(value: float) -> list[str]:
    """E series a value belongs to (any decade)."""
    return [s for s in SERIES if in_series(value, s)]


def series_neighbors(value: float, series: str) -> tuple[float, float]:
    dec = math.floor(math.log10(value))
    cand = sorted(v * 10 ** d for d in (dec - 1, dec, dec + 1) for v in SERIES[series])
    below = max((c for c in cand if c <= value * (1 + 1e-9)), default=cand[0])
    above = min((c for c in cand if c >= value * (1 - 1e-9)), default=cand[-1])
    return below, above


# ---------------------------------------------------------------------------
# Netlist preparation and checks
# ---------------------------------------------------------------------------
_ALLOWED_DOTS = {
    "op", "dc", "ac", "tran", "noise", "tf", "sens", "pz", "disto", "four", "fourier",
    "meas", "measure", "param", "params", "model", "subckt", "ends", "func",
    "option", "options", "opt", "ic", "nodeset", "temp", "global", "save", "print",
    "plot", "probe", "width", "csparam",
}
_RUN_ANALYSES = {"dc", "ac", "tran", "noise", "tf", "sens", "pz", "disto"}
_RE_OUTPUT = re.compile(r"^[A-Za-z0-9_#().,+\-*/ ]{1,80}$")


def _ac_problems(line: str) -> list[str]:
    """A .ac line that ngspice would not take as meant. Models often write the points last
    ('.ac lin 900 1100 1'): ngspice only warns that it "assumes default parameter(s)",
    sweeps something else and every .meas looks in the wrong place."""
    parts = line.split()
    order = ("The order is .ac dec|oct|lin <points> <start frequency> <stop frequency>, with "
             "start < stop, e.g. .ac dec 100 10 100k")
    if len(parts) < 5 or parts[1].lower() not in ("dec", "oct", "lin"):
        return [f"'{line}': {order}."]
    try:
        n, f1, f2 = (spice_value(p) for p in parts[2:5])
    except ValueError:
        return []
    if f1 == f2:
        return [f"The .ac sweep goes from {parts[3]} to {parts[4]}: that is a single point, so "
                "a .meas WHEN cannot find any crossing."]
    if f1 > f2 or f1 <= 0 or n < 1 or n != int(n):
        return [f"'{line}' asks for {parts[2]} points from {parts[3]} to {parts[4]}, which "
                f"ngspice does not take as meant (it sweeps with default values instead, and "
                f"the .meas look in the wrong place). {order}."]
    return []


def prepare_netlist(netlist: str) -> tuple[list[str], dict]:
    """Cleans up the model's netlist and checks it. Returns (lines, info)."""
    if not isinstance(netlist, str) or not netlist.strip():
        raise NetlistError("The netlist is empty.")
    text = netlist.replace("\r\n", "\n").replace("\\n", "\n")
    text = re.sub(r"^\s*```[A-Za-z]*\s*$", "", text, flags=re.M)

    lines: list[str] = []
    info = {"analyses": set(), "meas": [], "meas_lines": {}, "warnings": [], "ground": False,
            "elements": {}, "first": None}
    for n, raw in enumerate(text.split("\n"), 1):
        l = raw.strip()
        if not l or l.startswith("*"):
            continue
        low = l.lower()
        if low.split()[0] in ("shell", "system", "exec", "load", "source"):
            raise NetlistError(f"Line {n}: '{l.split()[0]}' is not allowed in a netlist.")
        if l.startswith("."):
            cmd = low[1:].split()[0] if len(l) > 1 else ""
            if cmd == "end":
                continue
            if cmd == "step":
                raise NetlistError(
                    "ngspice does not support .step (it is an LTspice directive). To sweep "
                    "values, call 'simulate' several times or use 'analyze_tolerances'.")
            if cmd in ("control", "endc"):
                raise NetlistError(
                    "Do not include .control/.endc blocks: the tool generates its own. Use "
                    ".meas to measure in .tran/.ac/.dc; the DC operating point is always "
                    "printed.")
            if cmd == "op":
                # The tool already computes the operating point; next to .ac/.tran/.dc, an
                # .op of its own makes ngspice evaluate the .meas against it and fail silently
                info["op_removed"] = True
                continue
            if cmd in ("include", "inc", "lib", "osdi"):
                raise NetlistError(
                    f"Line {n}: .{cmd} is disabled in this environment. Write the models "
                    "(.model / .subckt) directly in the netlist.")
            if cmd not in _ALLOWED_DOTS:
                raise NetlistError(f"Line {n}: the .{cmd} directive is not allowed here.")
            if cmd in _RUN_ANALYSES:
                info["analyses"].add(cmd)
            if cmd in ("meas", "measure"):
                parts = low.split()
                if len(parts) >= 3:
                    info["meas"].append(parts[2])
                    info["meas_lines"][parts[2]] = low
            if cmd == "ac":
                info["warnings"] += _ac_problems(l)
            lines.append(l)
            continue
        if l.startswith("+"):
            lines.append(l)
            continue
        toks = l.split()
        if info["first"] is None:
            info["first"] = l
        name = toks[0].lower()
        info["elements"][name] = len(lines)
        if any(t.lower() in ("0", "gnd") for t in toks[1:5]):
            info["ground"] = True
        # Classic mistake: in SPICE 'M' is milli, not mega.
        if name[0] in "rlc" and len(toks) >= 4:
            v = toks[3].lower()
            if re.fullmatch(r"[\d.]+(e[+-]?\d+)?m(ohm|ω|f|h)?", v):
                info["warnings"].append(
                    f"{toks[0]} = {toks[3]}: in SPICE 'M' means MILLI (1e-3). "
                    "If you meant mega, write 'MEG' (e.g. 1MEG).")
        lines.append(l)
    if not info["elements"]:
        raise NetlistError("I can't find any component in the netlist.")
    if info.get("op_removed") and info["analyses"]:
        info["warnings"].append("I removed your .op line: the DC operating point is always "
                                "computed, and next to .ac/.tran/.dc it makes the .meas "
                                "statements fail silently.")
    if not info["ground"]:
        info["warnings"].append("I don't see the ground node '0' on any component: the circuit "
                                "needs a ground reference.")
    return lines, info


def _run(lines: list[str], control: list[str]) -> tuple[str, float]:
    """Runs ngspice with the netlist and a .control block generated here (reliable)."""
    content = ("* agent circuit\n" + "\n".join(lines) +
               "\n.control\nset noaskquit\n" + "\n".join(control) +
               "\nquit\n.endc\n.end\n")
    with tempfile.TemporaryDirectory(prefix="spice_agent_") as d:
        path = os.path.join(d, "circuit.cir")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        t0 = time.perf_counter()
        try:
            r = subprocess.run([NGSPICE, "circuit.cir"], cwd=d, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, errors="replace",
                               timeout=SIM_TIMEOUT)
        except FileNotFoundError:
            raise SimulationError(f"ngspice executable not found ('{NGSPICE}').")
        except subprocess.TimeoutExpired:
            raise SimulationError(f"The simulation exceeded {SIM_TIMEOUT} s and was cancelled "
                                  "(time step too small or a huge analysis?).")
    return (r.stdout or "") + "\n" + (r.stderr or ""), time.perf_counter() - t0


_RE_ASSIGN = re.compile(r"^\s*([^\s=]+)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\b(.*)$")
_ERROR_KEYS = ("error", "warning", "could not", "unknown", "not found", "singular",
               "timestep too small", "failed", "interrupted", "no such", "is not a valid",
               "cannot", "can't", "unrecognized", "undefined")


def _sections(output: str) -> dict[str, list[str]]:
    sec, current = {"_": []}, "_"
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("@@"):
            current = s[2:]
            sec.setdefault(current, [])
            continue
        sec[current].append(line)
    return sec


def _errors(output: str) -> list[str]:
    seen, out = set(), []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("Note:") or "compatibility mode" in s:
            continue
        if any(k in s.lower() for k in _ERROR_KEYS):
            # ngspice prints the offending line on the next line: add it
            if s.lower().endswith(("substitute:", "from line")) and i + 1 < len(lines):
                s = f"{s} {lines[i + 1].strip()}"
            s = s[:220]
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out[:12]


def _op_name(name: str) -> str:
    n = name.lower()
    if n.endswith("#branch"):
        return f"i({n[:-7]})"
    if "(" in n:
        return n
    return f"v({n})"


# Connections: the netlist translated back into words, so the model can see whether it is
# simulating the circuit it intended (it often writes transistor terminals in another order)
_TERMINALS = {"q": ("collector", "base", "emitter", "substrate"),
              "m": ("drain", "gate", "source", "bulk"),
              "j": ("drain", "gate", "source"), "d": ("anode", "cathode"),
              "v": ("+", "−"), "i": ("+", "−")}
_NODE_COUNT = {"r": 2, "c": 2, "l": 2, "e": 4, "g": 4, "f": 2, "h": 2, "b": 2, "s": 4, "w": 2,
               "t": 4}
_GROUNDS = ("0", "gnd")
MAX_CONNECTIONS = 14


def _elements(lines: list[str]) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """(name, type, [(terminal, node)]) of each component, excluding .subckt internals."""
    models = {}
    for l in lines:
        m = re.match(r"\.model\s+(\S+)\s+([a-z]+)", l, re.I)
        if m:
            models[m.group(1).lower()] = m.group(2).upper()
    out, depth = [], 0
    for l in lines:
        low = l.lower()
        if low.startswith(".subckt"):
            depth += 1
        elif low.startswith(".ends"):
            depth = max(0, depth - 1)
        if depth or l.startswith((".", "+")):
            continue
        toks = l.split()
        letter = toks[0][0].lower()
        kind = ""
        if letter in _TERMINALS:
            n = {"q": 3, "m": 4, "j": 3, "d": 2, "v": 2, "i": 2}[letter]
            if letter == "q" and len(toks) > 5 and toks[4].lower() not in models \
                    and toks[5].lower() in models:
                n = 4                               # with a substrate node
            if letter in "qmj" and len(toks) > n + 1:
                kind = models.get(toks[n + 1].lower(), "")
            terms = list(zip(_TERMINALS[letter][:n], toks[1:n + 1]))
        elif letter == "x":
            no_params = [t for t in toks[1:] if "=" not in t]
            terms = [("", t) for t in no_params[:-1]]
            kind = f"subcircuit {no_params[-1]}" if no_params else ""
        elif letter in _NODE_COUNT:
            terms = [("", t) for t in toks[1:_NODE_COUNT[letter] + 1]]
        else:
            continue
        out.append((toks[0], kind, terms))
    return out


def connections(lines: list[str]) -> list[str]:
    """Each component with its nodes, and what each node mostly touches (transistors, sources)."""
    elems = _elements(lines)
    roles: dict[str, list[tuple[str, str]]] = {}   # node -> [(component, "base of Q1")]
    uses: dict[str, int] = {}                      # node -> terminals touching it
    for name, _, terms in elems:
        for t, node in terms:
            uses[node.lower()] = uses.get(node.lower(), 0) + 1
            if name[0].lower() in "qmjdv":
                roles.setdefault(node.lower(), []).append((name, f"{t} of {name}"))

    def label(node: str, own: str) -> str:
        if node.lower() in _GROUNDS:
            return "ground"
        if uses.get(node.lower(), 0) == 1:
            return f"node {node} (floating: not connected to any other component)"
        p = [txt for c, txt in roles.get(node.lower(), []) if c != own][:2]
        return f"node {node}" + (f" ({', '.join(p)})" if p else "")

    out = []
    for name, kind, terms in sorted(elems, key=lambda e: e[0][0].lower() not in "qmj"):
        if name[0].lower() in _TERMINALS:
            body = " · ".join(f"{t} → {label(n, name)}" for t, n in terms)
        else:
            body = (" – " if len(terms) == 2 else ", ").join(label(n, name) for _, n in terms)
        out.append(name + (f" ({kind})" if kind else "") + f": {body}")
    if len(out) > MAX_CONNECTIONS:
        out = out[:MAX_CONNECTIONS] + [f"... and {len(out) - MAX_CONNECTIONS} more components"]
    return out


def numbered_nodes(lines: list[str]) -> list[str]:
    """Nodes with a numeric name (1, 2, 3...), not counting ground."""
    nodes = {n for _, _, terms in _elements(lines) for _, n in terms
             if n.isdigit() and n != "0"}
    return sorted(nodes, key=int)


# Quantities of each transistor after the operating point (parameters that exist in the
# tested ngspice models: BJT, level-1 and BSIM3 MOSFET, JFET)
_DEV_PARAMS = {"q": ("ic", "ib", "vbe", "vbc", "gm"), "m": ("id", "vgs", "vds", "gm"),
               "j": ("id", "vgs", "vgd", "gm")}
_DEV_ORDER = {"q": ("ic", "ib", "vbe", "vce", "gm"), "m": ("id", "vgs", "vds", "gm"),
              "j": ("id", "vgs", "vds", "gm")}
_DEV_LABEL = {"ic": "Ic", "ib": "Ib", "id": "Id", "vbe": "Vbe", "vce": "Vce", "vgs": "Vgs",
              "vds": "Vds", "gm": "gm"}
_DEV_UNIT = {"ic": "A", "ib": "A", "id": "A", "vbe": "V", "vce": "V", "vgs": "V", "vds": "V",
             "gm": "S"}

# ngspice .meas errors -> what to do (in the order they usually appear)
_MEAS_TIP_NUMBER = ("In .meas, the value must be a number (e.g. 0.7071068), not an "
                    "expression.")
_MEAS_TIPS = [
    ("out of interval", "A .meas WHEN did not find the crossing. Either the sweep does not "
                        "contain it (or it sits right at one end: sweep from one decade below to "
                        "one decade above the expected value), or the signal never reaches that "
                        "value: check that the simulated circuit is the one you intended "
                        "(connections, low-frequency gain)."),
    ("are not supported", _MEAS_TIP_NUMBER),
    ("equal sign missing", "In .meas, write the parameters with '=' and no spaces: "
                           "WHEN vm(out)=0.5, FROM=1k TO=10k."),
]
# WHEN <vector>=<value> or VAL=<value> in a .meas line (to spot expressions as values)
_RE_MEAS_VALUE = re.compile(r"(?:\bwhen\s+\S+?|\bval)\s*=\s*(\S+)", re.I)


def _meas_has_expression(line: str) -> bool:
    """Does a .meas line use an expression (not a plain number) as its WHEN/VAL value?"""
    for value in _RE_MEAS_VALUE.findall(line):
        try:
            spice_value(value)
        except ValueError:
            return True
    return False


def simulate_data(netlist: str) -> dict:
    """Simulates and returns structured data: op, meas, errors, warnings."""
    lines, info = prepare_netlist(netlist)
    # E series of each R, C and L: the model tends to treat non-standard values as standard
    series = {}
    for name, i in info["elements"].items():
        if name[0] in "rcl":
            toks = lines[i].split()
            try:
                series[toks[0]] = (toks[3], series_of(spice_value(toks[3])))
            except (IndexError, ValueError):
                pass
    control = ["op", "echo @@OP", "print all"]
    transistors = [n for n in info["elements"] if n[0] in _DEV_PARAMS]
    if transistors:         # one 'print' per parameter: a missing one does not kill the rest
        control += ["echo @@DEV"] + [f"print @{n}[{prm}]" for n in transistors
                                     for prm in _DEV_PARAMS[n[0]]]
    if info["analyses"]:
        control += ["echo @@RUN", "run"]
    control += ["echo @@END"]
    output, dt = _run(lines, control)
    sec = _sections(output)
    op = {}
    for line in sec.get("OP", []):
        m = _RE_ASSIGN.match(line)
        if m:
            op[_op_name(m.group(1))] = float(m.group(2))
    meas = {}
    meas_failed = []
    for line in sec.get("RUN", []):
        m = _RE_ASSIGN.match(line)
        if m and m.group(1).lower() in info["meas"]:
            meas[m.group(1).lower()] = float(m.group(2))
    devices: dict[str, dict] = {}
    for line in sec.get("DEV", []):
        m = re.match(r"\s*@(\w+)\[(\w+)\]\s*=\s*(\S+)", line)
        if m:
            try:
                devices.setdefault(m.group(1), {})[m.group(2)] = float(m.group(3))
            except ValueError:
                pass
    for name, v in devices.items():
        if not op or not all(math.isfinite(x) for x in v.values()):
            v.clear()                   # without an operating point the rest is garbage
            v["region"] = "no solution"
            continue
        if name[0] == "q" and {"vbe", "vbc"} <= v.keys():
            v["vce"] = v["vbe"] - v["vbc"]
            v["region"] = ("saturation" if v["vbc"] > 0.4 else "cutoff" if v["vbe"] < 0.4
                           else "active")
        if name[0] == "j" and {"vgs", "vgd"} <= v.keys():
            v["vds"] = v["vgs"] - v["vgd"]
    errors = _errors(output)
    if transistors:         # errors from our own @device queries, not from the circuit
        errors = [e for e in errors if "no such parameter" not in e.lower()
                  and not ("checkvalid" in e.lower() and "@" in e)]
    # ngspice prints "fc = 0" even when the .meas uses a vector that does not exist
    bad = [m.group(1).lower() for e in errors
           for m in [re.search(r"no such vector as (.+?)\.?$", e, re.I)] if m]
    for name in list(meas):
        if any(v in info["meas_lines"].get(name, "") for v in bad):
            del meas[name]
    for name in info["meas"]:
        if name not in meas:
            meas_failed.append(name)
    warnings = list(info["warnings"])
    sources: dict[frozenset, str] = {}
    for name, _, terms in _elements(lines):
        if name[0].lower() == "v" and len(terms) == 2:
            pair = frozenset("0" if n.lower() in _GROUNDS else n.lower() for _, n in terms)
            if pair in sources:
                warnings.append(f"{sources[pair]} and {name} are voltage sources in parallel "
                                "(between the same nodes): ngspice cannot solve that. Use a "
                                "single source or, if you are comparing two circuits in the "
                                "same netlist, give them different nodes.")
            sources.setdefault(pair, name)
    models = [m.group(1).lower() for l in lines
              for m in [re.match(r"\.model\s+(\S+)", l, re.I)] if m]
    for name in sorted({m for m in models if models.count(m) > 1}):
        warnings.append(f"There are {models.count(name)} .model statements named {name}: "
                        "ngspice uses only one of them for all. If you want variants (with a "
                        "different parameter), give them different names and use each one in "
                        "its component.")
    numbered = numbered_nodes(lines)
    if numbered:
        warnings.append(f"You are using numbered nodes ({', '.join(numbered[:6])}): name them "
                        "after their function (vcc, b, c, e, out...); that makes it easier to "
                        "see in 'Connections' whether each component goes where you intended.")
    for name, v in devices.items():
        if v.get("region") in ("saturation", "cutoff"):
            text = (f"{name.upper()} is in {v['region']}. If that is not what you wanted, "
                    "before changing values check in 'Connections' that each terminal goes "
                    "where you intended: in SPICE a BJT is written Q<name> collector base "
                    "emitter model.")
            if v["region"] == "saturation" and abs(v.get("ib", 0)) > abs(v.get("ic", 0)):
                text += (f" Here Ib ({fmt_eng(abs(v['ib']), 'A', 4)}) is larger than Ic: a "
                         "typical sign of swapped terminals.")
            warnings.append(text)
    for pattern, tip in _MEAS_TIPS:
        if any(pattern in e.lower() for e in errors) and tip not in warnings:
            warnings.append(tip)
    # Not every ngspice version says "are not supported" for an expression: check the line
    if any(_meas_has_expression(info["meas_lines"].get(n, "")) for n in meas_failed) \
            and _MEAS_TIP_NUMBER not in warnings:
        warnings.append(_MEAS_TIP_NUMBER)
    if meas_failed and "ac" in info["analyses"]:
        warnings.append("To measure in .ac use the ngspice vectors: vdb(out) (dB), vm(out) "
                        "(magnitude), vp(out) (phase). WHEN returns the crossing frequency "
                        "directly, e.g. .meas ac f1 WHEN vm(out)=0.5. With the source at AC 1, "
                        "vm(out) is already |Vout/Vin|. mag(), voltage ratios and 'find freq' "
                        "do not work. Also check that the .ac sweep contains the crossing.")
    if info["first"] and errors and " ".join(info["first"].lower().split())[:40] in \
            " ".join(output.lower().split()):
        warnings.append("The error is in your first line. Is it a title? You don't need one: "
                        "the tool adds the title. Remove it or turn it into a comment with '*'.")
    if any("singular" in e.lower() for e in errors):
        warnings.append("ngspice reports a SINGULAR MATRIX: the operating point is not reliable. "
                        "Check for floating nodes or nodes without a DC path to ground '0'.")
    return {"op": op, "meas": meas, "meas_failed": meas_failed, "errors": errors,
            "warnings": warnings, "time": dt, "analyses": sorted(info["analyses"]),
            "series": series, "devices": devices, "connections": connections(lines)}


def quantities_of(d: dict) -> dict[str, float]:
    """Named values of a simulation: v(out), i(vcc), the .meas and q1.ic, q1.vce..."""
    m = {**d.get("op", {}), **d.get("meas", {})}
    for dev, v in d.get("devices", {}).items():
        m.update({f"{dev}.{k}": x for k, x in v.items() if isinstance(x, (int, float))})
    return m


def numbers_of(d: dict) -> list[float]:
    """Every value ngspice returned in a simulation (for the origin check)."""
    return [abs(v) for v in quantities_of(d).values()]


def simulate(netlist: str) -> str:
    try:
        d = simulate_data(netlist)
    except (NetlistError, SimulationError) as e:
        return f"ERROR: {e}"
    parts = []
    ok = bool(d["op"]) or bool(d["meas"])
    parts.append(("Simulation run" if ok else "The simulation produced NO results")
                 + f" (ngspice, {d['time']:.2f} s).")
    if d["op"] and d["analyses"] and not any(d["op"].values()):
        parts.append("DC operating point: all 0 (the circuit only has AC excitation).")
    elif d["op"]:
        parts.append("DC operating point:")
        items = list(d["op"].items())
        for k, v in items[:40]:
            unit = "A" if k.startswith("i(") else "V"
            parts.append(f"  {k} = {fmt_eng(v, unit)}")
        if len(items) > 40:
            parts.append(f"  ... and {len(items) - 40} more values (not shown)")
        if any(k.startswith("i(v") for k in d["op"]):
            parts.append("  (SPICE convention: a negative i(Vx) = the source DELIVERS current.)")
    if d["devices"]:
        parts.append("Transistors (DC operating point):")
        for name, v in d["devices"].items():
            if v.get("region") == "no solution":
                parts.append(f"  {name.upper()}: no solution (ngspice did not find the "
                             "operating point; see the messages and warnings)")
                continue
            fields = [f"{_DEV_LABEL[k]} = {fmt_eng(v[k], _DEV_UNIT[k], 4)}"
                      for k in _DEV_ORDER[name[0]] if k in v]
            parts.append(f"  {name.upper()}: " + ", ".join(fields)
                         + (f" ({v['region']} region)" if "region" in v else ""))
    if d["connections"]:
        parts.append("Connections (compare them with the circuit you intended):")
        parts += [f"  {l}" for l in d["connections"]]
    if d["meas"]:
        parts.append("Measurements (.meas):")
        for k, v in d["meas"].items():
            parts.append(f"  {k} = {v:.7g}")
    if d["series"]:
        parts.append("E series of the values (compare them with the one the task asks for):")
        items = list(d["series"].items())
        for name, (text, series) in items[:20]:
            parts.append(f"  {name} = {text}: "
                         + (", ".join(series) if series else "not in any E series"))
        if len(items) > 20:
            parts.append(f"  ... and {len(items) - 20} more components (not shown)")
    if d["meas_failed"]:
        parts.append("Measurements that FAILED (check conditions/interval): "
                     + ", ".join(d["meas_failed"]))
    if d["errors"]:
        parts.append("ngspice messages:")
        parts += [f"  {e}" for e in d["errors"]]
    if d["warnings"]:
        parts.append("Warnings:")
        parts += [f"  - {a}" for a in d["warnings"]]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tolerances: corners and Monte Carlo
# ---------------------------------------------------------------------------
def _value_position(line: str) -> tuple[int, float]:
    """Index of the token holding the main value of an R, C, L, V or I, and that value."""
    toks = line.split()
    kind = toks[0][0].lower()
    if kind in "rcl":
        if len(toks) < 4 or "=" in toks[3] or "{" in toks[3]:
            raise NetlistError(f"I can't read the value of '{toks[0]}'. Write it as "
                               f"'{toks[0]} node1 node2 value' with a number (e.g. 10k).")
        return 3, spice_value(toks[3])
    if kind in "vi":
        for i, t in enumerate(toks[3:], 3):
            if t.lower() == "dc" and i + 1 < len(toks):
                return i + 1, spice_value(toks[i + 1])
        if len(toks) >= 4:
            try:
                return 3, spice_value(toks[3])
            except ValueError:
                pass
        raise NetlistError(f"I can't vary '{toks[0]}'. Write it as "
                           f"'{toks[0]} n+ n- DC value'.")
    raise NetlistError(f"'{toks[0]}': only resistors, capacitors, inductors and DC V/I sources "
                       "can be varied.")


def _measure(lines: list[str], output: str, meas_mode: bool) -> float | None:
    if meas_mode:
        text, _ = _run(lines, ["echo @@RUN", "run", "echo @@END"])
        for line in _sections(text).get("RUN", []):
            m = _RE_ASSIGN.match(line)
            if m and m.group(1).lower() == output.lower():
                return float(m.group(2))
        return None
    text, _ = _run(lines, ["op", "echo @@VAL", f"print {output}", "echo @@END"])
    for line in _sections(text).get("VAL", []):
        m = _RE_ASSIGN.match(line)
        if m:
            return float(m.group(2))
    return None


def tolerance_data(netlist: str, tolerances: dict, output: str, method: str = "corners",
                   samples: int = 1000, distribution: str = "gaussian",
                   target_value: float | None = None, error_limit_pct: float | None = None,
                   seed: int = 1) -> dict:
    lines, info = prepare_netlist(netlist)
    if not isinstance(tolerances, dict) or not tolerances:
        raise NetlistError("Give 'tolerances' as an object, e.g. {\"R1\": 1, \"R2\": 1} (in %).")
    output = str(output).strip()
    if not _RE_OUTPUT.match(output):
        raise NetlistError("'output' must be something like v(out), i(V1), v(out)/v(in) "
                           "or the name of a .meas in the netlist.")
    meas_mode = output.lower() in info["meas"]
    if not meas_mode and "(" not in output:
        raise NetlistError(f"'{output}' is neither a .meas in the netlist nor an expression "
                           "like v(out).")

    comps = []   # (name, line index, token position, nominal, relative tolerance)
    for name, tol in tolerances.items():
        key = str(name).lower()
        if key not in info["elements"]:
            raise NetlistError(f"Component '{name}' is not in the netlist.")
        idx = info["elements"][key]
        pos, nominal = _value_position(lines[idx])
        t = spice_value(str(tol).replace("%", "")) / 100.0
        if not 0 <= t < 1:
            raise NetlistError(f"Tolerance of '{name}' out of range: {tol} %.")
        comps.append((str(name), idx, pos, nominal, t))

    def variant(devs: tuple[float, ...]) -> list[str]:
        new = list(lines)
        for (name, idx, pos, nominal, t), d in zip(comps, devs):
            toks = new[idx].split()
            toks[pos] = f"{nominal * (1 + d):.12g}"
            new[idx] = " ".join(toks)
        return new

    t0 = time.perf_counter()
    nominal = _measure(lines, output, meas_mode)
    if nominal is None:
        raise SimulationError(f"Could not measure '{output}' in the nominal circuit. "
                              "Check the netlist with 'simulate' first.")
    target = float(target_value) if target_value not in (None, "") else nominal
    if target == 0:
        raise NetlistError("The target value cannot be 0 (the error is given in %).")

    method = str(method).lower().strip()
    if method.startswith(("cor", "wor")):
        method = "corners"
        if len(comps) > MAX_CORNER_COMPONENTS:
            raise NetlistError(f"Too many components for corners (max. "
                               f"{MAX_CORNER_COMPONENTS}); use Monte Carlo.")
        combos = list(itertools.product((-1, 1), repeat=len(comps)))
        devs = [tuple(s * c[4] for s, c in zip(combo, comps)) for combo in combos]
    elif method.startswith("mont") or method == "mc":
        method = "montecarlo"
        n = max(10, min(int(samples), MAX_SAMPLES))
        rnd = random.Random(int(seed))
        gauss = not str(distribution).lower().startswith("uni")
        distribution = "gaussian" if gauss else "uniform"

        def one(t: float) -> float:
            if t == 0:
                return 0.0
            if not gauss:
                return rnd.uniform(-t, t)
            while True:   # gaussian with 3σ = tolerance, truncated at ±tolerance
                x = rnd.gauss(0.0, t / 3)
                if abs(x) <= t:
                    return x
        devs = [tuple(one(c[4]) for c in comps) for _ in range(n)]
    else:
        raise NetlistError("'method' must be 'corners' or 'montecarlo'.")

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        values = list(ex.map(lambda d: _measure(variant(d), output, meas_mode), devs))
    good = [(d, v) for d, v in zip(devs, values) if v is not None]
    if not good:
        raise SimulationError("No variant produced a result.")
    vals = [v for _, v in good]
    err = lambda v: (v - target) / target * 100.0
    dmin, vmin = min(good, key=lambda x: x[1])
    dmax, vmax = max(good, key=lambda x: x[1])
    res = {
        "method": method, "output": output, "target": target, "nominal": nominal,
        "nominal_error_pct": err(nominal), "min": vmin, "max": vmax,
        "min_error_pct": err(vmin), "max_error_pct": err(vmax),
        "worst_error_pct": max(abs(err(v)) for v in vals + [nominal]),
        "dev_min": dmin, "dev_max": dmax, "components": [c[0] for c in comps],
        "tolerances_pct": [c[4] * 100 for c in comps], "simulations": len(devs) + 1,
        "failed": len(devs) - len(good), "time": time.perf_counter() - t0,
    }
    if method == "montecarlo":
        res.update(distribution=distribution, seed=int(seed),
                   mean=statistics.fmean(vals), sigma=statistics.pstdev(vals))
    if error_limit_pct not in (None, ""):
        lim = abs(float(error_limit_pct))
        res["limit_pct"] = lim
        res["within"] = sum(1 for v in vals if abs(err(v)) <= lim)
        res["yield_pct"] = res["within"] / len(vals) * 100.0
    return res


def analyze_tolerances(netlist: str, tolerances: dict, output: str,
                       method: str = "corners", samples: int = 1000,
                       distribution: str = "gaussian", target_value=None,
                       error_limit_pct=None, seed: int = 1) -> str:
    try:
        r = tolerance_data(netlist, tolerances, output, method, samples, distribution,
                           target_value, error_limit_pct, seed)
    except (NetlistError, SimulationError, ValueError, TypeError) as e:
        return f"ERROR: {e}"

    def desc(devs):
        return ", ".join(f"{n} {d * 100:+.3g} %" for n, d in zip(r["components"], devs))

    tols = ", ".join(f"{n} ±{t:g} %" for n, t in zip(r["components"], r["tolerances_pct"]))
    p = []
    if r["method"] == "corners":
        p.append(f"Worst-case analysis by CORNERS ({r['simulations']} simulations, "
                 f"{r['time']:.1f} s). Tolerances: {tols}.")
    else:
        p.append(f"MONTE CARLO: {r['simulations'] - 1} samples, {r['distribution']} "
                 "distribution" + (" (3σ = tolerance, truncated)" if
                                   r["distribution"] == "gaussian" else "")
                 + f", seed {r['seed']} ({r['time']:.1f} s). Tolerances: {tols}.")
    p.append(f"Output: {r['output']}   Reference for the error: {r['target']:.7g}")
    p.append(f"Nominal: {r['nominal']:.7g}  -> error {fmt_pct(r['nominal_error_pct'])}")
    p.append(f"Minimum: {r['min']:.7g}  -> error {fmt_pct(r['min_error_pct'])}   "
             f"[{desc(r['dev_min'])}]")
    p.append(f"Maximum: {r['max']:.7g}  -> error {fmt_pct(r['max_error_pct'])}   "
             f"[{desc(r['dev_max'])}]")
    p.append(f"Worst absolute error: {r['worst_error_pct']:.4g} %")
    if r["method"] == "montecarlo":
        p.append(f"Mean: {r['mean']:.7g}   Standard deviation: {r['sigma']:.4g} "
                 f"({r['sigma'] / abs(r['target']) * 100:.4g} % of the reference)")
    if "limit_pct" in r:
        if r["method"] == "corners":
            p.append(f"Limit ±{r['limit_pct']:g} %: "
                     + ("MET at every corner." if r["within"] == r["simulations"] - 1
                        and abs(r["nominal_error_pct"]) <= r["limit_pct"]
                        else "NOT MET at some corner."))
        else:
            p.append(f"Within ±{r['limit_pct']:g} %: {r['yield_pct']:.4g} % of the "
                     f"samples ({r['within']} of {r['simulations'] - 1}).")
    if r["failed"]:
        p.append(f"Warning: {r['failed']} simulations gave no result.")
    return "\n".join(p)


# ---------------------------------------------------------------------------
# Standard values and calculator
# ---------------------------------------------------------------------------
def standard_values(value=None, series: str = "E24") -> str:
    series = str(series).upper().strip()
    if series not in SERIES:
        return f"ERROR: unknown series '{series}'. Available: {', '.join(SERIES)}."
    if value in (None, ""):
        return f"Series {series} (one decade): " + ", ".join(f"{v:g}" for v in SERIES[series])
    try:
        x = spice_value(value)
    except ValueError as e:
        return f"ERROR: {e}"
    if x <= 0:
        return "ERROR: the value must be positive."
    below, above = series_neighbors(x, series)
    nearest = below if abs(below - x) <= abs(above - x) else above
    e = lambda v: (v - x) / x * 100
    return (f"Series {series}, requested value {fmt_spice(x)}:\n"
            f"  nearest: {fmt_spice(nearest)} ({fmt_pct(e(nearest))})\n"
            f"  next lower: {fmt_spice(below)} ({fmt_pct(e(below))})\n"
            f"  next higher: {fmt_spice(above)} ({fmt_pct(e(above))})")


def _parallel(*rs):
    return 1.0 / sum(1.0 / r for r in rs)


_FUNCTIONS = {
    "sqrt": math.sqrt, "log": math.log, "ln": math.log, "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
    "atan": math.atan, "atan2": math.atan2, "sinh": math.sinh, "cosh": math.cosh,
    "tanh": math.tanh, "abs": abs, "min": min, "max": max, "round": round,
    "floor": math.floor, "ceil": math.ceil, "hypot": math.hypot, "degrees": math.degrees,
    "radians": math.radians, "parallel": _parallel, "par": _parallel,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
        ast.Mod: lambda a, b: a % b, ast.FloorDiv: lambda a, b: a // b}
_RE_SUFFIX = re.compile(r"(?<![A-Za-z_\d.])(\d+\.?\d*|\.\d+)(meg|[kmunpµ])(?![A-Za-z_\d])",
                        re.I)


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(b) > 1000 or abs(a) > 1e100:
                raise ValueError("power too large")
            return a ** b
        if type(node.op) in _OPS:
            return _OPS[type(node.op)](a, b)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _FUNCTIONS and not node.keywords:
        return _FUNCTIONS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("expression not allowed (only numbers, + - * / ** %, parentheses and "
                     "math functions)")


def calculate(expression: str) -> str:
    if not isinstance(expression, str) or not expression.strip():
        return "ERROR: empty expression."
    lines = []
    for chunk in re.split(r"[;\n]", expression):
        t = chunk.strip()
        if not t:
            continue
        if len(t) > 300:
            lines.append(f"{t[:40]}... = ERROR: expression too long")
            continue
        py = t.replace("^", "**").replace("µ", "u")
        py = _RE_SUFFIX.sub(lambda m: f"({m.group(1)}*{spice_value('1' + m.group(2)):g})", py)
        try:
            v = _eval(ast.parse(py, mode="eval"))
            lines.append(f"{t} = {v:.10g}")
        except ZeroDivisionError:
            lines.append(f"{t} = ERROR: division by zero")
        except (ValueError, TypeError, SyntaxError, OverflowError) as e:
            lines.append(f"{t} = ERROR: {e}")
    return "\n".join(lines) if lines else "ERROR: empty expression."


# ---------------------------------------------------------------------------
# Schemas for the model (OpenAI "tools" format) and dispatcher
# ---------------------------------------------------------------------------
SCHEMAS = [
    {"type": "function", "function": {
        "name": "simulate",
        "description": (
            "Simulates a circuit with ngspice and returns the DC operating point (every node "
            "voltage and source current), the quantities of each transistor, the connections "
            "of each component in words and the results of the .meas statements. Netlist "
            "rules: no title line, no .control blocks, ground = node 0, 'k' = kilo, 'MEG' = "
            "mega ('M' is MILLI). For .tran/.ac/.dc add .meas to get numbers. .step and "
            ".include are not supported."),
        "parameters": {"type": "object", "properties": {
            "netlist": {"type": "string", "description":
                        "Complete SPICE netlist, one statement per line. E.g.:\n"
                        "V1 a 0 DC 9\nR1 a b 2.2k\nR2 b 0 4.7k\n.op"}},
            "required": ["netlist"]}}},
    {"type": "function", "function": {
        "name": "analyze_tolerances",
        "description": (
            "Analyzes how component tolerances affect an output. Method 'corners' (worst "
            "case, every ±tolerance combination) or 'montecarlo' (random samples). Returns "
            "nominal, minimum, maximum, errors in % relative to target_value and, if you give "
            "error_limit_pct, whether it is met or the % of samples within it."),
        "parameters": {"type": "object", "properties": {
            "netlist": {"type": "string", "description": "Netlist with the nominal values."},
            "tolerances": {"type": "object", "description":
                           "Tolerance in % per component, e.g. {\"R1\": 1, \"R2\": 1}. "
                           "Accepts R, C, L and DC V/I sources.",
                           "additionalProperties": {"type": "number"}},
            "output": {"type": "string", "description":
                       "What to measure: v(out), i(V1), v(out)/v(in)... or the name of a .meas."},
            "method": {"type": "string", "enum": ["corners", "montecarlo"]},
            "samples": {"type": "integer", "description": "Monte Carlo only (10-5000)."},
            "distribution": {"type": "string", "enum": ["gaussian", "uniform"],
                             "description": "Monte Carlo only. Gaussian: 3σ = tolerance."},
            "target_value": {"type": "number", "description":
                             "Ideal value of the output, used to compute the error (e.g. 5)."},
            "error_limit_pct": {"type": "number", "description":
                                "Allowed error limit in %, e.g. 2."},
            "seed": {"type": "integer", "description": "Random seed (Monte Carlo)."}},
            "required": ["netlist", "tolerances", "output"]}}},
    {"type": "function", "function": {
        "name": "standard_values",
        "description": ("Returns the standard values of an E series (E3, E6, E12, E24, E48, "
                        "E96) closest to a given value, with their error in %. Without "
                        "'value', lists the series."),
        "parameters": {"type": "object", "properties": {
            "value": {"type": "string", "description": "Desired value, e.g. '11.1k' or 11100."},
            "series": {"type": "string", "enum": list(SERIES)}},
            "required": ["series"]}}},
    {"type": "function", "function": {
        "name": "calculate",
        "description": ("Exact calculator. Supports + - * / ** ( ), functions (sqrt, log10, "
                        "exp, parallel(a,b,...)), the constants pi and e, and SPICE suffixes "
                        "(2.2k, 4.7u; 'm' = milli, 'meg' = mega). Several expressions separated "
                        "by ';'. Use it for any arithmetic."),
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string",
                           "description": "E.g.: 9*4.7k/(2.2k+4.7k); parallel(2.2k,4.7k)"}},
            "required": ["expression"]}}},
]


def schemas() -> list[dict]:
    """Tools offered to the model on this machine."""
    return list(SCHEMAS)


_DISPATCH = {
    "simulate": simulate,
    "analyze_tolerances": analyze_tolerances,
    "standard_values": standard_values,
    "calculate": calculate,
}


def run_tool(name: str, arguments) -> str:
    """Single entry point: validates and runs. Never raises."""
    if name not in _DISPATCH:
        return (f"ERROR: tool '{name}' does not exist. Available: "
                f"{', '.join(_DISPATCH)}.")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as e:
            return f"ERROR: the arguments are not valid JSON ({e}). Try again."
    if not isinstance(arguments, dict):
        return "ERROR: the arguments must be a JSON object."
    try:
        return _DISPATCH[name](**arguments)
    except TypeError as e:
        return f"ERROR: wrong arguments for '{name}': {e}"
    except Exception as e:  # noqa: BLE001 - the model must always get an answer
        return f"ERROR: unexpected error in '{name}': {type(e).__name__}: {e}"


def ngspice_version() -> str | None:
    try:
        r = subprocess.run([NGSPICE, "-v"], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL, errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"ngspice-?\s*([\w.+-]+)", r.stdout + r.stderr)
    return m.group(0) if m else (r.stdout.strip().splitlines() or ["?"])[0]
