"""What each tool does on the worker thread, apart from scripts and views."""

from __future__ import annotations

import re

from .rapi import T32Error
from .language import explain_command, explain_function
from .replies import Reply, failed, format_value, ok, warning
from .session import Session

OBSERVE, CONTROL, RAW = "observe", "control", "raw"
GO_LINE = re.compile(r"^\s*(Go|g)(\.|\s|$)", re.I)
NO_CODE = re.compile(r"^\s*(Data|d)\.LOAD\S*\s.*/NoCODE\b", re.I)
_REFUSED = [(re.compile(r"^\s*DO(\s|$)", re.I), "DO returns before the script ends", "use the script tool"),
            (re.compile(r"^\s*QUIT(\s|$)", re.I), "QUIT would end PowerView", "use session_close")]


class Job:
    """start() and poll() run on the worker. Returning None means: poll me again."""

    tier = OBSERVE
    hard = True  # the deadline kills PowerView, because the call blocks the API
    default_deadline = 60.0
    default_wait = 5.0
    poll_interval = 0.05
    during_script = False
    pending_header = "working"  # what the reply header says while this has not finished
    label = ""

    @property
    def doing(self) -> str:
        """What this job is busy with, for a reply about it getting stuck."""
        return self.label or "the operation"

    def start(self, s: Session) -> Reply | None:
        raise NotImplementedError

    def poll(self, s: Session) -> Reply | None:
        raise NotImplementedError

    def on_deadline(self, s: Session, limit: float) -> Reply:
        raise NotImplementedError

    def cancel(self, s: Session) -> Reply:
        return ok("Cancelled: stopped waiting.")

    def pending(self, op: int) -> list[str]:
        return [f"Running: {self.label or 'the operation'} has not finished.",
                f"Next: operation_get({op}) to keep waiting."]


class CommandJob(Job):
    tier = RAW
    label = ""

    def __init__(self, lines: list[str], stop_on_error: bool = True):
        self.lines = lines
        self.stop_on_error = stop_on_error
        self.current = "the commands"
        if len(lines) == 1:
            self.label = lines[0][:60]

    @property
    def doing(self) -> str:
        return self.current

    def start(self, s: Session) -> Reply:
        if not self.lines:
            return failed("no commands given")
        for number, line in enumerate(self.lines, 1):
            for pattern, why, instead in _REFUSED:
                if pattern.match(line):
                    return failed(f"line {number}: {why}.", "Nothing ran.", next_step=f"{instead}.")
        total = len(self.lines)
        results = []
        for number, line in enumerate(self.lines, 1):
            self.current = f"line {number} of {total} ({line[:60]})"
            result = s.run_line(line)
            results.append(result)
            if not result.ok and self.stop_on_error:
                ran = f"Lines 1-{number - 1} ran." if number > 2 else ("Line 1 ran." if number == 2 else "")
                skipped = total - number
                rest = f"{skipped} later line{'s' if skipped != 1 else ''} skipped." if skipped else ""
                notes = _notes(results)
                return failed(f"line {number} of {total}: {result.error}", f"  {number}: {line}",
                              *[x for x in (ran, rest) if x], *notes, *explain_command(s, line, result.error))
        failures = [(i, r) for i, r in enumerate(results, 1) if not r.ok]
        if failures:
            reply = failed(f"{len(failures)} of {total} lines failed")
            reply.lines.extend(f"  {i}: {r.text}: {r.error}" for i, r in failures)
            reply.lines.extend(_notes(results))
            for i, r in failures:
                reply.lines.extend(explain_command(s, r.text, r.error))
            return reply
        return ok(f"OK: {total} line{'s' if total != 1 else ''}", *_notes(results), *_cautions(s, self.lines))


def _cautions(s: Session, lines: list[str]) -> list[str]:
    """Commands that return OK and may still not do what the caller meant. Nothing else can catch these."""
    notes = []
    if any(NO_CODE.search(line) for line in lines):
        notes.append("Note: /NoCODE loaded symbols only. Target memory and the program counter are unchanged.")
    if any(GO_LINE.match(line) for line in lines) and s.target_state() == "running":
        notes.append('Note: the target is running and nothing is waiting for it. run(action="go") waits until it stops.')
    return notes


def _notes(results) -> list[str]:
    lines = []
    for r in results:
        if r.warning:
            lines.append(f"{warning(r.warning)}  ({r.text})")
        elif r.output:
            lines.append(r.output)
    return lines


class EvaluateJob(Job):
    default_deadline = 10.0
    during_script = True

    def __init__(self, expressions: list[str]):
        self.expressions = expressions

    def start(self, s: Session) -> Reply:
        if not self.expressions:
            return failed("no expressions given")
        lines, errors, advice = [], 0, []
        for expression in self.expressions:
            try:
                lines.append(f"{expression} = {format_value(s.conn.fnc(expression))}")
            except T32Error as exc:
                errors += 1
                lines.append(f"{expression}: failed: {exc.text}")
                advice.extend(a for a in explain_function(s, exc.text) if a not in advice)
        reply = Reply(lines=lines, tail=advice, is_error=errors == len(self.expressions))
        if len(self.expressions) > 1 and s.target_state() == "running":
            reply.tail.append("Warning: target was running, so these values come from different moments.")
        return reply


