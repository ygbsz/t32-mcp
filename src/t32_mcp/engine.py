"""Operations, waiting, deadlines, sessions."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import jobs, powerview, prompts
from .jobs import CONTROL, RAW, Job, StatusJob, attach_snapshot, connection_lines, directory_lines, state_lines
from .powerview import Found, Install, Instance, api_port, establish, reset_device, running_instances, scan
from .rapi import Connection, LinkError, T32Error
from .replies import Reply, failed, ok, render
from .session import Session

KEEP_OPERATIONS = 256
KEEP_SESSIONS = 20
SETUP_LOST = "The target state is unknown and the CPU configuration is gone."
SHORT_CALL_S = 30.0  # limit for calls that should return at once: Go, DO, polling
WATCH_INTERVAL_S = 0.25
EXIT_WAIT_S = 2.0


@dataclass
class ToolReply:
    text: str
    is_error: bool = False


@dataclass
class Operation:
    id: int
    job: Job
    session: Session | None
    deadline_s: float
    created: float = field(default_factory=time.monotonic)
    started: float | None = None
    state: str = "queued"  # queued | blocking | polling | done
    timed_out: bool = False
    kill_at: float = 0.0
    text: str = ""
    is_error: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)


class Journal:
    """Written before the work starts, so a crash loses nothing."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, **record) -> None:
        record["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# TRACE32 has no function that reads the print file type back, so view cannot restore it.
ATTACH_LEFT = "PowerView is still running. Its print file type may have changed."


def _default_state_root() -> Path:
    root = Path(__file__).resolve().parent
    if root.parent.name == "src" and (root.parent.parent / "pyproject.toml").is_file():
        root = root.parent.parent
    return root / "tmp" / "sessions"


class Engine:
    def __init__(self, install: Install, state_root: Path | None = None):
        self.install = install
        self.state_root = state_root or _default_state_root()
        self.session: Session | None = None
        self.journal: Journal | None = None
        self.generation = 0
        self.operations: OrderedDict[int, Operation] = OrderedDict()
        self.next_op = 1
        self.loop: asyncio.AbstractEventLoop | None = None
        self._open_lock = asyncio.Lock()
        self._lost_probes: set[str] = set()  # serials our own dead PowerViews still hold
        self._lost_simulator = False  # a dead *SIM instance holds a probe we never named

    # Operations

    def _new_op(self, job: Job, session: Session | None, deadline_s: float) -> Operation:
        op = Operation(self.next_op, job, session, deadline_s)
        self.next_op += 1
        self.operations[op.id] = op
        while len(self.operations) > KEEP_OPERATIONS:
            oldest = next(iter(self.operations))
            if self.operations[oldest].state != "done":
                break
            del self.operations[oldest]
        return op

    def _finish(self, op: Operation, reply: Reply, target: str | None = None, read_target: bool = True) -> None:
        if op.state == "done":
            return  # a late result of an abandoned call
        s = op.session
        if target is None:
            target = "session dead" if s is None or s.dead else None
        if target is None:
            target = f"target {s.target_state()}" if read_target else "target unknown"
        since = op.started or op.created
        op.text = render(op.id, s.generation if s else self.generation, target,
                         (time.monotonic() - since) * 1000, reply)
        op.is_error = reply.is_error
        op.state = "done"
        if self.journal:
            self.journal.write(op=op.id, phase="end", is_error=op.is_error, reply=op.text)
        try:
            self.loop.call_soon_threadsafe(op.event.set)
        except RuntimeError:
            pass  # the server is gone; the journal has the result

    async def _await(self, op: Operation, wait_s: float) -> ToolReply:
        try:
            await asyncio.wait_for(op.event.wait(), wait_s)
        except TimeoutError:
            return self._pending(op)
        return ToolReply(op.text, op.is_error)

    def _pending(self, op: Operation) -> ToolReply:
        s = op.session
        since = op.started or op.created
        reply = Reply(lines=op.job.pending(op.id), label=op.job.label)
        return ToolReply(render(op.id, s.generation if s else 0, op.job.pending_header,
                                (time.monotonic() - since) * 1000, reply))

    def _refuse(self, s: Session | None, job: Job) -> Reply | None:
        if s is None:
            return failed("no session is open.", next_step="call session_open.")
        if s.dead:
            return failed(s.dead, next_step="call session_open, then rerun your setup.")
        if job.tier in (CONTROL, RAW) and s.access == "observe":
            return failed("this session is read-only.",
                          next_step='ask the user first, then call session_close and '
                                    'session_open(connection="attach", access="control").')
        if s.script_op is not None and not job.during_script:
            return failed(f"script operation {s.script_op} is running.",
                          next_step=f"operation_get({s.script_op}) to wait, or operation_cancel({s.script_op}) to stop it.")
        return None

    async def execute(self, job: Job, wait_s: float | None = None, deadline_s: float | None = None) -> ToolReply:
        self.loop = asyncio.get_running_loop()
        s = self.session
        op = self._new_op(job, s, deadline_s or job.default_deadline)
        if self.journal:
            self.journal.write(op=op.id, phase="begin", tool=type(job).__name__, detail=vars(job))
        refusal = self._refuse(s, job)
        if refusal:
            self._finish(op, refusal, target="session dead" if s and s.dead else "no target", read_target=False)
            return ToolReply(op.text, True)
        s.submit(lambda: self._start(op))
        return await self._await(op, job.default_wait if wait_s is None else wait_s)

    def _start(self, op: Operation) -> None:
        s, job = op.session, op.job
        if op.state != "queued":
            return
        if s.dead:
            self._finish(op, failed(f"not run: {s.dead}", next_step="call session_open, then rerun your setup."))
            return
        op.started, s.op_id = time.monotonic(), op.id
        self._guard(op, op.deadline_s if job.hard else min(op.deadline_s, SHORT_CALL_S))
        try:
            reply = job.start(s)
        except LinkError as exc:
            self._died(op, exc)
            return
        except T32Error as exc:
            reply = failed(exc.text, label=job.label)
        except Exception as exc:
            reply = self._our_bug(op, exc)
        finally:
            s.blocked = None
        self._continue(op, reply)

    def _our_bug(self, op: Operation, exc: Exception) -> Reply:
        """An exception here would kill the worker, and every later call would stay Running with no reason given."""
        if self.journal:
            self.journal.write(op=op.id, phase="internal_error", error="".join(traceback.format_exception(exc)))
        s = op.session
        s.script_op = None
        return failed(f"internal error in t32-mcp during {op.job.doing}: {exc!r}",
                      "The operation may have partly run; its state in TRACE32 is unknown.",
                      next_step="check with session_status before going on. The session is still open.",
                      label=op.job.label)

    def _guard(self, op: Operation, limit: float) -> None:
        """Every call into PowerView runs under the watchdog, however short it should be."""
        op.state, op.kill_at = "blocking", time.monotonic() + limit
        op.session.blocked = op

    def _watch(self, s: Session) -> None:
        """Wall clock, on purpose: PowerView's keepalives restart socket timeouts forever."""
        while not s.dead and not s.closed:
            time.sleep(WATCH_INTERVAL_S)
            op = s.blocked
            if op is not None and op.state == "blocking" and time.monotonic() > op.kill_at:
                self._hard_deadline(op)

    def _continue(self, op: Operation, reply: Reply | None) -> None:
        if op.state == "done":
            return
        if reply is not None:
            self._finish(op, reply)
            return
        op.state = "polling"
        self.loop.call_soon_threadsafe(self.loop.call_later, op.job.poll_interval,
                                       op.session.submit, lambda: self._poll(op))

    def _poll(self, op: Operation) -> None:
        if op.state != "polling":
            return
        s, job = op.session, op.job
        if s.dead:
            self._finish(op, failed(s.dead, next_step="call session_open, then rerun your setup."))
            return
        s.op_id = op.id
        remaining = op.deadline_s - (time.monotonic() - op.started)
        self._guard(op, max(remaining, 0.0) + SHORT_CALL_S)
        try:
            reply = job.on_deadline(s, op.deadline_s) if remaining <= 0 else job.poll(s)
        except LinkError as exc:
            self._died(op, exc)
            return
        except T32Error as exc:
            reply = failed(exc.text, label=job.label)
        except Exception as exc:
            reply = self._our_bug(op, exc)
        finally:
            s.blocked = None
        self._continue(op, reply)

    def _hard_deadline(self, op: Operation) -> None:
        s = op.session
        op.timed_out = True
        if s.owned:
            s.instance.kill()  # the worker wakes up with a LinkError in _died
            return
        s.dead = f"{op.job.doing} passed its {op.deadline_s:.0f} s limit in the user's PowerView."
        self._finish(op, failed(f"{s.dead} The result is unknown and it may still be running.",
                                next_step="tell the user to check PowerView. This session is no longer used."),
                     target="session dead")

    def _died(self, op: Operation, exc: Exception) -> None:
        s, job = op.session, op.job
        if s.owned:
            try:
                s.instance.process.wait(EXIT_WAIT_S)  # the socket drops a moment before the process is gone
            except subprocess.TimeoutExpired:
                pass
            if s.serial:
                self._lost_probes.add(s.serial)
            else:
                self._lost_simulator = True
        name = job.doing
        if op.timed_out:
            s.dead = f"{name} passed its {op.deadline_s:.0f} s limit, so PowerView was killed."
        elif s.owned and not s.instance.alive:
            s.dead = f"PowerView exited with {s.instance.exit_code} during {name}."
        else:
            s.dead = f"the connection to PowerView was lost during {name}: {exc}"
        details = [f"The probe is held. {SETUP_LOST}"] if s.owned else []
        self._finish(op, failed(s.dead, *details, next_step="call session_open, then rerun your setup."),
                     target="session dead")
        if self.journal:
            self.journal.write(op=op.id, phase="session_dead", reason=s.dead)
        s.stop_worker()

    # Operations API

    async def get(self, operation: int, wait_s: float = 30.0) -> ToolReply:
        self.loop = asyncio.get_running_loop()
        op = self.operations.get(operation)
        if op is None:
            return ToolReply(render(0, self.generation, "no target", None,
                                    failed(f"there is no operation {operation}.")), True)
        return await self._await(op, wait_s)

    async def cancel(self, operation: int, kill: bool = False) -> ToolReply:
        self.loop = asyncio.get_running_loop()
        op = self.operations.get(operation)
        if op is None:
            return ToolReply(render(0, self.generation, "no target", None,
                                    failed(f"there is no operation {operation}.")), True)
        if op.state == "done":
            return ToolReply(op.text, op.is_error)
        s = op.session
        if op.state == "queued":
            self._finish(op, ok("Cancelled before it reached PowerView."), read_target=False)
            return ToolReply(op.text)
        if op.state == "blocking":
            if not kill:
                reply = failed(f"operation {op.id} is blocked inside PowerView and cannot be interrupted.",
                               next_step=f"operation_cancel({op.id}, kill=true) kills PowerView and loses target state.")
                return ToolReply(render(op.id, s.generation, "working", None, reply), True)
            if not s.owned:
                reply = failed("this PowerView belongs to the user, so it is never killed.",
                               next_step="tell the user it is blocked.")
                return ToolReply(render(op.id, s.generation, "working", None, reply), True)
            op.timed_out = False
            s.instance.kill()
            await op.event.wait()
            return ToolReply(op.text, op.is_error)

        def cancel_now() -> None:
            if op.state != "polling":
                return
            s.op_id = op.id
            self._guard(op, SHORT_CALL_S)
            try:
                reply = op.job.cancel(s)
            except LinkError as exc:
                self._died(op, exc)
                return
            except T32Error as exc:
                reply = failed(exc.text)
            finally:
                s.blocked = None
            self._finish(op, reply)

        s.submit(cancel_now)
        await op.event.wait()
        return ToolReply(op.text, op.is_error)

    # Sessions

    async def status(self) -> ToolReply:
        s = self.session
        if s is None:
            op_id = self.next_op
            self.next_op += 1
            running = await asyncio.to_thread(running_instances)
            listed = [f"PowerView running: pid {f.pid} {f.exe} ports {f.ports}" for f in running]
            reply = ok("No session is open.", f"Installed targets: {', '.join(self.install.targets()) or 'none'}",
                       f"System directory: {self.install.sys_dir}", *listed)
            return ToolReply(render(op_id, self.generation, "no target", None, reply))
        blocked = s.blocked
        if blocked is None:
            return await self.execute(StatusJob())
        seconds = time.monotonic() - blocked.started
        reply = ok(f"Busy: operation {blocked.id} ({blocked.job.doing}) "
                   f"has blocked PowerView for {seconds:.0f} s.",
                   f"Connection: {s.kind}, PowerView pid {s.pid}, API port {s.conn.port}",
                   f"It is killed at {blocked.deadline_s:.0f} s." if s.owned else "This PowerView is never killed.",
                   *directory_lines(s))
        return ToolReply(render(blocked.id, s.generation, "working", None, reply))

    def _new_directory(self) -> Path:
        self.state_root.mkdir(parents=True, exist_ok=True)
        directory = self.state_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-g{self.generation}"
        directory.mkdir()
        old = sorted((p for p in self.state_root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for stale in old[:-KEEP_SESSIONS]:
            for item in stale.iterdir():
                item.unlink(missing_ok=True)
            stale.rmdir()
        return directory

    async def open(self, target: str, connection: str, serial: str | None, port: int | None,
                   access: str | None, takeover: bool) -> ToolReply:
        self.loop = asyncio.get_running_loop()
        async with self._open_lock:
            op_id = self.next_op
            self.next_op += 1
            live = self.session
            if live and not live.dead:
                reply = failed(f"a session is already open (generation {live.generation}).",
                               next_step="call session_close first.")
                return ToolReply(render(op_id, live.generation, "no change", None, reply), True)
            if live:
                live.conn.close()
            self.session = None
            started = time.monotonic()
            self.generation += 1
            try:
                reply, session = await asyncio.to_thread(self._open, target, connection, serial, port, access, takeover)
            except (LinkError, T32Error, OSError) as exc:
                reply, session = failed(f"session_open failed: {exc}"), None
            if session is None:
                self.generation -= 1
                return ToolReply(render(op_id, self.generation, "no session", (time.monotonic() - started) * 1000, reply),
                                 reply.is_error)
            self.session = session
            threading.Thread(target=self._watch, args=(session,), name="t32-watch", daemon=True).start()
            self.journal = Journal(session.directory / "journal.jsonl")
            self.journal.write(op=op_id, phase="session_open", connection=connection, target=target,
                               pid=session.pid, port=session.conn.port, reply=reply.lines)
            return ToolReply(render(op_id, session.generation, f"target {session.target_state()}",
                                    (time.monotonic() - started) * 1000, reply), reply.is_error)

    def _open(self, target, connection, serial, port, access, takeover) -> tuple[Reply, Session | None]:
        if connection == "attach":
            return self._open_attach(port, access)
        if access:
            return failed("access only applies to attach."), None
        if not self.install.executable(target).is_file():
            found = ", ".join(self.install.targets()) or "none"
            return failed(f"{target} is not installed", f"Installed targets: {found}"), None
        directory = self._new_directory()
        instance = Instance.launch(self.install, target, directory, interactive=True)
        try:
            conn = instance.connect()
            if connection == "usb":
                return self._open_usb(instance, conn, directory, serial, takeover)
            return self._open_sim(instance, conn, target, directory, serial, takeover)
        except BaseException:
            instance.kill()
            raise

    def _pick_probe(self, instance, conn, serial, takeover) -> tuple[Reply | None, powerview.Device | None, list[str]]:
        """Find the probe and release it if it is ours to release."""
        devices = scan(conn)
        if serial:
            devices = [d for d in devices if d.serial == serial]
        if not devices:
            return failed(f"no probe found over USB{f' with serial {serial}' if serial else ''}.",
                          next_step='check the cable and power, or use connection="sim".'), None, []
        if len(devices) > 1:
            listed = [f"  {d.serial} {d.port}{' (in use)' if d.in_use else ''}" for d in devices]
            return failed("several probes found.", *listed, next_step="pass serial."), None, []
        device = devices[0]
        notes: list[str] = []
        if device.in_use:
            ours = device.serial in self._lost_probes or self._lost_simulator
            if not ours and not takeover:
                return failed(f"probe {device.serial} is held by another PowerView.",
                              next_step="if that PowerView is gone, call session_open(takeover=true). "
                                        "Otherwise ask the user which instance to use, or attach to it."), None, []
            outcome, seconds = reset_device(conn, device.serial)
            notes.append(f"Probe {device.serial} was still held, so it was reset ({seconds:.0f} s, {outcome}).")
            device = next((d for d in scan(conn) if d.serial == device.serial), device)
            if device.in_use:
                return failed(f"probe {device.serial} is still held after one reset.", *notes,
                              next_step="tell the user to power-cycle the PowerDebug."), None, []
            self._lost_probes.discard(device.serial)
            self._lost_simulator = False
        return None, device, notes

    def _open_usb(self, instance, conn, directory, serial, takeover) -> tuple[Reply, Session | None]:
        refusal, device, notes = self._pick_probe(instance, conn, serial, takeover)
        if refusal:
            instance.quit(conn) or instance.kill()
            return refusal, None
        establish(conn, device.serial)
        session = Session(self.generation, "usb", conn, directory, self.install, instance, "control", device.serial)
        return self._opened(session, notes), session

    def _open_sim(self, instance, conn, target, directory, serial, takeover) -> tuple[Reply, Session | None]:
        devices = scan(conn)
        notes: list[str] = []
        pbi = "PBI=SIM\n"
        if devices:
            refusal, device, notes = self._pick_probe(instance, conn, serial, takeover)
            if refusal:
                instance.quit(conn) or instance.kill()
                return refusal, None
            pbi = "PBI=*SIM\nUSB\n"
        else:
            notes = ["Warning: no probe to license the simulator, so the session stops working "
                     "50 commands after the first Go or Step."]
        instance.quit(conn) or instance.kill()
        instance = Instance.launch(self.install, target, directory, pbi=pbi)
        try:
            conn = instance.connect()
        except BaseException:
            instance.kill()
            raise
        session = Session(self.generation, "sim", conn, directory, self.install, instance, "control")
        return self._opened(session, notes), session

    def _opened(self, session: Session, notes: list[str]) -> Reply:
        lines = [f"Opened generation {session.generation}.", *notes, *connection_lines(session),
                 *state_lines(session), *directory_lines(session)]
        if session.generation > 1 and notes:
            lines.append(SETUP_LOST)
        return ok(*lines)

    def _open_attach(self, port: int | None, access: str | None) -> tuple[Reply, Session | None]:
        pid = None
        if port is None:
            found = running_instances()
            if not found:
                return failed("no PowerView is running.",
                              next_step='ask the user to start it, or use connection="usb".'), None
            with_api = [(f, api_port(f)) for f in found]
            usable = [(f, p) for f, p in with_api if p]
            if len(usable) > 1:
                listed = [f"  pid {f.pid} {f.exe} port {p}" for f, p in usable]
                return failed("several PowerViews have a Remote API.", *listed,
                              next_step="ask the user which one, then pass port."), None
            if not usable:
                pids = ", ".join(str(f.pid) for f in found)
                return failed(f"PowerView is running (pid {pids}) but its Remote API is off.",
                              next_step="ask the user to type this into the PowerView command line, "
                                        f"then call session_open again:\n  {prompts.ENABLE_API}"), None
            (only, port), = usable
            pid = only.pid
        conn = Connection(port, timeout=5.0)
        directory = self._new_directory()
        session = Session(self.generation, "attach", conn, directory, self.install, None, access or "observe", pid=pid)
        lines = [f"Attached, generation {session.generation}.", *connection_lines(session),
                 *attach_snapshot(session), *directory_lines(session)]
        return ok(*lines), session

    async def close(self, leave_running: bool = False) -> ToolReply:
        self.loop = asyncio.get_running_loop()
        s = self.session
        op_id = self.next_op
        self.next_op += 1
        if s is None:
            return ToolReply(render(op_id, self.generation, "no target", None, failed("no session is open.")), True)
        if s.blocked is not None and not s.dead:
            reply = failed(f"operation {s.blocked.id} is blocked inside PowerView.",
                           next_step=f"operation_cancel({s.blocked.id}, kill=true), then session_close.")
            return ToolReply(render(op_id, s.generation, "working", None, reply), True)
        self.session = None
        s.closed = True
        reply = await asyncio.to_thread(self._close, s, leave_running)
        s.stop_worker()
        if self.journal:
            self.journal.write(op=op_id, phase="session_close", reply=reply.lines)
        return ToolReply(render(op_id, s.generation, "closed", None, reply), reply.is_error)

    def _close(self, s: Session, leave_running: bool) -> Reply:
        if s.dead:
            s.conn.close()
            return ok("Session closed. PowerView was already gone.")
        if not s.owned:
            s.conn.close()
            return ok(f"Disconnected. {ATTACH_LEFT}")
        if leave_running:
            s.conn.close()
            return ok(f"Disconnected. PowerView (pid {s.pid}) stays open on API port {s.conn.port}.")
        if s.instance.quit(s.conn):
            return ok("PowerView closed.")
        s.instance.kill()
        if s.serial:
            self._lost_probes.add(s.serial)
        else:
            self._lost_simulator = True
        return ok("PowerView ignored QUIT and was killed. The probe may stay held until the next session_open.")

    async def shutdown(self) -> None:
        """The client went away. Never leaves a PowerView of ours running."""
        s, self.session = self.session, None
        if s is None or s.dead:
            return
        s.closed = True
        if s.owned:
            if s.blocked is not None:
                s.instance.kill()
            else:
                await asyncio.to_thread(lambda: s.instance.quit(s.conn) or s.instance.kill())
        else:
            s.conn.close()
        s.stop_worker()
