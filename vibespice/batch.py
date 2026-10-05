# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""Running jobs one after another: time limits, estimates from the history and the summary
at the end."""
from __future__ import annotations

import argparse
import csv
import re
import statistics
import time
from datetime import datetime, timedelta

from . import agent
from .console import BOLD, GREY, RED, YELLOW, c, fmt_dur, keep_awake, notify_done, tilde


def estimate(done: int, total: int | None, t0: float, limit: float | None = None,
             name: str = "runs") -> str:
    """Progress line after each iteration: time spent and minutes left.

    Batches are measured with wall-clock time (time.time), not a monotonic clock: the latter
    does not advance while the computer is suspended, and the batch would run past its end."""
    elapsed = time.time() - t0
    avg = elapsed / done
    txt = (f"⏱ {done}" + (f"/{total}" if total else "") + f" {name} in {elapsed / 60:.0f} min "
           f"({avg / 60:.1f} min average)")
    left = total - done if total else None
    if limit:                             # only starts another one if it can finish it
        by_time = int(max(0.0, limit - elapsed) // avg) if avg else 0
        left = by_time if left is None else min(left, by_time)
    if left is not None:
        end = datetime.now() + timedelta(seconds=left * avg)
        txt += f" · ≈ {left * avg / 60:.0f} min left · end ≈ {end:%H:%M}"
    return txt


def fits_another(elapsed: float, avg: float, limit: float | None) -> bool:
    """Is there time for another iteration before the limit, given the average so far?"""
    return not limit or elapsed + avg <= limit


def limit_notice(limit: float, what: str) -> None:
    end = datetime.now() + timedelta(seconds=limit)
    print(c(f"⏱ Time limit: {limit / 60:.0f} min → finishes at the latest around "
            f"{end:%H:%M} (does not start {what} if there is no time to finish it)", GREY))


def history_seconds(challenge_id: str, model: str, think: str) -> list[float]:
    """Durations of similar earlier runs, taken from summary*.csv in the logs folder."""
    out = []
    for path in sorted(agent.LOGS.glob("summary*.csv")):
        try:
            with path.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if (row.get("challenge") == challenge_id and row.get("model") == model
                            and row.get("think") == think
                            and row.get("result") not in ("ERROR", "INTERRUPTED")):
                        try:
                            out.append(float(row["seconds"]))
                        except (KeyError, ValueError, TypeError):
                            pass
        except OSError:
            pass
    return out


def estimate_before(ids: list[str], times: int | None, model: str, think: str,
                    limit: float | None) -> None:
    """Before starting: how long each iteration and the batch usually take, from your history."""
    medians, no_data = {}, []
    for i in ids:
        h = history_seconds(i, model, think)
        if h:
            medians[i] = statistics.median(h)
        else:
            no_data.append(i)
    if not medians:
        return
    txt = "⏱ Based on your history, each run takes ≈ " + ", ".join(
        f"{m / 60:.0f} min (challenge {i})" for i, m in medians.items())
    total = sum(medians.values()) * times if times and not no_data else None
    if limit:
        total = min(total, limit) if total else limit
    if total:
        end = datetime.now() + timedelta(seconds=total)
        txt += f" → this batch, ≈ {total / 60:.0f} min (end ≈ {end:%H:%M})"
    if no_data:
        txt += f" · no history for challenge {', '.join(no_data)}"
    print(c(txt, GREY))


def duration(text: str) -> float:
    """'2h', '90m', '1h30', '45' (minutes) or '30s' → seconds."""
    t = text.lower().replace(" ", "")
    m = re.fullmatch(r"(\d+)s", t)
    if m:
        s = float(m.group(1))
    else:
        m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)(?:m|min)?)?", t)
        s = (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) * 60.0 if m else 0
    if s <= 0:
        raise argparse.ArgumentTypeError(f"invalid duration: '{text}' (e.g. 2h, 90m or 1h30)")
    return s


def run_batch(client: agent.OWUIClient, jobs: list[tuple], args, kind: str) -> int:
    """Runs each job (task, challenge or None, label) args.repeat times, within
    args.time_limit, and summarizes. kind goes to the logs: free, single, repeat or all."""
    args.think, error = agent.resolve_think(client, args.think)
    if error:
        print(c(error, RED))
        return 2
    args.batch_kind = kind
    if args.num_ctx == "auto":
        num_ctx = agent.loaded_context(client)
    else:
        num_ctx = int(args.num_ctx)
        print(c(f"⚠ You are requesting num_ctx={num_ctx}. If other users of the server use a "
                "different value, Ollama will reload the model every time your requests "
                "alternate.", YELLOW))

    awake = keep_awake()
    if awake:
        print(c(f"☕ {awake}", GREY))
    if kind != "free":
        estimate_before([ch.id for _, ch, _ in jobs], args.repeat, client.model, args.think,
                        args.time_limit)

    results = []
    total, t0, limit, stopped = len(jobs) * args.repeat, time.time(), args.time_limit, ""
    if limit:
        limit_notice(limit, "another run")
    try:
        for task, challenge, label in jobs:
            for n in range(1, args.repeat + 1):
                done, elapsed = len(results), time.time() - t0
                if limit and done and not fits_another(elapsed, elapsed / done, limit):
                    stopped = (f"after {done} of {total} runs (limit of {limit / 60:.0f} min, "
                               f"average {elapsed / done / 60:.1f} min)")
                    break
                r = agent.run_once(client, task, challenge, args, label, num_ctx,
                                   n if args.repeat > 1 else None)
                results.append((label, n, r))
                if total > 1:
                    print(c(estimate(len(results), total, t0, limit, "runs"), GREY))
            if stopped:
                break
    except KeyboardInterrupt:
        pass
    if stopped:
        print(c(f"\n⏱ Stopped by time {stopped}.", YELLOW))

    if len(results) > 1:
        print(c("\n═══ Summary ═══", BOLD))
        per_job: dict[str, list] = {}
        for label, n, r in results:
            per_job.setdefault(label, []).append(r)
        for label, items in per_job.items():
            passed = sum(1 for r in items if r["state"] == "PASS")
            times = [r["st"]["seconds"] for r in items if r["st"]]
            avg = sum(times) / len(times) if times else 0
            print(f"  {label}: {passed}/{len(items)} PASS · average time {fmt_dur(avg)} · "
                  + " ".join(("✅" if r["state"] == "PASS" else "❌") for r in items))
        print(c(f"Full history in {tilde(agent.LOGS / 'summary.csv')}", GREY))
    if len(results) > 1 or time.time() - t0 > 120:
        passed = sum(1 for _, _, r in results if r["state"] == "PASS")
        notify_done("vibespice: batch finished",
                    f"{passed}/{len(results)} PASS in {fmt_dur(time.time() - t0)}"
                    + (f" · stopped by time {stopped}" if stopped else ""))
    return 0
