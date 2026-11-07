"""L1: real PowerView on this machine. Every PowerView behaviour the server relies on.

Needs TRACE32 and a free licensed PowerDebug. The simulator is licensed by the probe. No target board.
"""

import asyncio
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from t32_mcp import powerview
from t32_mcp.engine import Engine
from t32_mcp.jobs import CommandJob, EvaluateJob, RunJob
from t32_mcp.powerview import Install, Instance, running_instances, scan
from t32_mcp.scripts import ScriptJob
from t32_mcp.views import ViewJob

pytestmark = pytest.mark.hw


def free_bench_serial(directory) -> str | None:
    """Select an unambiguous, idle USB probe for this test run."""
    install = Install.locate()
    if running_instances() or not install.executable("arm").is_file():
        return None
    instance = Instance.launch(install, "arm", directory, interactive=True)
    conn = None
    try:
        conn = instance.connect()
        devices = scan(conn)
        if len(devices) == 1 and not devices[0].in_use and devices[0].serial:
            return devices[0].serial
        return None
    finally:
        if conn is None:
            instance.kill()
        else:
            instance.quit(conn) or instance.kill()
            conn.close()


@pytest.fixture(scope="session", autouse=True)
def bench(tmp_path_factory):
    serial = free_bench_serial(tmp_path_factory.mktemp("probe"))
    if serial is None:
        pytest.skip("needs TRACE32, no running PowerView, and exactly one idle USB PowerDebug")
    return serial


@pytest.fixture
def engine(tmp_path):
    return Engine(Install.locate(), state_root=tmp_path)


def live(test):
    """Run an async test body, and never leave a PowerView behind."""
    def wrapper(engine, *args, **kwargs):
        async def body():
            try:
                await test(engine, *args, **kwargs)
            finally:
                await engine.shutdown()
        asyncio.run(body())
    wrapper.__name__ = test.__name__
    wrapper.__doc__ = test.__doc__
    wrapper.__signature__ = inspect.signature(test)  # pytest finds fixtures here
    return wrapper


async def sim(engine, serial=None):
    reply = await engine.open("arm", "sim", serial, None, None, False)
    assert not reply.is_error, reply.text
    return reply


async def configured_sim(engine):
    await sim(engine)
    reply = await engine.execute(CommandJob(["SYStem.CPU STM32H7S3L8", "SYStem.Up"]))
    assert not reply.is_error, reply.text


@live
async def test_session_report(engine):
    reply = await sim(engine)
    assert "Opened generation 1" in reply.text and "Demo scripts:" in reply.text
    status = await engine.status()
    assert "Connection: sim" in status.text and "Cortex-M" in status.text
    closed = await engine.close()
    assert "PowerView closed" in closed.text
    assert not running_instances()


@live
async def test_failed_command_is_reported(engine):
    await sim(engine)
    await engine.execute(CommandJob(["SYStem.CPU CortexM7", "SYStem.Up"]))
    reply = await engine.execute(CommandJob(["PRINT 1", "Break.Set nosuchsymbol_xyz", "PRINT 2"]))
    assert reply.is_error and "line 2 of 3: symbol not found" in reply.text
    assert "1 later line skipped" in reply.text


@live
async def test_old_error_does_not_stick_to_a_good_command(engine):
    await sim(engine)
    await engine.execute(CommandJob(["LAB_INVALID"]))
    reply = await engine.execute(CommandJob(['PRINT "hello"']))
    assert not reply.is_error and "OK: 1 line" in reply.text and "hello" in reply.text


@live
async def test_each_expression_stands_alone(engine):
    await sim(engine)
    reply = await engine.execute(EvaluateJob(["VERSION.BUILD()", "Var.VALUE(nosuch)", "1==1"]))
    assert not reply.is_error
    assert "VERSION.BUILD() = 0x" in reply.text and "Var.VALUE(nosuch): failed: symbol not found" in reply.text
    assert "1==1 = true" in reply.text


