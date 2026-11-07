"""Finding, starting, connecting and ending PowerView instances."""

from __future__ import annotations

import ctypes
import os
import re
import socket
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil

from .rapi import Connection, LinkError, T32Error

DEFAULT_SYS = r"C:\T32"
START_TIMEOUT_S = 40.0
ESTABLISH_TIMEOUT_S = 30.0
RESET_TIMEOUT_S = 25.0
QUIT_TIMEOUT_S = 5.0

_EXE = re.compile(r"^t32m([a-z0-9]+)\.exe$", re.IGNORECASE)
_NOT_TARGETS = {"ciserver"}


@dataclass(frozen=True)
class Install:
    sys_dir: Path

    @classmethod
    def locate(cls, sys_dir: str | None = None) -> "Install":
        return cls(Path(sys_dir or os.environ.get("T32SYS") or DEFAULT_SYS))

    @property
    def bin_dir(self) -> Path:
        return self.sys_dir / "bin" / "windows64"

    def targets(self) -> list[str]:
        if not self.bin_dir.is_dir():
            return []
        names = (_EXE.match(p.name) for p in self.bin_dir.iterdir())
        return sorted(m.group(1).lower() for m in names if m and m.group(1).lower() not in _NOT_TARGETS)

    def executable(self, target: str) -> Path:
        return self.bin_dir / f"t32m{target}.exe"


@dataclass
class Device:
    index: int
    serial: str
    name: str
    port: str
    in_use: bool


@dataclass
class Found:
    pid: int
    exe: str
    ports: list[int]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def config_text(install: Install, directory: Path, pbi: str) -> str:
    return (
        f"OS=\nID=T32MCP{os.getpid()}\nSYS={install.sys_dir}\nTMP={directory}\n\n"
        f"{pbi}"
        "\nSCREEN=\nHEADER=t32-mcp\n"
    )


class Instance:
    """A PowerView this server started."""

    def __init__(self, process: subprocess.Popen, port: int):
        self.process = process
        self.port = port

    @classmethod
    def launch(cls, install: Install, target: str, directory: Path, pbi: str = "",
               interactive: bool = False) -> "Instance":
        config = directory / "config.t32"
        config.write_text(config_text(install, directory, pbi), encoding="ascii")
        port = free_port()
        args = [str(install.executable(target)), "--t32-safestart", f"--t32-api-rcl=TCP:{port}",
                "--t32-api-rcl-remotehost=DENY"]
        if interactive:
            args.append("--t32-startmode=INTERACTIVECONNECTION")
        # Standard streams must stay away from ours: they carry the MCP protocol.
        process = subprocess.Popen(args + ["-c", str(config)], cwd=directory, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return cls(process, port)

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    @property
    def exit_code(self) -> str | None:
        code = self.process.poll()
        return None if code is None else hex(code & 0xFFFFFFFF)

    def connect(self, timeout: float = START_TIMEOUT_S) -> Connection:
        end = time.monotonic() + timeout
        while True:
            if not self.alive:
                raise LinkError(f"PowerView exited during start with {self.exit_code}")
            try:
                return Connection(self.port)
            except LinkError:
                if time.monotonic() > end:
                    raise
                time.sleep(0.2)

    def close_dialogs(self) -> int:
        """Boxes from DIALOG.OK outlive the script, and DIALOG.END cannot reach them. Windows can."""
        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010
        closed = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(handle, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            name = ctypes.create_unicode_buffer(16)
            user32.GetClassNameW(handle, name, 16)
            if owner.value == self.pid and name.value == "#32770":
                user32.PostMessageW(handle, WM_CLOSE, 0, 0)
                closed.append(handle)
            return True

        user32.EnumWindows(visit, 0)
        return len(closed)

    def kill(self) -> None:
        if self.alive:
            self.process.kill()
        self.process.wait()

    def quit(self, conn: Connection) -> bool:
        """Ask nicely. True when the process left within the limit."""
        try:
            conn.cmd("QUIT")
        except (LinkError, T32Error):
            pass
        try:
            self.process.wait(QUIT_TIMEOUT_S)
            return True
        except subprocess.TimeoutExpired:
            return False


def scan(conn: Connection) -> list[Device]:
    """List debug modules. Needs Interactive Connection Mode. Touches nothing."""
    for command in ("CONNECTION.CONFIG.RESet", "CONNECTION.USB.scan ON", "CONNECTION.ETHernet.scan OFF",
                    "CONNECTION.TCPUSB.scan OFF", "CONNECTION.FindDEVices"):
        conn.cmd(command)
    found = []
    for i in range(int(conn.fnc("CONNECTION.DEVice.Number()"))):
        serial = str(conn.fnc(f"CONNECTION.DEVice.SerialNumber({i}.)"))
        found.append(Device(i, serial, str(conn.fnc(f"CONNECTION.DEVice.NAME({i}.)")),
                            str(conn.fnc(f"CONNECTION.DEVice.PORT({i}.)")),
                            conn.fnc(f"CONNECTION.DEVice.InUse({i}.)") not in (False, 0)))
    return found


def reset_device(conn: Connection, serial: str) -> tuple[str, float]:
    """One reset, never repeated: a timeout usually means it finished anyway."""
    index = conn.fnc(f'CONNECTION.DEVice.IndexBySerialNumber("{serial}")')
    start = time.monotonic()
    outcome = "returned"
    try:
        conn.cmd(f"CONNECTION.ResetDevice {index}.")
    except T32Error as exc:
        outcome = exc.text
    return outcome, time.monotonic() - start


def establish(conn: Connection, serial: str) -> None:
    index = conn.fnc(f'CONNECTION.DEVice.IndexBySerialNumber("{serial}")')
    conn.cmd(f"CONNECTION.Select.DEVice {index}.")
    conn.cmd("CONNECTION.ESTABLISH")
    end = time.monotonic() + ESTABLISH_TIMEOUT_S
    while conn.fnc("CONNECTION.STATE.Interactive()") not in (False, 0):
        try:
            conn.cmd("CONNECTION.ESTABLISH /FINALIZE")
            break
        except T32Error:
            if time.monotonic() > end:
                raise
            time.sleep(0.25)
    if conn.fnc("CONNECTION.STATE.ERROR()") not in (False, 0):
        raise T32Error(str(conn.fnc("CONNECTION.GetDriverError()")))


def running_instances() -> list[Found]:
    """Every PowerView on this machine with the TCP ports it listens on."""
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        match = _EXE.match(proc.info["name"] or "")
        if not match or match.group(1).lower() in _NOT_TARGETS:
            continue
        try:
            ports = sorted({c.laddr.port for c in proc.net_connections(kind="tcp")
                            if c.status == psutil.CONN_LISTEN})
        except psutil.Error:
            ports = []
        found.append(Found(proc.info["pid"], proc.info["name"], ports))
    return found


def api_port(found: Found) -> int | None:
    """Which of its ports is the Remote API? Listening ports can be TCF or GDB servers."""
    for port in found.ports:
        try:
            conn = Connection(port, timeout=3.0)
        except (LinkError, T32Error):
            continue
        try:
            if conn.fnc("API.RCL.TCP.PORT()") == port:
                return port
        except (LinkError, T32Error):
            pass
        finally:
            conn.close()
    return None
