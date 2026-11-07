"""Scripted TRACE32 replies for tests that used to require lab firmware or a capture."""
import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from t32_mcp.engine import Engine
from t32_mcp.powerview import Install, Instance
from t32_mcp.rapi import Message, T32Error
from t32_mcp.session import Session


class LabConnection:
    """A RAPI test double. Each scenario supplies its own values, commands and windows."""
    port = 20000

    def __init__(self):
        self.state = "stopped"
        self.values = {"ERROR.OCCURRED()": False}
        self.windows = {}
        self.command_effects = {}
        self.command_errors = {}
        self.commands = []
        self.printer_path = None
        self.printer_format = None

    def fnc(self, expression):
        if expression not in self.values:
            raise T32Error(f"No scripted result for {expression}")
        result = self.values[expression]
        if isinstance(result, Exception):
            raise result
        return result() if callable(result) else result

    def cmd(self, command):
        self.commands.append(command)
        if command in self.command_errors:
            raise T32Error(self.command_errors[command])
        if command in self.command_effects:
            self.command_effects[command]()
        elif command in {"ERROR.RESet", "WinPOS ,,,,,,T32MCPV", "WinCLEAR T32MCPV"}:
            return
        elif command.startswith('PRinTer.FILE "'):
            self.printer_path = Path(command.removeprefix('PRinTer.FILE "').removesuffix('"'))
        elif command.startswith("PRinTer.FileType "):
            self.printer_format = command.removeprefix("PRinTer.FileType ")
        elif command.startswith("WinPrint."):
            window = command.removeprefix("WinPrint.")
            text = self.windows[(window, self.printer_format)]
            self.printer_path.write_bytes(text.encode("latin-1"))
        elif not any(command == window for window, _ in self.windows):
            raise AssertionError(f"Unexpected TRACE32 command: {command}")

    def target_state(self):
        return self.state

    def set_stop(self, symbol, pc="ST:0x24020000"):
        self.state = "stopped"
        self.values.update({"PP()": pc, "sYmbol.FUNCTION(PP())": symbol})

    def message(self):
        return Message(0, "")

    def close(self):
        pass


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def lab_backend(tmp_path):
    install = Install(tmp_path)
    engine = Engine(install, state_root=tmp_path)
    conn = LabConnection()
    instance = Mock(spec=Instance, pid=42, alive=True)
    instance.quit.return_value = True
    engine.generation = 1
    engine.session = Session(1, "usb", conn, tmp_path, install, instance, "control", serial="mock-probe")
    try:
        yield engine, conn
    finally:
        asyncio.run(engine.shutdown())