@live
async def test_script_that_finishes(engine):
    await sim(engine)
    reply = await engine.execute(ScriptJob(None, 'PRINT "from script"\nENDDO\n', None))
    assert "OK: script finished" in reply.text and "from script" in reply.text


@live
async def test_script_that_stops_on_an_error_names_the_command(engine):
    await sim(engine)
    reply = await engine.execute(ScriptJob(None, 'PRINT "begin"\nLAB_INVALID\nPRINT "after"\nENDDO\n', None))
    assert reply.is_error and "script stopped on an error" in reply.text
    assert "command: LAB_INVALID" in reply.text and "error: unknown command" in reply.text
    assert "begin" in reply.text and "after" not in reply.text
    depth = await engine.execute(EvaluateJob(["PRACTICE.SD()"]))
    assert "0x0 (0)" in depth.text


@live
async def test_script_that_handled_its_error_is_not_a_failure(engine):
    await sim(engine)
    reply = await engine.execute(ScriptJob(None, 'ON ERROR CONTinue\nLAB_INVALID\nPRINT "after"\nENDDO\n', None))
    assert not reply.is_error and "handled errors" in reply.text and "LAB_INVALID" in reply.text and "after" in reply.text


@live
async def test_long_script_stays_reachable_and_cancels(engine):
    await sim(engine)
    first = await engine.execute(ScriptJob(None, 'PRINT "wait"\nWAIT 30.s\nENDDO\n', None), wait_s=0.3)
    assert "Running:" in first.text
    assert not (await engine.execute(EvaluateJob(["VERSION.BUILD()"]))).is_error
    refused = await engine.execute(CommandJob(["PRINT 1"]))
    assert refused.is_error and "is running" in refused.text
    cancelled = await engine.cancel(int(first.text.split()[1]))
    assert "Cancelled: script stopped" in cancelled.text
    assert not (await engine.execute(CommandJob(["PRINT 1"]))).is_error
    assert "0x0 (0)" in (await engine.execute(EvaluateJob(["PRACTICE.SD()"]))).text


@live
async def test_script_deadline_stops_it_and_keeps_the_session(engine):
    await sim(engine)
    reply = await engine.execute(ScriptJob(None, "WAIT 30.s\nENDDO\n", None), deadline_s=0.5)
    assert reply.is_error and "limit and was stopped" in reply.text
    assert not (await engine.execute(CommandJob(["PRINT 1"]))).is_error


@live
async def test_script_with_a_dialog_does_not_hang(engine):
    await sim(engine)
    reply = await engine.execute(ScriptJob(None, 'DIALOG.OK "press me"\nPRINT "ran on"\nENDDO\n', None), wait_s=8)
    assert reply.is_error and "dialog" in reply.text, reply.text
    assert "0x0 (0)" in (await engine.execute(EvaluateJob(["PRACTICE.SD()"]))).text
    assert "still open" not in reply.text and engine.session.instance.close_dialogs() == 0
    assert "ran on" not in reply.text  # closing the box must not let the script continue
    assert not (await engine.execute(CommandJob(['PRINT "still works"']))).is_error
    next_script = await engine.execute(ScriptJob(None, 'PRINT "next"\nENDDO\n', None))
    assert "OK: script finished" in next_script.text and "next" in next_script.text


@live
async def test_missing_script(engine):
    await sim(engine)
    reply = await engine.execute(ScriptJob(r"C:\nonexistent_abc.cmm", None, None))
    assert reply.is_error and "no script at" in reply.text


@live
async def test_view_returns_window_content(engine):
    await sim(engine)
    await engine.execute(CommandJob(["SYStem.CPU CortexM7", "SYStem.Up"]))
    reply = await engine.execute(ViewJob("Register.view"))
    assert not reply.is_error and "R0" in reply.text and "Warning" not in reply.text


@live
async def test_view_keeps_the_warning_a_print_would_overwrite(engine):
    await configured_sim(engine)
    reply = await engine.execute(ViewJob("Data.dump nosuchsymbol_xyz"))
    assert "Warning: Warning: symbol not found" in reply.text or "symbol not found" in reply.text