class RunJob(Job):
    tier = CONTROL
    hard = False

    COMMANDS = {"go": "Go", "step": "Step", "step_over": "Step.Over", "go_up": "Go.Up",
                "go_return": "Go.Return", "break": "Break"}
    pending_header = "target running"

    def __init__(self, action: str, until: str | None = None):
        self.action = action
        self.until = until
        self.warning: str | None = None
        self.label = f"{action} {until}" if until else action

    def start(self, s: Session) -> Reply | None:
        if self.until and self.action != "go":
            return failed("until only works with action go")
        if self.action == "go" and not self.until and s.conn.target_state() == "running":
            return None  # already started, by command Go or the user: there is only the waiting left to do
        command = self.COMMANDS[self.action]
        result = s.run_line(f"{command} {self.until}" if self.until else command)
        if not result.ok:
            return failed(result.error or "command failed", f"  {result.text}", label=self.label)
        self.warning = result.warning
        return None  # a target that stops at once is fine: Go is never sent twice

    def poll(self, s: Session) -> Reply | None:
        state = s.conn.target_state()
        if state == "running":
            return None
        if state != "stopped":
            return ok(f"Target is {state}.", label=self.label)
        return self._stopped(s)

    def _stopped(self, s: Session) -> Reply:
        pc = s.fact("PP()") or "unknown address"
        function = s.fact("sYmbol.FUNCTION(PP())")
        where = f"{pc} {function.rsplit(chr(92), 1)[-1]}" if function else pc
        reply = ok(f"Stopped at {where}", label=self.label)
        code = s.fact("DISASSEMBLE.ADDRESS(PP())")
        if code:
            reply.lines.append(f"  {code}")
        if self.until:
            try:
                same = s.conn.fnc(f"ADDRESS.OFFSET(PP())==ADDRESS.OFFSET({self.until})")
            except T32Error:
                reply.tail.append(f"Warning: could not compare the stop address with {self.until}.")
            else:
                if not same:
                    reply.tail.append(f"Warning: stopped before reaching {self.until}.")
        if self.warning:
            reply.tail.append(warning(self.warning))
        return reply

    def on_deadline(self, s: Session, limit: float) -> Reply:
        return ok(f"Stopped waiting after {limit:.0f} s. The target is still running; nothing stopped it.",
                  'Next: run(action="break") to stop it, or look at it with evaluate.', label=self.label)

    def cancel(self, s: Session) -> Reply:
        return ok("Cancelled: stopped waiting. The target is still running.", label=self.label)

    def pending(self, op: int) -> list[str]:
        return ["Running: target has not stopped.",
                f'Next: operation_get({op}) to keep waiting, or run(action="break") to stop it.']


class StatusJob(Job):
    default_deadline = 10.0
    during_script = True

    def start(self, s: Session) -> Reply:
        return ok(*connection_lines(s), *state_lines(s), *directory_lines(s))


def connection_lines(s: Session) -> list[str]:
    kind = "started here" if s.owned else f"attached, {s.access} access"
    lines = [f"Connection: {s.kind}, {kind}, PowerView pid {s.pid}, API port {s.conn.port}"]
    build = s.fact("VERSION.BUILD()")
    lines.append(f"TRACE32: {s.fact('VERSION.SOFTWARE()')}" + (f" (build {build})" if build else ""))
    probe = [x for x in (s.fact("INTERFACE.NAME()"), s.fact("CABLE.NAME()")) if x]
    if probe:
        lines.append(f"Probe: {', '.join(probe)}" + (f", serial {s.serial}" if s.serial else ""))
    index = s.fact("LICENSE.getINDEX()")
    if index is not None:
        features = s.fact(f"LICENSE.FEATURES({index}.)")
        if features:
            lines.append(f"License: {s.fact(f'LICENSE.FAMILY({index}.)')}: {features}")
    return lines


def state_lines(s: Session) -> list[str]:
    lines = [f"CPU: {s.fact('SYStem.CPU()') or 'none selected'}", f"Target: {s.target_state()}",
             f"Script depth: {s.fact('PRACTICE.SD()')}"]
    clients = s.fact("API.RCL.TCP.NrUsedCons()")
    if clients and int(float(clients)) > 1:
        lines.append(f"Warning: {clients} API clients are connected; someone else may be driving this PowerView.")
    return lines


def directory_lines(s: Session) -> list[str]:
    sys_dir = s.install.sys_dir
    return [f"System directory: {sys_dir}", f"Demo scripts: {sys_dir / 'demo'}", f"Manuals: {sys_dir / 'pdf'}",
            f"Session files: {s.directory}"]


def attach_snapshot(s: Session) -> list[str]:
    """What the user left behind. Read before anything can disturb it."""
    lines = state_lines(s)
    if s.fact("CONNECTION.STATE.Interactive()") not in (None, "False"):
        lines.append("PowerView has not connected to a probe yet.")
    if s.fact("ERROR.OCCURRED()") == "True":
        lines.append(f"Last error: {s.fact('ERROR.MESSAGE()')}")
        command = s.fact("ERROR.CMDLINE()")
        if command:
            lines.append(f"Last failing command: {command}")
    return lines
