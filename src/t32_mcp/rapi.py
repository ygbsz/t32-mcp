"""The only module that imports pyrcl. Private pyrcl calls stay here, pinned by contract tests."""

from __future__ import annotations

from dataclasses import dataclass

import lauterbach.trace32.rcl as t32

# PowerView answers keepalives during a blocked call, and each one restarts this
# timeout, so it never limits a call: the engine's wall-clock deadline does that.
RECV_TIMEOUT_S = 30.0

# T32_GetState
STATES = {0: "down", 1: "bootstall", 2: "stopped", 3: "running"}

MSG_ERROR = 2 | 16
MSG_STATUS = 8


class T32Error(Exception):
    """TRACE32 refused or failed something. The session is still usable."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


class LinkError(Exception):
    """The connection to PowerView is gone."""


@dataclass(frozen=True)
class Message:
    type: int
    text: str

    @property
    def is_error(self) -> bool:
        return bool(self.type & MSG_ERROR)

    @property
    def is_warning(self) -> bool:
        return bool(self.type & MSG_STATUS)


def _t32_text(exc: Exception) -> str:
    if exc.args:
        first = exc.args[0]
        if isinstance(first, str) and first:
            return first
    return str(exc) or type(exc).__name__


class Connection:
    def __init__(self, port: int, timeout: float = RECV_TIMEOUT_S):
        try:
            self._dbg = t32.connect(node="localhost", port=port, protocol="TCP", timeout=timeout)
        except (OSError, t32.ApiConnectionError) as exc:
            raise LinkError(str(exc)) from exc
        except t32.BaseError as exc:
            raise T32Error(_t32_text(exc)) from exc
        self.port = port

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except (OSError, t32.ApiConnectionError) as exc:
            raise LinkError(str(exc)) from exc
        except t32.BaseError as exc:
            raise T32Error(_t32_text(exc)) from exc

    def cmd(self, command: str) -> None:
        self._call(self._dbg.cmd, command)

    def fnc(self, expression: str):
        return self._call(self._dbg.fnc, expression)

    def message(self) -> Message:
        m = self._call(self._dbg.get_message)
        return Message(m.type, m.text.strip("\x00 \r\n"))

    def target_state(self) -> str:
        raw = self._call(self._dbg.get_state)
        code = raw if isinstance(raw, int) else bytes(raw)[0]
        return STATES.get(code, "unknown")

    def practice_state(self) -> int:
        """0 idle, 1 running, 2 a dialog is open."""
        return self._call(self._dbg._get_practice_state)

    def stop_script(self) -> None:
        self._call(self._dbg._t32_stop)

    def close(self) -> None:
        try:
            self._dbg.disconnect()
        except Exception:
            pass