@live
async def test_view_of_a_broken_window_fails_for_real(engine):
    await sim(engine)
    reply = await engine.execute(ViewJob("Frame.view /Task \"nosuchtask\""))
    assert reply.is_error and "no such task" in reply.text


@live
async def test_view_never_runs_anything_else(engine):
    await sim(engine)
    await engine.execute(CommandJob(["SYStem.CPU CortexM7", "SYStem.Up", "Data.Set 0x20000000 %Word 0xE7FE",
                                     "Register.Set PC 0x20000000", "Register.Set R13 0x20010000",
                                     "Data.Set 0x20001000 %Long 0xAAAAAAAA"]))
    marker = engine.session.directory / "marker.txt"
    for window in ("Go", "Data.Set 0x20001000 %Long 0x1", f'OS.Hidden cmd /c echo x > "{marker}"', "SYStem.Down"):
        reply = await engine.execute(ViewJob(window))
        assert reply.is_error, window
    state = await engine.execute(EvaluateJob(["Data.Long(D:0x20001000)", "STATE.RUN()"]))
    assert "0xaaaaaaaa" in state.text and "STATE.RUN() = false" in state.text
    assert not marker.exists()


@live
async def test_view_is_complete_where_winprt_would_cut_it(engine):
    """PER.view: WinPRT printed 2 KB of a 1.5 MB window without complaint."""
    await configured_sim(engine)
    per_file = engine.install.sys_dir / "perstm32h7s.per"
    configured = await engine.execute(CommandJob([f'PER.ReProgram "{per_file}"']))
    assert not configured.is_error, configured.text
    reply = await engine.execute(ViewJob("PER.view", max_chars=1000))
    assert "Truncated: showing 1000 of" in reply.text
    size = int(reply.text.rsplit(" characters", 1)[0].rsplit(" of ", 1)[1])
    assert size > 500_000
    path = Path(reply.text.rsplit("Full output: ", 1)[1].strip())
    assert path.is_file()


@live
async def test_a_wrong_option_in_a_window_leads_to_the_manual(engine):
    await configured_sim(engine)
    reply = await engine.execute(ViewJob("Register.view /all"))
    assert reply.is_error and "unknown command" in reply.text
    assert "Register.view exists here" in reply.text and "SpotLight" in reply.text and "/all" in reply.text


@live
async def test_a_wrong_name_in_view_gets_candidates(engine):
    await configured_sim(engine)
    reply = await engine.execute(ViewJob("Data.Disassemble 0x24020000"))
    assert reply.is_error and "Close names:" in reply.text and "Data.Assemble" in reply.text


@live
async def test_a_failed_command_names_what_exists(engine):
    await configured_sim(engine)
    wrong = await engine.execute(CommandJob(["Memory.Set 0x24020000 %Long 1"]))
    assert wrong.is_error and "Memory.Set does not exist" in wrong.text and "Data.Set" in wrong.text
    value = await engine.execute(EvaluateJob(["Trace.RECORD()", "1"]))
    assert not value.is_error and "Trace.RECORDS()" in value.text
    address = await engine.execute(EvaluateJob(["Data.Long(0x24020000)"]))
    assert address.is_error and "access class" in address.text


@live
async def test_a_script_error_is_explained(engine):
    await sim(engine)
    reply = await engine.execute(ScriptJob(None, "SYStem.CPU CortexM7\nMemory.Set 0x1 %Long 1\nENDDO\n", None))
    assert reply.is_error and "Memory.Set does not exist" in reply.text and "Data.Set" in reply.text


@live
async def test_a_failed_lookup_leaves_the_session_usable(engine):
    await sim(engine)
    await engine.execute(CommandJob(["Memory.Set 1"]))
    reply = await engine.execute(CommandJob(["PRINT 1"]))
    assert not reply.is_error, reply.text


