"""The reply to a line that failed.

An agent is usually sure of the name it got wrong, so it does not look anything up. The failure
is the one moment it is listening. TRACE32 itself says whether a name exists; the manuals give
the syntax and the names close by.

Nothing here ever turns a failure into a success or the other way round.
"""

from __future__ import annotations

import re
from functools import wraps
from pathlib import Path

from .manuals import CUT, NAME, Entry, Help, load_help
from .rapi import LinkError, T32Error

MAX_ADVICE = 500
NO_FUNCTION = re.compile(r"no function '([^']+)' exists")
COMMAND_ERRORS = ("unknown command", "error while loading extension", "syntax error", "no more arguments expected")


def help_path(s) -> Path:
    return s.install.sys_dir / "help.t32"


def available(s, name: str, kind: str = "command") -> bool | None:
    """TRACE32's own answer for this session. None when it cannot be asked."""
    function = "PRACTICE.CoMmanD.AVAILable" if kind == "command" else "PRACTICE.FUNCtion.AVAILable"
    try:
        return str(s.conn.fnc(f"{function}({name})")).strip().lower() == "true"
    except (T32Error, LinkError):
        return None


def _usable(s, entries: list[Entry], limit: int, skip: set[str] = frozenset()) -> list[Entry]:
    kept = []
    for e in entries:
        if e.bare.lower() not in skip and available(s, e.bare, e.kind):
            kept.append(e)
            if len(kept) == limit:
                break
    return kept


def _fit(lines: list[str]) -> list[str]:
    """Keep to the budget. An option list that stops early must say so, or it reads as complete."""
    kept, used = [], 0
    for line in lines:
        used += len(line) + 1
        if used > MAX_ADVICE - len(CUT):
            if kept[-1] != CUT:
                kept.append(CUT)
            break
        kept.append(line)
    return kept


def _unknown_name(s, library: Help, name: str, kind: str) -> list[str]:
    close = _usable(s, library.close_to(name, kind), 3)
    described = _usable(s, library.described_as(name, kind), 3, {e.bare.lower() for e in close})
    lines = []
    if close:
        lines.append("Close names: " + ", ".join(e.shown for e in close))
    if described:
        lines.append("Described similarly: " + ", ".join(e.shown for e in described))
    return lines


def command_name(line: str) -> str:
    words = line.split()
    return words[0] if words else ""


def manuals_next(s, name: str | None = None) -> str:
    """Where to look when there is nothing better to say."""
    if load_help(help_path(s)):
        return f'Next: manual("{name}") has its syntax.' if name else "Next: ask manual, with a few words for what you want."
    return f"Next: the manuals are in {s.install.sys_dir / 'pdf'}."


def advice_only(explain):
    """Advice is extra. Nothing that goes wrong in it may touch the reply it is added to."""

    @wraps(explain)
    def guarded(*args) -> list[str]:
        try:
            return explain(*args)
        except Exception:
            return []

    return guarded


@advice_only
def explain_command(s, line: str, error: str | None) -> list[str]:
    """Advice for a failed command line. Empty when there is nothing certain to say."""
    if not error or not any(text in error for text in COMMAND_ERRORS):
        return []
    name = command_name(line)
    if not NAME.match(name):
        return []
    exists = available(s, name)
    if exists is None:
        return [manuals_next(s, name)]
    library = load_help(help_path(s))
    picked = library.pick(name) if library else None
    entry = picked.command if picked else None
    if exists:
        lead = f"{name} exists here, so an option or an argument is wrong."
        if entry and picked.differs_by_target:
            return [lead, f'Its syntax differs by target: manual("{name} <target>") shows one page.']
        form = library.format_of(entry) if entry else []
        lines = _fit([lead, *form])
        return lines + [manuals_next(s, name)] if not form or lines[-1] == CUT else lines
    if entry:
        return _fit([f"{name} is in the manual ({entry.summary[:48]}) but not available in this session."])
    names = _unknown_name(s, library, name, "command") if library else []
    return _fit([f"{name} does not exist.", *(names or [manuals_next(s)])])


@advice_only
def explain_unknown_window(s, window: str) -> list[str]:
    """A name view refused. If it exists it is simply not a window, and the refusal already says so."""
    name = command_name(window)
    if not NAME.match(name) or available(s, name) is not False:
        return []
    library = load_help(help_path(s))
    return _fit(_unknown_name(s, library, name, "command")) if library else []


@advice_only
def explain_function(s, error: str) -> list[str]:
    """Advice for an expression that failed."""
    if "expected ADDRESS" in " ".join(error.split()):
        return ["Next: an address needs an access class, such as D:0x20000000 or P:Register(LR)."]
    match = NO_FUNCTION.search(error)
    if not match:
        return []
    library = load_help(help_path(s))
    names = _unknown_name(s, library, match.group(1), "function") if library else []
    return _fit(names or [manuals_next(s)])
