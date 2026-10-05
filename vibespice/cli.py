# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Mauro Rodriguez Blasco
"""
Command line: vibespice <command>.

    vibespice run "Design a 24 V to 3.3 V divider with E12"   # the main use
    vibespice run --file task.txt
    vibespice bench                       # lists the challenges
    vibespice bench 2 --repeat 5 --think no
    vibespice bench all --time-limit 2h
    vibespice check | status | selftest
    vibespice analyze --by-version
    vibespice init                        # creates the configuration file

Without arguments, in a terminal, a guided menu asks what it needs and shows the command.
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import __version__, agent, analyze, batch, checks, config
from . import challenges as C
from .console import BLUE, BOLD, GREEN, GREY, RED, c, tilde


def prog_name() -> str:
    """How the user started us, so the commands we suggest work as they are."""
    return "python3 -m vibespice" if Path(sys.argv[0]).name == "__main__.py" else "vibespice"


def list_challenges(prog: str) -> None:
    print(c(f"Available challenges ({prog} bench ID, or {prog} bench all):", BOLD))
    for cid, ch in C.CHALLENGES.items():
        print(f"  {cid}  {ch.title}  " + c(f"[{', '.join(ch.tags)}]", GREY))
    print(c("\nSolutions are in vibespice/challenges.py and in the README (the model never "
            "sees them).", GREY))


def init(prog: str) -> int:
    path, created = config.init()
    if not created:
        print(f"The configuration file already exists: {tilde(path)}")
        print(c(f"Edit it with any text editor, then run: {prog} check", GREY))
        return 0
    print(c(f"✅ Created {tilde(path)} (only you can read it)", GREEN))
    print(f"Open it and fill in url, api_key and model in [profiles.main], for example:\n"
          f"  nano {tilde(path)}\nThen run:  {prog} check   (it lists the models if model is "
          "empty)")
    return 0


def status(client: agent.OWUIClient) -> int:
    try:
        for m in client.ollama_ps() or [{"name": "(no model loaded)"}]:
            print(f"{m.get('name')} · context {m.get('context_length', '-')} · "
                  f"VRAM {(m.get('size_vram') or 0) / 1e9:.1f} GB · expires "
                  f"{str(m.get('expires_at', '-'))[:19]}")
    except agent.APIError as e:
        print(c(str(e), RED))
        return 2
    return 0


# ---------------------------------------------------------------------------
# Guided menu
# ---------------------------------------------------------------------------
MENU = [
    ("Run a task: describe what to design or simulate", ["run", "{task}"]),
    ("Check the connection and the tools", ["check"]),
    ("Show the list of challenges", ["bench"]),
    ("Run a challenge once", ["bench", "{challenge}"]),
    ("Measure reliability: repetitions with a time limit",
     ["bench", "{challenge}", "--repeat", "{times}", "--time-limit", "{time}"]),
    ("Analyze the saved runs", ["analyze"]),
    ("Local test without AI (self-test)", ["selftest"]),
]


def menu(prog: str, ask=input) -> list[str] | None:
    """No arguments and in a terminal: guided menu that shows the command it will run."""
    print(c("vibespice — what do you want to do?", BOLD))
    for i, (text, _) in enumerate(MENU, 1):
        print(f"  {i}. {text}")
    print(c(f"  0. Quit   (all commands: {prog} -h)", GREY))
    try:
        choice = ask("Option: ").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            return None
        text, template = MENU[int(choice) - 1]
        values = {}
        if "{task}" in template:
            values["task"] = ask("What should it design or simulate? ").strip()
            if not values["task"]:
                return None
        if "{challenge}" in template:
            for cid, ch in C.CHALLENGES.items():
                print(c(f"     {cid}  {ch.title}", GREY))
            values["challenge"] = ask("Challenge [6]: ").strip() or "6"
        if "{times}" in template:
            values["times"] = ask("How many repetitions at most? [5]: ").strip() or "5"
        if "{time}" in template:
            values["time"] = ask("For how long? (e.g. 45m, 1h30) [45m]: ").strip() or "45m"
            batch.duration(values["time"])
        argv = [a.format(**values) for a in template]
        print(c(f"About to run:  {prog} {shlex.join(argv)}", BLUE))
        print(c("(next time you can type it directly)", GREY))
        if ask("Go ahead? [Y/n]: ").strip().lower() in ("n", "no"):
            return None
        return argv
    except (EOFError, KeyboardInterrupt, argparse.ArgumentTypeError) as e:
        if isinstance(e, argparse.ArgumentTypeError):
            print(c(str(e), RED))
        return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def build_parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog, description="Iterative SPICE simulation agent: a language model designs "
        "and simulates analog circuits with ngspice, on your computer.")
    p.add_argument("--version", action="version", version=f"vibespice {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")

    server = argparse.ArgumentParser(add_help=False)
    server.add_argument("--profile", help="profile of the configuration file to use (default: "
                                          "default_profile)")
    model = argparse.ArgumentParser(add_help=False, parents=[server])
    model.add_argument("--model", help="model id on the server (default: model in the profile)")
    model.add_argument("--think", default="yes",
                       help="reasoning: yes, no or a level the model supports (qwen3.8: low, "
                            "medium, xhigh; with 'yes', the model's default level)")
    model.add_argument("--mode", choices=["native", "text"], default="native",
                       help="native = the API's tool_calls; text = <tool_call> inside the text")
    model.add_argument("--max-steps", type=int, default=20, help="cap on model replies")
    model.add_argument("--num-ctx", default="auto",
                       help="'auto' = the one the server already has loaded (avoids reloads); "
                            "or a number")
    model.add_argument("--show-thinking", action="store_true", help="shows the reasoning")

    r = sub.add_parser("run", parents=[model], help="design or simulate what you ask for",
                       description="Gives the model a task and lets it simulate with ngspice "
                       "until it answers. Free tasks have no automatic verification.")
    r.add_argument("task", nargs="?", help="what to design or simulate, in quotes")
    r.add_argument("-f", "--file", help="read the task from a file ('-' = standard input)")
    r.set_defaults(repeat=1, time_limit=None)

    b = sub.add_parser("bench", parents=[model], help="evaluate the model with the challenges",
                       description="Runs challenges with independent verification. Without "
                       "IDs, lists them.")
    b.add_argument("challenges", nargs="*", metavar="ID", help="challenge IDs, or 'all'")
    b.add_argument("--repeat", type=int, default=1, help="repeats N times (measures reliability)")
    b.add_argument("--time-limit", type=batch.duration, metavar="T",
                   help="maximum batch time, e.g. 2h, 90m or 1h30; does not start an iteration "
                        "that cannot finish in time")

    k = sub.add_parser("check", parents=[server],
                       help="check ngspice, the connection, the model and tool calls")
    k.add_argument("--model", help="model id on the server (default: model in the profile)")
    sub.add_parser("status", parents=[server],
                   help="show which models the server has loaded (admin key)")
    sub.add_parser("selftest", help="local test without AI: ngspice, tools and verifiers")
    a = sub.add_parser("analyze", help="statistics of the saved runs")
    analyze.add_arguments(a)
    sub.add_parser("init", help="create the configuration file (it never overwrites it)")
    return p


def task_text(args) -> str | None:
    """The task of 'run', from the argument, a file or standard input."""
    if bool(args.task) == bool(args.file):
        print(c("Give the task in quotes or with --file (one of the two).", RED))
        return None
    if args.task:
        return args.task
    try:
        text = sys.stdin.read() if args.file == "-" else \
            Path(args.file).expanduser().read_text(encoding="utf-8")
    except OSError as e:
        print(c(f"Can't read the task: {e}", RED))
        return None
    if not text.strip():
        print(c("The task is empty.", RED))
        return None
    return text


def main(argv: list[str] | None = None) -> int:
    prog = prog_name()
    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty() and sys.stdout.isatty():
        argv = menu(prog)
        if argv is None:
            return 0
    p = build_parser(prog)
    args = p.parse_args(argv)

    if args.command is None:
        p.print_help()
        return 0
    if args.command == "init":
        return init(prog)
    settings = config.load(getattr(args, "profile", None))
    agent.configure(settings)
    if args.command == "selftest":
        return 1 if checks.selftest() else 0
    if args.command == "analyze":
        return analyze.run(args, agent.LOGS)

    if args.command == "run":
        text = task_text(args)
        if text is None:
            return 2
        jobs, kind = [(text, None, "free")], "free"
    elif args.command == "bench":
        if not args.challenges:
            list_challenges(prog)
            return 0
        ids = list(C.CHALLENGES) if "all" in args.challenges else args.challenges
        unknown = [i for i in ids if i not in C.CHALLENGES]
        if unknown:
            print(c(f"No challenge {', '.join(repr(i) for i in unknown)}. "
                    f"Run '{prog} bench' to list them.", RED))
            return 2
        jobs = [(C.CHALLENGES[i].message(), C.CHALLENGES[i], f"challenge{i}") for i in ids]
        kind = ("all" if "all" in args.challenges else "repeat" if args.repeat > 1
                else "single")

    try:
        client = agent.make_client(settings, getattr(args, "model", None),
                                   need_model=args.command in ("run", "bench"))
    except agent.APIError as e:
        if args.command == "check":
            checks.check(None, settings)
        print(c(str(e), RED))
        return 2

    if args.command == "check":
        return 1 if checks.check(client, settings) else 0
    if args.command == "status":
        return status(client)
    return batch.run_batch(client, jobs, args, kind)
