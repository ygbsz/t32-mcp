"""One connection to one PowerView, owned by one worker thread."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .powerview import Install, Instance
from .rapi import Connection, LinkError, Message, T32Error


@dataclass
class LineResult:
    text: str
    ok: bool
    error: str | None = None
    warning: str | None = None
    output: str | None = None


class Session:
    def __init__(self, generation: int, kind: str, conn: Connection, directory: Path, install: Install,
                 instance: Instance | None, access: str, serial: str | None = None,
                 pid: int | None = None):
        self.generation = generation
        self.kind = kind
        self.conn = conn
        self.directory = directory
        self.install = install
        self.instance = instance
        self.access = access  # observe | control
        self.serial = serial
        self.pid = pid if pid is not None else (instance.pid if instance else None)
        self.dead: str | None = None
        self.closed = False
        self.op_id = 0
        self.script_op: int | None = None
        self.blocked = None  # the operation inside a blocking call, if any
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        # Daemon: a call stuck in an attached PowerView must not keep the server alive.
        self._thread = threading.Thread(target=self._worker, name=f"t32-g{generation}", daemon=True)
        self._thread.start()

    @property
    def owned(self) -> bool:
        return self.instance is not None

    def _worker(self) -> None:
        while (task := self._queue.get()) is not None:
            task()

    def submit(self, task: Callable[[], None]) -> None:
        self._queue.put(task)

    def stop_worker(self) -> None:
        self._queue.put(None)

    # Error attribution. In an owned session ERROR.RESet makes the
    # error state and the message type mean "this command". An attached session keeps the
    # user's error state, so the message is compared with what was there before.

    def begin(self) -> Message | None:
        if self.owned:
            self.conn.cmd("ERROR.RESet")
            return None
        return self.conn.message()

    def new_message(self, before: Message | None) -> Message | None:
        message = self.conn.message()
        if message.type == 0 or message == before:
            return None
        return message

    def run_line(self, line: str) -> LineResult:
        before = self.begin()
        try:
            self.conn.cmd(line)
        except T32Error as exc:
            return LineResult(line, False, error=exc.text)
        # Read at once: any query in between can write the error state itself.
        occurred = self.conn.fnc("ERROR.OCCURRED()") if self.owned else False
        message = self.new_message(before)
        if occurred or (message and message.is_error):
            text = message.text if message and message.is_error else str(self.conn.fnc("ERROR.MESSAGE()"))
            return LineResult(line, False, error=text or "command failed")
        if message and message.is_warning:
            return LineResult(line, True, warning=message.text)
        if message and message.text:
            return LineResult(line, True, output=message.text)
        return LineResult(line, True)

    def target_state(self) -> str:
        try:
            return self.conn.target_state()
        except (LinkError, T32Error):
            return "unknown"

    def fact(self, expression: str) -> str | None:
        """A value for a report. One that cannot be read is left out, not guessed."""
        try:
            value = self.conn.fnc(expression)
        except T32Error:
            return None
        return None if value is None or value == "" else str(value)
