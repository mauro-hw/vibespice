# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Mauro Rodriguez Blasco
# Additional term under section 7(b) of the license: see NOTICE.
"""Terminal output: colors, number formatting, the waiting line, the desktop notification
and keeping the computer awake during a batch."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


GREEN, RED, YELLOW, BLUE, GREY, BOLD = "32", "31", "33", "36", "90", "1"


def shorten(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n].rstrip() + " …"


def fmt_tok(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt_dur(s: float) -> str:
    return f"{s:.1f} s" if s < 90 else f"{int(s // 60)} min {int(s % 60)} s"


class Heartbeat:
    """While waiting (for the model or a tool), rewrites a line with the elapsed time every few
    seconds. Only in a terminal: it writes nothing to files or pipes."""

    def __init__(self, text: str, every: float = 5.0):
        self.text, self.every = text, every
        self.active = sys.stdout.isatty()
        self._stop = threading.Event()

    def __enter__(self):
        if self.active:
            self._t0 = time.perf_counter()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            line = f"    ⏳ {self.text}… {fmt_dur(time.perf_counter() - self._t0)}"
            print("\r" + c(line, GREY) + "\033[K", end="", flush=True)

    def __exit__(self, *exc) -> bool:
        if self.active:
            self._stop.set()
            self._thread.join()
            print("\r\033[K", end="", flush=True)
        return False


def notify_done(title: str, text: str) -> None:
    """Desktop notification when a batch finishes (if there is a desktop and notify-send)."""
    if os.environ.get("VIBESPICE_NO_NOTIFY") or not (os.environ.get("DISPLAY")
                                                     or os.environ.get("WAYLAND_DISPLAY")):
        return
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "VibeSPICE", title, text],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def on_mains_power() -> bool:
    """Is the laptop plugged in? (A machine without a battery, like a Raspberry Pi, counts as yes.)"""
    has_battery = False
    for d in Path("/sys/class/power_supply").glob("*"):
        try:
            kind = (d / "type").read_text().strip()
            if kind == "Battery":
                has_battery = True
            elif kind in ("Mains", "USB") and (d / "online").read_text().strip() == "1":
                return True
        except OSError:
            continue
    return not has_battery


def keep_awake() -> str:
    """Stops the computer from suspending by itself while the agent works; says what it did.

    Idle suspend, always; lid-close suspend, only on mains power (on battery, a closed laptop
    still working could end up in a backpack). The screen still locks and you can suspend by
    hand. Each inhibitor is held by a process that ends with the agent (tail --pid), so it
    never stays behind even if the agent dies."""
    wait = ["tail", f"--pid={os.getpid()}", "-f", "/dev/null"]
    commands = []
    if "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "") \
            and shutil.which("gnome-session-inhibit"):
        commands.append(["gnome-session-inhibit", "--inhibit", "suspend", "--app-id", "vibespice",
                         "--reason", "VibeSPICE batch running", *wait])
    elif shutil.which("systemd-inhibit"):
        commands.append(["systemd-inhibit", "--what=sleep", "--who=VibeSPICE",
                         "--why=VibeSPICE batch running", *wait])
    lid = on_mains_power() and shutil.which("systemd-inhibit")
    if lid:
        commands.append(["systemd-inhibit", "--what=handle-lid-switch", "--who=VibeSPICE",
                         "--why=VibeSPICE batch running", *wait])
    done = 0
    for command in commands:
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            done += 1
        except OSError:
            pass
    if not done:
        return ""
    return ("The computer will not suspend by itself until it finishes"
            + ("; with the charger plugged in, not when closing the lid either" if lid
               else "; on battery, closing the lid does suspend it") + ".")


def tilde(path: Path) -> str:
    """A path to show: with ~ instead of the home folder."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text
