"""Reading a window, and the check of its name."""

from __future__ import annotations

import re

from .jobs import Job
from .rapi import T32Error
from .language import explain_command, explain_unknown_window
from .replies import Reply, failed, tidy, warning
from .session import Session

WINDOW_NAME = "T32MCPV"

# WinPrint.<anything> runs <anything>: WinPrint.Go starts the target and
# WinPrint.OS.Hidden runs host commands. So the command name is checked here.
DISPLAY_WORDS = frozenset("""
    view state list listfunc listline listmodule listvar listtask listnesting dump mix auto browse
    info map watch chart statistic timing profilechart profilestatistic tree tasklist queue semaphore
    software hardware environment findall
""".split())
CHANGE_WORDS = frozenset("""
    set delete reset init load save store clear remove create add enable disable config on off
    program write erase go step up down attach mode select open close
""".split())

# Read-only windows with a change word in their names.
DISPLAY_EXCEPTIONS = frozenset({"symbol.list.program"})

# Found by tools/window_sweep.py on build 190766. Rerun it on a new TRACE32 version.
KNOWN_HANGS = {"var.draw": "Var.View"}

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")


def check_window(window: str) -> Reply | None:
    """None when the command may be sent."""
    next_step = ("use the full command name of a window. If it is a window that is not on the list, print it with "
                 "command: PRinTer.FILE <file>, then WinPrint.<window>. Anything else belongs in command.")
    if any(c in window for c in "\r\n"):
        return failed("window must be a single line", next_step=next_step)
    parts = window.split()
    if not parts or not _NAME.match(parts[0]):
        return failed(f"{window!r} is not a window command", next_step=next_step)
    name = parts[0].lower()
    if name in KNOWN_HANGS:
        return failed(f"{parts[0]} hangs PowerView when printed.", next_step=f"use {KNOWN_HANGS[name]} instead.")
    words = name.split(".")
    if name in DISPLAY_EXCEPTIONS:
        return None
    if not DISPLAY_WORDS.intersection(words) or CHANGE_WORDS.intersection(words):
        return failed(f"{parts[0]} is not on the list of windows view accepts", next_step=next_step)
    return None


class ViewJob(Job):
    def __init__(self, window: str, format: str = "text", max_chars: int = 12000):
        self.window = window
        self.file_type = {"text": "ASCIIE", "csv": "CSV"}[format]
        self.max_chars = max_chars
        self.label = window[:60]

    def start(self, s: Session) -> Reply:
        refusal = check_window(self.window)
        if refusal:
            refusal.label = self.label
            refusal.lines.extend(explain_unknown_window(s, self.window))
            return refusal
        path = s.directory / f"view-{s.op_id}.txt"
        running = s.target_state() == "running"
        printer = None if s.owned else s.conn.fnc("PRINTER.FILENAME()")

        # First opening: only for the message, because printing overwrites it.
        before = s.begin()
        s.conn.cmd(f"WinPOS ,,,,,,{WINDOW_NAME}")
        try:
            s.conn.cmd(self.window)
        except T32Error as exc:
            advice = explain_command(s, self.window, exc.text)
            if not advice:
                raise
            return failed(exc.text, f"  {self.window}", *advice, label=self.label)
        message = s.new_message(before)
        try:
            s.conn.cmd(f"WinCLEAR {WINDOW_NAME}")
        except T32Error:
            pass  # some commands with display names open no window

        # Second opening: the content. WinPRT would keep only what fits the window.
        try:
            s.conn.cmd(f'PRinTer.FILE "{path}"')
            s.conn.cmd(f"PRinTer.FileType {self.file_type}")
            s.conn.cmd(f"WinPrint.{self.window}")
        finally:
            if printer:
                s.conn.cmd(f'PRinTer.FILE "{printer}"')

        text = tidy(path.read_bytes().decode("latin-1")) if path.exists() else ""
        reply = Reply(content=text[: self.max_chars] if text else None, label=self.label)
        if not text:
            reply.lines.append("The window produced no output.")
            if self.file_type == "CSV":
                reply.lines.append('Next: use format="text". Chart windows have no CSV form.')
        if running:
            reply.tail.append("Warning: target was running, so this is a live snapshot.")
        if message and message.is_warning:
            reply.tail.append(warning(message.text))
        if len(text) > self.max_chars:
            reply.tail.append(f"Truncated: showing {self.max_chars} of {len(text)} characters. Full output: {path}")
        return reply
