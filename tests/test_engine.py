"""L0: engine state machine against a fake PowerView, including the failures that are slow to stage."""

import asyncio
import threading
import time

import pytest

from t32_mcp import engine as engine_module
from t32_mcp.engine import Engine
from t32_mcp.jobs import CommandJob, EvaluateJob, Job, RunJob
from t32_mcp.powerview import Install
from t32_mcp.rapi import LinkError, Message, T32Error
from t32_mcp.replies import ok
from t32_mcp.session import Session


class FakeInstance:
    def __init__(self):
        self.killed = threading.Event()

    pid = 4242

    @property
    def alive(self):
        return not self.killed.is_set()

    @property
    def exit_code(self):
        return "0xc0000409"

    @property
    def process(self):
        return self

    def poll(self):
        return None if self.alive else 1

    def wait(self, timeout=None):
        return self.poll()

    def kill(self):
        self.killed.set()


class FakeConn:
    port = 20000

    def __init__(self, instance=None):
        self.instance = instance
        self.block = threading.Event()
        self.blocked_on = None
        self.state = "stopped"
        self.commands = []
        self.late_failure = False

    def cmd(self, command):
        self.commands.append(command)
        if command == self.blocked_on:
            while not self.block.wait(0.01):
                if self.instance and self.instance.killed.is_set():
                    raise LinkError("connection reset")
            if self.late_failure:
                raise LinkError("connection reset")

    def fnc(self, expression):
        if expression == "ERROR.OCCURRED()":
            return False
        return 1

    def message(self):
        return Message(0, "")

    def target_state(self):
        return self.state

    def close(self):
        pass


def open_session(tmp_path, owned=True, access="control"):
    eng = Engine(Install.locate(), state_root=tmp_path)
    instance = FakeInstance() if owned else None
    conn = FakeConn(instance)
    eng.generation = 1
    s = Session(1, "sim", conn, tmp_path, eng.install, instance, access, serial="S1", pid=1)
    eng.session = s
    threading.Thread(target=eng._watch, args=(s,), daemon=True).start()
    return eng, s, conn


@pytest.fixture(autouse=True)
def fast_watchdog(monkeypatch):
    monkeypatch.setattr(engine_module, "WATCH_INTERVAL_S", 0.01)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("editable", [True, False], ids=["source", "wheel"])
def test_default_session_directory_belongs_to_the_mcp(tmp_path, monkeypatch, editable):
    checkout = tmp_path / "checkout"
    package = (checkout / "src" if editable else tmp_path / "site-packages") / "t32_mcp"
    package.mkdir(parents=True)
    if editable:
        (checkout / "pyproject.toml").touch()
    monkeypatch.setattr(engine_module, "__file__", str(package / "engine.py"))
    monkeypatch.chdir(tmp_path)
    directory = Engine(Install.locate())._new_directory()
    assert directory.parent == (checkout if editable else package) / "tmp" / "sessions"


