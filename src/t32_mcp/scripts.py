"""Running a PRACTICE script and deciding how it ended."""

from __future__ import annotations

import time
from pathlib import Path

from .jobs import RAW, Job
from .language import explain_command
from .rapi import T32Error
from .replies import Reply, failed, ok, tidy
from .session import Session

AREA = "T32MCPS"
OUTPUT_LINES = 40
DIALOG_CLOSE_S = 0.2

RUNNING, DIALOG, ON_ERROR, PAUSED, DONE_WITH_ERRORS, DONE = (
    "running", "dialog", "on_error", "paused", "done_with_errors", "done")


def classify(practice_state: int, depth: int, base_depth: int, occurred: bool) -> str:
    if practice_state == 1:
        return RUNNING
    if depth <= base_depth:  # state 2 outlives a closed dialog, so it only counts inside a script
        return DONE_WITH_ERRORS if occurred else DONE
    if practice_state == 2:
        return DIALOG
    return ON_ERROR if occurred else PAUSED


class ScriptJob(Job):
    tier = RAW
    hard = False
    default_deadline = 600.0

    def __init__(self, path: str | None, text: str | None, args: str | None):
        self.path, self.text, self.args = path, text, args
        self.previous_area: str | None = None
        self.label = Path(path).name if path else "inline script"

    def start(self, s: Session) -> Reply | None:
        if bool(self.path) == bool(self.text):
            return failed("pass either path or text", label=self.label)
        if self.path:
            script = Path(self.path)
            if not script.is_file():
                return failed(f"no script at {script}", label=self.label)
        else:
            script = s.directory / f"script-{s.op_id}.cmm"
            script.write_text(self.text, encoding="utf-8")
        self.base = int(s.conn.fnc("PRACTICE.SD()"))
        self.error_before = s.fact("ERROR.CMDLINE()")
        if s.owned:
            self.previous_area = str(s.conn.fnc("AREA.SELECTed()"))
            s.conn.cmd(f"AREA.Create {AREA} 200. 400.")
            s.conn.cmd(f"AREA.Select {AREA}")
            s.conn.cmd("ERROR.RESet")
        command = f'DO "{script}"' + (f" {self.args}" if self.args else "")
        try:
            s.conn.cmd(command)
        except T32Error as exc:
            self._release_area(s)
            return failed(exc.text, f"  {command}", label=self.label)
        s.script_op = s.op_id
        return None

    def _occurred(self, s: Session) -> bool:
        if s.owned:
            return bool(s.conn.fnc("ERROR.OCCURRED()"))
        return s.fact("ERROR.CMDLINE()") != self.error_before

    def poll(self, s: Session) -> Reply | None:
        state = classify(s.conn.practice_state(), int(s.conn.fnc("PRACTICE.SD()")), self.base, self._occurred(s))
        if state == RUNNING:
            return None
        return self._finish(s, state)

    def _error_lines(self, s: Session) -> list[str]:
        lines = []
        command = s.fact("ERROR.CMDLINE()")
        if command:
            self.failed_command = command.removeprefix("B::")
            lines.append(f"  command: {self.failed_command}")
        message = s.fact("ERROR.MESSAGE()")
        if message:
            self.failed_error = message
            lines.append(f"  error: {message}")
        return lines

    def _clear_stack(self, s: Session) -> None:
        """A stopped script would otherwise sit under the next one."""
        if int(s.conn.fnc("PRACTICE.SD()")) > self.base:
            s.conn.cmd("END")

    def _finish(self, s: Session, state: str) -> Reply:
        errors = self._error_lines(s) if state in (ON_ERROR, DONE_WITH_ERRORS) else []
        if state in (ON_ERROR, PAUSED, DIALOG):
            self._clear_stack(s)
        dialog_left = state == DIALOG and not self._close_dialog(s)
        output = self._collect(s)
        s.script_op = None
        cleared = "The script stack was cleared. Commands before this point already took effect."
        if state == ON_ERROR:
            reply = failed("script stopped on an error", *errors, *output, cleared, label=self.label)
            reply.lines.extend(explain_command(s, self.failed_command, self.failed_error))
        elif state == DIALOG:
            reply = failed("script opened a dialog and was stopped, because nobody can answer it", *output,
                           cleared, next_step="run a copy of the script without the dialog.", label=self.label)
            if dialog_left:
                reply.tail.append("Warning: the dialog is still open in PowerView; the user has to close it.")
        elif state == PAUSED:
            reply = failed("script stopped without an error", *output, cleared, label=self.label)
        elif state == DONE_WITH_ERRORS:
            reply = ok("OK: script finished, but it handled errors along the way", *errors, *output, label=self.label)
        else:
            reply = ok("OK: script finished", *output, label=self.label)
        return reply

    def _close_dialog(self, s: Session) -> bool:
        """True when nothing is left on screen. The user's own PowerView is left alone."""
        try:
            s.conn.cmd("DIALOG.END")  # dialogs a script built itself; a message box refuses this
        except T32Error:
            pass
        if s.owned:
            s.instance.close_dialogs()
            time.sleep(DIALOG_CLOSE_S)
            return s.instance.close_dialogs() == 0
        return s.conn.practice_state() != 2

    def _stop(self, s: Session) -> list[str]:
        s.conn.stop_script()
        self._clear_stack(s)
        output = self._collect(s)
        s.script_op = None
        return output

    def on_deadline(self, s: Session, limit: float) -> Reply:
        output = self._stop(s)
        return failed(f"script passed its {limit:.0f} s limit and was stopped", *output,
                      "Commands before this point already took effect.",
                      next_step="raise deadline_s if it needs longer.", label=self.label)

    def cancel(self, s: Session) -> Reply:
        output = self._stop(s)
        return ok("Cancelled: script stopped. Commands before this point already took effect.", *output,
                  label=self.label)

    def _collect(self, s: Session) -> list[str]:
        if not s.owned:
            return []
        path = s.directory / f"script-{s.op_id}.log"
        lines: list[str] = []
        try:
            s.conn.cmd(f'PRinTer.FILE "{path}"')
            s.conn.cmd("PRinTer.FileType ASCIIE")
            s.conn.cmd(f"WinPrint.AREA.view {AREA}")
            text = tidy(path.read_bytes().decode("latin-1")) if path.exists() else ""
            lines = [line for line in text.split("\n")[1:] if line.strip()]
        except T32Error:
            pass
        self._release_area(s)
        if not lines:
            return []
        shown = lines[-OUTPUT_LINES:]
        heading = "Output:" if len(shown) == len(lines) else f"Output (last {len(shown)} of {len(lines)} lines, all in {path}):"
        return [heading, *(f"  {line}" for line in shown)]

    def _release_area(self, s: Session) -> None:
        if self.previous_area is None:
            return
        try:
            s.conn.cmd(f"AREA.Select {self.previous_area}")
            s.conn.cmd(f"AREA.Delete {AREA}")
        except T32Error:
            pass
        self.previous_area = None

    def pending(self, op: int) -> list[str]:
        return [f"Running: {self.label} has not finished. Only evaluate and session_status work until it does.",
                f"Next: operation_get({op}) to keep waiting, or operation_cancel({op}) to stop it."]