@live
async def test_running_target_is_left_alone_at_the_deadline(engine):
    await sim(engine)
    await engine.execute(CommandJob(["SYStem.CPU CortexM7", "SYStem.Up", "Data.Set 0x20000000 %Word 0xE7FE",
                                     "Register.Set PC 0x20000000", "Register.Set R13 0x20010000"]))
    reply = await engine.execute(RunJob("go"), wait_s=5, deadline_s=0.5)
    assert "still running" in reply.text and "target running" in reply.text
    stopped = await engine.execute(RunJob("break"))
    assert "Stopped at" in stopped.text and "0x20000000" in stopped.text.lower()


@live
async def test_blocked_call_is_killed_and_recovered(engine, bench):
    await sim(engine, bench)
    reply = await engine.execute(CommandJob(["WAIT 15.s"]), wait_s=10, deadline_s=1)
    assert reply.is_error and "limit, so PowerView was killed" in reply.text and "session dead" in reply.text
    assert not running_instances()
    refused = await engine.execute(EvaluateJob(["1"]))
    assert refused.is_error and "session_open" in refused.text
    again = await sim(engine, bench)
    assert "generation 2" in again.text and "was still held, so it was reset" in again.text
    assert "g2" in again.text
    assert not (await engine.execute(EvaluateJob(["VERSION.BUILD()"]))).is_error


@live
async def test_crash_is_reported_and_recovered(engine, bench):
    await sim(engine, bench)
    engine.session.instance.process.kill()
    reply = await engine.execute(EvaluateJob(["VERSION.BUILD()"]), wait_s=10)
    assert reply.is_error and "PowerView exited" in reply.text and "session_open" in reply.text
    assert "generation 2" in (await sim(engine, bench)).text


@live
async def test_kill_ends_a_blocked_command(engine, bench):
    await sim(engine, bench)
    first = await engine.execute(CommandJob(["WAIT 40.s"]), wait_s=0.3, deadline_s=60)
    assert "Running:" in first.text
    op = int(first.text.split()[1])
    busy = await asyncio.wait_for(engine.status(), 2)
    assert "Busy: operation" in busy.text
    refused = await engine.cancel(op)
    assert "kill=true" in refused.text
    killed = await engine.cancel(op, kill=True)
    assert killed.is_error and "PowerView exited" in killed.text
    assert "generation 2" in (await sim(engine, bench)).text


@live
async def test_real_connection_reports_the_real_failure(engine, bench):
    reply = await engine.open("arm", "usb", bench, None, None, False)
    assert not reply.is_error, reply.text
    assert "PowerDebug" in reply.text and f"serial {bench}" in reply.text
    powered = await engine.execute(EvaluateJob(["STATE.POWER()"]))
    assert not powered.is_error, powered.text
    if "STATE.POWER() = false" in powered.text:  # exercise SYStem.Up only after a confirmed power-off observation
        failed = await engine.execute(CommandJob(["SYStem.CPU STM32H7S3L8", "SYStem.Up"]))
        assert failed.is_error and "target power fail" in failed.text
    window = await engine.execute(ViewJob("VERSION.HARDWARE"))
    assert "Serial Number" in window.text
    closed = await engine.close()
    assert "PowerView closed" in closed.text


@live
async def test_probe_held_by_a_stranger_needs_takeover(engine, tmp_path, bench):
    outsider = Instance.launch(engine.install, "arm", tmp_path, interactive=True)
    conn = None
    try:
        conn = outsider.connect()
        devices = scan(conn)
        if len(devices) != 1 or devices[0].serial != bench or devices[0].in_use:
            pytest.skip("the selected bench probe is no longer available")
        powerview.establish(conn, bench)
        assert conn.fnc("VERSION.SERIAL.DEBUG()") == bench
        outsider.kill()  # the test's own process now leaves this exact probe held
    finally:
        if conn is None:
            outsider.kill()
        else:
            if outsider.alive:
                outsider.quit(conn) or outsider.kill()
            conn.close()
    time.sleep(1)
    refused = await engine.open("arm", "usb", bench, None, None, False)
    assert refused.is_error and "held by another PowerView" in refused.text and "takeover=true" in refused.text
    time.sleep(0.5)
    taken = await engine.open("arm", "usb", bench, None, None, True)
    assert not taken.is_error and "was still held, so it was reset" in taken.text, taken.text