def test_finishes_within_wait(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        reply = await eng.execute(CommandJob(["PRINT 1"]))
        assert "OK: 1 line" in reply.text and not reply.is_error
    run(go())


def test_wait_exhausted_replies_running_and_keeps_working(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "slow"
        first = await eng.execute(CommandJob(["slow"]), wait_s=0)
        assert "Running:" in first.text and "operation_get(1)" in first.text
        conn.block.set()
        final = await eng.get(1, wait_s=2)
        assert "OK: 1 line" in final.text
    run(go())


class Broken(Job):
    """A bug in the server, on either side of a poll."""

    label = "broken"
    poll_interval = 0.01

    def __init__(self, where):
        self.where = where

    def start(self, s):
        if self.where == "start":
            raise NameError("name 'oops' is not defined")
        return None

    def poll(self, s):
        raise NameError("name 'oops' is not defined")


@pytest.mark.parametrize("where", ["start", "poll"])
def test_a_bug_here_is_a_failure_and_the_session_lives_on(tmp_path, where):
    async def go():
        eng, s, conn = open_session(tmp_path)
        reply = await eng.execute(Broken(where), wait_s=2)
        assert reply.is_error and "internal error in t32-mcp" in reply.text and "NameError" in reply.text
        assert not s.dead
        after = await eng.execute(CommandJob(["PRINT 1"]), wait_s=2)
        assert not after.is_error and "OK: 1 line" in after.text
    run(go())


def test_a_bug_while_a_script_runs_does_not_leave_the_session_refusing_everything(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)

        class BrokenScript(Broken):
            def start(self, session):
                session.script_op = session.op_id

        await eng.execute(BrokenScript("poll"), wait_s=2)
        assert s.script_op is None
    run(go())


def test_a_deadline_names_the_line_that_hung(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "hang"
        reply = await eng.execute(CommandJob(["first", "hang", "never"]), wait_s=2, deadline_s=0.05)
        assert "line 2 of 3 (hang) passed its 0 s limit" in reply.text and "CommandJob" not in reply.text
    run(go())


def test_go_in_command_says_nothing_is_waiting(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.state = "running"
        reply = await eng.execute(CommandJob(["Break.Set main", "Go"]))
        assert 'run(action="go") waits' in reply.text
    run(go())


def test_go_that_stopped_at_once_needs_no_warning(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        reply = await eng.execute(CommandJob(["Go"]))
        assert "nothing is waiting" not in reply.text
    run(go())


@pytest.mark.parametrize("line, warned", [("Data.LOAD.Elf a.elf /NoCODE", True), ("data.load.elf a.elf /nocode", True),
                                          ("D.LOAD a.elf /NoCODE", True), ("Data.LOAD.Elf a.elf", False),
                                          ("Data.LOAD.Elf a.elf /NoCODEAT 0--1", False), ("PRINT /NoCODE", False)])
def test_loading_symbols_only_is_called_out(tmp_path, line, warned):
    async def go():
        eng, s, conn = open_session(tmp_path)
        reply = await eng.execute(CommandJob([line]))
        assert ("symbols only" in reply.text) == warned
    run(go())


def test_run_go_waits_for_a_target_already_running(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.state = "running"
        sent = len(conn.commands)
        first = await eng.execute(RunJob("go"), wait_s=0.05)
        assert "Running" in first.text and not first.is_error and "Go" not in conn.commands[sent:]
        conn.state = "stopped"
        assert "Stopped at" in (await eng.get(1, wait_s=2)).text
    run(go())


def test_a_failed_expression_gets_its_advice_once(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        real = conn.fnc

        def fnc(expression):
            if expression.startswith("Data.Long"):
                raise T32Error("wrong parameter type : expected  ADDRESS")
            return real(expression)

        conn.fnc = fnc
        reply = await eng.execute(EvaluateJob(["Data.Long(0x1)", "Data.Long(0x2)", "1"]))
        assert not reply.is_error and reply.text.count("access class") == 1
    run(go())


def test_a_failed_command_says_where_to_look_it_up(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        accepted = conn.cmd

        def cmd(command):
            if command == "Memory.Set 1":
                raise T32Error("unknown command")
            accepted(command)

        conn.cmd = cmd
        reply = await eng.execute(CommandJob(["Memory.Set 1"]))
        assert reply.is_error and "Next: " in reply.text and "manual" in reply.text
    run(go())


def test_a_run_that_has_not_stopped_says_the_target_is_running(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.state = "running"
        first = await eng.execute(RunJob("go"), wait_s=0.05)
        assert "target running" in first.text and "working" not in first.text
        conn.state = "stopped"
        await eng.get(1, wait_s=2)
    run(go())


def test_reading_a_finished_operation_repeats_the_reply_and_sends_nothing(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        first = await eng.execute(CommandJob(["PRINT 1"]))
        sent = len(conn.commands)
        assert (await eng.get(1)).text == first.text
        assert len(conn.commands) == sent
    run(go())


def test_deadline_kills_powerview_even_when_the_socket_never_times_out(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "hang"
        reply = await eng.execute(CommandJob(["hang"]), wait_s=2, deadline_s=0.05)
        assert reply.is_error and s.instance.killed.is_set()
        assert "passed its 0 s limit, so PowerView was killed" in reply.text
        assert "session dead" in reply.text and "session_open" in reply.text
    run(go())


def test_queued_work_never_reaches_a_dead_session(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "hang"
        await eng.execute(CommandJob(["hang"]), wait_s=0, deadline_s=0.05)
        queued = await eng.execute(CommandJob(["never"]), wait_s=2)
        assert queued.is_error and "not run" in queued.text or "killed" in queued.text
        assert "never" not in conn.commands
    run(go())


def test_after_death_every_call_points_at_session_open(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "hang"
        await eng.execute(CommandJob(["hang"]), wait_s=2, deadline_s=0.05)
        reply = await eng.execute(EvaluateJob(["1"]))
        assert reply.is_error and "session_open" in reply.text
    run(go())


def test_attached_powerview_is_never_killed(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path, owned=False)
        conn.blocked_on = "hang"
        reply = await eng.execute(CommandJob(["hang"]), wait_s=2, deadline_s=0.05)
        assert reply.is_error and "result is unknown" in reply.text and "tell the user" in reply.text
        conn.block.set()
    run(go())


def test_late_result_of_an_abandoned_call_changes_nothing(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path, owned=False)
        conn.blocked_on = "hang"
        reply = await eng.execute(CommandJob(["hang"]), wait_s=2, deadline_s=0.05)
        conn.block.set()
        await asyncio.sleep(0.05)
        assert (await eng.get(1)).text == reply.text
    run(go())


def test_late_connection_loss_of_an_abandoned_call_changes_nothing(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path, owned=False)
        conn.blocked_on = "hang"
        conn.late_failure = True
        reply = await eng.execute(CommandJob(["hang"]), wait_s=2, deadline_s=0.05)
        conn.block.set()
        await asyncio.sleep(0.05)
        assert (await eng.get(1)).text == reply.text
    run(go())


class HangThenPoll(Job):
    """A call that blocks past its deadline and only then asks to be polled."""

    label = "hang"

    def start(self, s):
        s.conn.cmd("hang")
        return None

    def poll(self, s):
        return ok("this must never replace the reply already given")

    on_deadline = lambda self, s, limit: self.poll(s)


def test_late_poll_request_of_an_abandoned_call_changes_nothing(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path, owned=False)
        conn.blocked_on = "hang"
        reply = await eng.execute(HangThenPoll(), wait_s=2, deadline_s=0.05)
        s.dead = None
        conn.block.set()
        await asyncio.sleep(0.2)
        assert (await eng.get(1)).text == reply.text
    run(go())


def test_observe_session_refuses_control_and_raw(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path, owned=False, access="observe")
        for job in (CommandJob(["Go"]), RunJob("go")):
            reply = await eng.execute(job)
            assert reply.is_error and "read-only" in reply.text
        assert conn.commands == []
    run(go())


def test_status_answers_while_blocked_without_touching_powerview(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "slow"
        await eng.execute(CommandJob(["slow"]), wait_s=0)
        await asyncio.sleep(0.05)
        reply = await asyncio.wait_for(eng.status(), 1)
        assert "Busy: operation 1" in reply.text
        conn.block.set()
    run(go())


def test_run_at_deadline_leaves_the_target_running(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        accepted = conn.cmd

        def cmd(command):
            accepted(command)
            if command == "Go":
                conn.state = "running"

        conn.cmd = cmd
        reply = await eng.execute(RunJob("go"), wait_s=2, deadline_s=0.1)
        assert "still running" in reply.text and not s.instance.killed.is_set()
        assert "Break" not in conn.commands
        assert conn.commands.count("Go") == 1
    run(go())


def test_go_that_stops_at_once_is_a_success_and_is_not_resent(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        reply = await eng.execute(RunJob("go"))
        assert "Stopped at" in reply.text and conn.commands.count("Go") == 1
    run(go())


def test_cancelling_a_blocked_command_asks_for_kill(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "slow"
        await eng.execute(CommandJob(["slow"]), wait_s=0)
        await asyncio.sleep(0.05)
        reply = await eng.cancel(1)
        assert reply.is_error and "kill=true" in reply.text and not s.instance.killed.is_set()
        conn.block.set()
    run(go())


def test_kill_ends_a_blocked_command(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "slow"
        await eng.execute(CommandJob(["slow"]), wait_s=0)
        await asyncio.sleep(0.05)
        reply = await asyncio.wait_for(eng.cancel(1, kill=True), 2)
        assert reply.is_error and s.instance.killed.is_set() and s.dead
    run(go())


def test_our_dead_probe_is_remembered(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        conn.blocked_on = "hang"
        await eng.execute(CommandJob(["hang"]), wait_s=2, deadline_s=0.05)
        assert "S1" in eng._lost_probes
    run(go())


def test_no_session(tmp_path):
    async def go():
        eng = Engine(Install.locate(), state_root=tmp_path)
        reply = await eng.execute(CommandJob(["x"]))
        assert reply.is_error and "session_open" in reply.text
    run(go())


def test_journal_records_before_the_work_starts(tmp_path):
    async def go():
        eng, s, conn = open_session(tmp_path)
        eng.journal = engine_module.Journal(tmp_path / "journal.jsonl")
        conn.blocked_on = "slow"
        await eng.execute(CommandJob(["slow"]), wait_s=0)
        assert '"phase": "begin"' in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
        conn.block.set()
    run(go())
