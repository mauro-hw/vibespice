#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Mauro Rodriguez Blasco
"""
Statistics of the runs saved in logs/ (standard library only).

    python3 analyze_logs.py                       # everything in logs/
    python3 analyze_logs.py --challenge 6         # a single challenge
    python3 analyze_logs.py --last 12             # the 12 most recent runs
    python3 analyze_logs.py --since 20260929 --md report.md
    python3 analyze_logs.py --folder logs/before-change
    python3 analyze_logs.py --challenge 7 --by-version   # before / after a change

Groups by challenge, model, reasoning and batch kind, and counts what helps to learn how
the model works: pass rate, time, tool use, measurements that work, unmeasured results,
signs that it simulates another circuit (saturated or cut-off BJTs, numbered nodes) and
how much it reasons.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import spice_tools as hs

DIR = Path(__file__).resolve().parent
RE_BAD_REGION = re.compile(r"\((saturation|cutoff) region\)")


def failed_criteria(md_text: str) -> list[str]:
    """Verification criteria that failed, without their numbers ('Simulated Ic'...)."""
    if "## Verification" not in md_text:
        return []
    section = md_text.split("## Verification", 1)[1].split("\n## ", 1)[0]
    return [re.split(r"[\d(=:]", l[4:], maxsplit=1)[0].strip(" ,")
            for l in section.splitlines() if l.startswith("- ❌ ")]


def with_numbered_nodes(event: dict) -> bool:
    args = event.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return False
    try:
        return bool(hs.numbered_nodes(hs.prepare_netlist((args or {}).get("netlist", ""))[0]))
    except (hs.NetlistError, AttributeError):
        return False


def batch_kind(name: str, d: dict) -> str:
    if d.get("batch"):
        return d["batch"]
    return "repeat" if "_rep" in name else "single"


def read(folder: Path, args) -> list[dict]:
    runs = []
    files = sorted(folder.glob("*.json"))
    if args.since:
        files = [f for f in files if f.name[:8] >= args.since]
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict) or "events" not in d:
            continue
        if args.challenge and str(d.get("challenge")) != args.challenge:
            continue
        md = f.with_suffix(".md")
        md_text = md.read_text(encoding="utf-8") if md.exists() else ""
        r = d.get("summary") or {}
        sims = [e for e in d.get("events", []) if e.get("tool") == "simulate"]
        nudges = r.get("nudges") or {}
        runs.append({
            "file": f.name,
            "challenge": str(d.get("challenge") or "free"),
            "model": d.get("model", "?"),
            "think": d.get("think", "?"),
            "batch": batch_kind(f.name, d),
            "version": d.get("version", "?"),
            "code": d.get("code", "not recorded"),
            "result": d.get("result", "?"),
            "seconds": r.get("seconds"),
            "tool_use": r.get("tool_use") or {},
            "sims": len(sims),
            # simulations with .meas: all fine / some failed
            "meas_ok": sum("Measurements (.meas):" in e.get("result", "")
                           and "Measurements that FAILED" not in e.get("result", "")
                           for e in sims),
            "meas_bad": sum("Measurements that FAILED" in e.get("result", "") for e in sims),
            # simulations with BJTs and, of those, with some saturated or cut off
            "sims_q": sum("Transistors (DC operating point):" in e.get("result", "")
                          for e in sims),
            "q_bad": sum(bool(RE_BAD_REGION.search(e.get("result", ""))) for e in sims),
            "numbered": sum(with_numbered_nodes(e) for e in sims),
            "criteria": failed_criteria(md_text),
            "origin": nudges.get("origin", md_text.count("> ⚠ These values in your JSON")),
            "invented": bool(r.get("invented")),
            "reasoning": sum(len(e.get("reasoning") or "") for e in d.get("events", [])),
        })
    if args.last:
        runs = runs[-args.last:]
    return runs


def table(rows: list[list[str]], text: int = 4) -> list[str]:
    """The first `text` columns are left-aligned; the numeric ones, right-aligned."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for n, r in enumerate(rows):
        out.append("  ".join(c.ljust(widths[i]) if i < text else c.rjust(widths[i])
                             for i, c in enumerate(r)))
        if n == 0:
            out.append("  ".join("─" * w for w in widths))
    return out


def report(runs: list[dict], by_version: bool = False) -> list[str]:
    lines = []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in runs:
        key = (e["challenge"], e["model"], e["think"], e["batch"])
        groups[key + ((f"{e['version']} {e['code']}",) if by_version else ())].append(e)
    if groups:
        rows = [["challenge", "model", "think", "batch", *(["code"] * by_version),
                 "runs", "PASS", "min/run", "simulate", "sims .meas ok", "Q sat/cutoff",
                 "nodes 1,2,3", "origin nudges", "invented", "reason. k chars/run"]]
        for key in sorted(groups):
            g = groups[key]
            n = len(g)
            passed = sum(e["result"] == "PASS" for e in g)
            sec = [e["seconds"] for e in g if e["seconds"]]
            meas = sum(e["meas_ok"] + e["meas_bad"] for e in g)
            sims = sum(e["sims"] for e in g)
            sims_q = sum(e["sims_q"] for e in g)
            rows.append([*key, str(n), f"{passed}/{n}",
                         f"{statistics.mean(sec) / 60:.1f}" if sec else "–",
                         str(sims),
                         f"{sum(e['meas_ok'] for e in g)}/{meas}" if meas else "–",
                         f"{sum(e['q_bad'] for e in g)}/{sims_q}" if sims_q else "–",
                         f"{sum(e['numbered'] for e in g)}/{sims}" if sims else "–",
                         str(sum(e["origin"] for e in g)),
                         f"{sum(e['invented'] for e in g)}/{n}",
                         f"{statistics.mean(e['reasoning'] for e in g) / 1000:.0f}"])
        lines += table(rows, 5 if by_version else 4)
        lines += ["", "Failing criteria (in how many runs):"]
        for key in sorted(groups):
            count = Counter(c for e in groups[key] for c in set(e["criteria"]))
            lines.append(f"  {' · '.join(key)}: " + (", ".join(
                f"{c} ×{n}" for c, n in count.most_common()) or "none"))
        use = Counter()
        for e in runs:
            use.update(e["tool_use"])
        lines += ["", "Tools used in total: "
                  + (", ".join(f"{k} ×{v}" for k, v in use.most_common()) or "none")]
        codes = Counter(f"{e['version']} {e['code']}" for e in runs)
        lines.append("Code versions: " + ", ".join(f"{k} ({v})" for k, v in codes.most_common()))
    else:
        lines.append("No runs match the filter.")
    return lines


def main() -> int:
    p = argparse.ArgumentParser(description="Statistics of vibespice's logs/")
    p.add_argument("--folder", default=str(DIR / "logs"), help="folder with the .json files")
    p.add_argument("--challenge", help="only this challenge")
    p.add_argument("--since", help="only from this date on (YYYYMMDD)")
    p.add_argument("--last", type=int, help="only the N most recent runs")
    p.add_argument("--md", help="also save the report to this Markdown file")
    p.add_argument("--by-version", action="store_true",
                   help="also split by code version (compare before and after a change)")
    args = p.parse_args()
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Folder {folder} does not exist")
        return 2
    runs = read(folder, args)
    lines = [f"Logs in {folder} · {len(runs)} runs", ""] + report(runs, args.by_version)
    print("\n".join(lines))
    if args.md:
        Path(args.md).write_text("```\n" + "\n".join(lines) + "\n```\n", encoding="utf-8")
        print(f"\nReport saved to {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