def user_powerview(tmp_path, api_off: bool = True):
    """A PowerView as a person starts one: no API options on the command line."""
    install = Install.locate()
    config = tmp_path / "user.t32"
    config.write_text(powerview.config_text(install, tmp_path, "PBI=SIM\n"), encoding="ascii")
    script = tmp_path / "typed.cmm"
    script.write_text(f"WAIT 6.s\nSYStem.CPU CortexM7\n{'SETUP.API.RCL.Enable /PRotocol TCP /Port AUTO /RemoteHost DENY'}\nENDDO\n",
                      encoding="ascii")
    return subprocess.Popen([str(install.executable("arm")), "-c", str(config), "-s", str(script)], cwd=tmp_path,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@live
async def test_attach_to_a_powerview_the_user_opened(engine, tmp_path):
    nothing = await engine.open("arm", "attach", None, None, None, False)
    assert nothing.is_error and "no PowerView is running" in nothing.text
    user = user_powerview(tmp_path)
    try:
        time.sleep(3)
        off = await engine.open("arm", "attach", None, None, None, False)
        assert off.is_error and "Remote API is off" in off.text and "SETUP.API.RCL.Enable" in off.text
        end = time.time() + 20
        while time.time() < end:
            found = running_instances()
            if found and found[0].ports:
                break
            time.sleep(0.25)
        joined = await engine.open("arm", "attach", None, None, None, False)
        assert not joined.is_error and "Attached" in joined.text and "CortexM7" in joined.text, joined.text

        refused = await engine.execute(CommandJob(["PRINT 1"]))
        assert refused.is_error and "read-only" in refused.text

        printer = (await engine.execute(EvaluateJob(["PRINTER.FILENAME()"]))).text
        window = await engine.execute(ViewJob("SYStem.state"))
        assert not window.is_error and "Mode" in window.text
        warned = await engine.execute(ViewJob("Data.dump nosuchsymbol_xyz"))
        assert "symbol not found" in warned.text
        clean = await engine.execute(ViewJob("SYStem.state"))
        assert "Warning" not in clean.text, clean.text
        assert (await engine.execute(EvaluateJob(["PRINTER.FILENAME()"]))).text.split("]")[1] == printer.split("]")[1]

        left = await engine.close()
        assert "still running" in left.text and "print file type" in left.text
        assert user.poll() is None
    finally:
        user.kill()
        user.wait()


def test_full_stack_over_real_stdio():
    """The real client, the real server process, the real PowerView."""
    from mcp import Client, StdioServerParameters

    async def body():
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
        params = StdioServerParameters(command=sys.executable, args=["-m", "t32_mcp"], env=env)
        async with Client(params) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"session_open", "session_status", "session_close", "command", "evaluate", "script",
                                  "view", "run", "manual", "operation_get", "operation_cancel"}
            assert "arm" in tools["session_open"].description
            assert "ctx" not in tools["view"].input_schema["properties"]
            assert tools["view"].annotations.read_only_hint and not tools["command"].annotations.read_only_hint

            opened = await client.call_tool("session_open", {"connection": "sim"})
            assert not opened.is_error, opened.content[0].text
            bad = await client.call_tool("command", {"lines": ["LAB_INVALID"]})
            assert bad.is_error and "unknown command" in bad.content[0].text
            good = await client.call_tool("evaluate", {"expressions": ["VERSION.BUILD()"]})
            assert not good.is_error and "0x" in good.content[0].text and good.structured_content is None
            size = len(good.content[0].text)
            assert size < 100, size
            await client.call_tool("session_close", {})
    asyncio.run(body())
    assert not running_instances()
