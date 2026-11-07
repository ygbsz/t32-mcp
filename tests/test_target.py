"""Offline contracts for board scenarios. Each peripheral/CPU observation comes from the fake RAPI."""
from unittest.mock import Mock

import pytest

from t32_mcp import engine as engine_module
from t32_mcp.jobs import CommandJob, EvaluateJob, RunJob
from t32_mcp.powerview import Device, Instance
from t32_mcp.rapi import LinkError
from t32_mcp.views import ViewJob

pytestmark = pytest.mark.anyio


async def checked(engine, job):
    reply = await engine.execute(job)
    assert not reply.is_error, reply.text
    return reply.text


async def test_step_and_return_report_the_observed_function(lab_backend):
    engine, conn = lab_backend
    conn.command_effects["Step"] = lambda: conn.set_stop("worker", "ST:0x20000020")
    conn.command_effects["Go.Up"] = lambda: conn.set_stop("caller", "ST:0x20000040")
    step = await checked(engine, RunJob("step"))
    up = await checked(engine, RunJob("go_up"))
    assert "Stopped at ST:0x20000020 worker" in step and "target stopped" in step
    assert "Stopped at ST:0x20000040 caller" in up
    assert [c for c in conn.commands if c != "ERROR.RESet"] == ["Step", "Go.Up"]


async def test_memory_can_be_read_while_the_target_runs(lab_backend):
    engine, conn = lab_backend
    expr = "Data.Long(EAD:0x20000000)"
    conn.values[expr] = iter([7, 8]).__next__
    conn.command_effects["Go"] = lambda: setattr(conn, "state", "running")
    conn.command_effects["Break"] = lambda: conn.set_stop("heartbeat")
    running = await engine.execute(RunJob("go"), wait_s=0.02)
    assert "Running" in running.text and "target running" in running.text
    first = await checked(engine, EvaluateJob([expr]))
    second = await checked(engine, EvaluateJob([expr]))
    assert f"{expr} = 0x7 (7)" in first and "target running" in first
    assert f"{expr} = 0x8 (8)" in second and "target running" in second
    assert "heartbeat" in await checked(engine, RunJob("break"))
    assert "heartbeat" in (await engine.get(1, wait_s=1)).text
    assert conn.commands.count("Go") == 1


async def test_write_breakpoint_reports_the_writer_or_the_symbol_error(lab_backend):
    engine, conn = lab_backend
    conn.command_errors["Break.Set record.word /Write"] = "symbol not found"
    failure = await engine.execute(CommandJob(["Break.Set record.word /Write", "Go"]))
    assert failure.is_error and "symbol not found" in failure.text
    assert "Go" not in conn.commands

    conn.command_effects["Var.Break.Set record.word /Write"] = lambda: None
    conn.command_effects["Go"] = lambda: conn.set_stop("write_word", "ST:0x20000110")
    conn.values["Var.VALUE(record.word)"] = 0xBAD0C0DE
    await checked(engine, CommandJob(["Var.Break.Set record.word /Write"]))
    assert "Stopped at ST:0x20000110 write_word" in await checked(engine, RunJob("go"))
    assert "0xbad0c0de" in await checked(engine, EvaluateJob(["Var.VALUE(record.word)"]))


async def test_fault_handler_registers_and_caller_are_delivered(lab_backend):
    engine, conn = lab_backend
    conn.command_effects["Go"] = lambda: conn.set_stop("Fault_Handler", "ST:0x20000100")
    cfsr, hfsr, stacked_pc = "Data.Long(SD:0xE000ED28)", "Data.Long(SD:0xE000ED2C)", "Data.Long(D:Register(SP)+0x18)"
    conn.values.update({cfsr: 1 << 16, hfsr: 1 << 30, stacked_pc: 0x20000044})
    conn.windows["Frame.view /Locals /Caller", "ASCIIE"] = "Fault_Handler\ntrigger_fault\nmain"
    assert "Fault_Handler" in await checked(engine, RunJob("go"))
    values = await checked(engine, EvaluateJob([cfsr, hfsr, stacked_pc]))
    assert f"{cfsr} = 0x10000" in values and f"{hfsr} = 0x40000000" in values
    assert f"{stacked_pc} = 0x20000044" in values
    stack = await checked(engine, ViewJob("Frame.view /Locals /Caller"))
    assert "Fault_Handler\ntrigger_fault\nmain" in stack


@pytest.mark.parametrize("saved_hfsr", [0, 0x40000000], ids=["cleared", "preserved"])
async def test_fault_snapshot_after_resume_uses_current_evidence(lab_backend, saved_hfsr):
    engine, conn = lab_backend
    conn.command_effects["Go"] = lambda: setattr(conn, "state", "running")
    conn.command_effects["Break"] = lambda: conn.set_stop("fault_loop")
    conn.values.update({"Var.VALUE(record.hfsr)": saved_hfsr, "Var.VALUE(record.cfsr)": 0x10000})
    running = await engine.execute(RunJob("go"), wait_s=0.02)
    assert "target running" in running.text
    assert "fault_loop" in await checked(engine, RunJob("break"))
    values = await checked(engine, EvaluateJob(["Var.VALUE(record.hfsr)", "Var.VALUE(record.cfsr)"]))
    assert f"Var.VALUE(record.hfsr) = {saved_hfsr:#x} ({saved_hfsr})" in values
    assert "Var.VALUE(record.cfsr) = 0x10000" in values
    assert "fault_loop" in (await engine.get(1, wait_s=1)).text


async def test_perf_snapshot_reports_method_and_running_target(lab_backend):
    engine, conn = lab_backend
    for cmd in ("PERF.RESet", "PERF.METHOD RealTime", "PERF.Arm", "PERF.OFF"):
        conn.command_effects[cmd] = lambda: None
    conn.command_effects["Go"] = lambda: setattr(conn, "state", "running")
    conn.windows["PERF.ListFunc", "ASCIIE"] = "Hits  Function\n  17  slow_work"
    conn.values["PERF.METHOD()"] = 4
    await checked(engine, CommandJob(["PERF.RESet", "PERF.METHOD RealTime", "Go", "PERF.Arm"]))
    view = await checked(engine, ViewJob("PERF.ListFunc"))
    assert "17  slow_work" in view and "target running" in view and "live snapshot" in view
    assert "PERF.METHOD() = 0x4 (4)" in await checked(engine, EvaluateJob(["PERF.METHOD()"]))
    await checked(engine, CommandJob(["PERF.OFF"]))
    assert "Break" not in conn.commands


async def test_command_go_then_run_waits_for_the_existing_operation(lab_backend):
    engine, conn = lab_backend
    conn.command_effects["Go"] = lambda: setattr(conn, "state", "running")
    conn.command_effects["Break"] = lambda: conn.set_stop("wait_for_release")
    first = await checked(engine, CommandJob(["Go"]))
    assert 'run(action="go") waits' in first
    waiting = await engine.execute(RunJob("go"), wait_s=0.02)
    assert "target running" in waiting.text
    assert "wait_for_release" in await checked(engine, RunJob("break"))
    assert "wait_for_release" in (await engine.get(2, wait_s=1)).text
    assert conn.commands.count("Go") == 1


async def test_symbols_keep_the_address_class_and_unknown_values(lab_backend):
    engine, conn = lab_backend
    name, function = "sYmbol.NAME(P:0x20000100)", "sYmbol.FUNCTION(P:Register(PC))"
    conn.values.update({name: "start", function: "worker"})
    reply = await checked(engine, EvaluateJob([name, function, "sYmbol.NAME(0x20000100)"]))
    assert f"{name} = start" in reply and f"{function} = worker" in reply
    assert "sYmbol.NAME(0x20000100): failed:" in reply


async def test_pipeline_counters_and_task_table(lab_backend):
    engine, conn = lab_backend
    conn.command_effects["Go job_done"] = lambda: conn.set_stop("job_done")
    conn.values.update({"ADDRESS.OFFSET(PP())==ADDRESS.OFFSET(job_done)": True,
                        "Var.VALUE(record.produced)": 32, "Var.VALUE(record.consumed)": 31})
    conn.windows["TASK.List.tasks", "ASCIIE"] = "Task       State\nproducer   ready\nconsumer   blocked\nidle       ready"
    done = await checked(engine, RunJob("go", "job_done"))
    assert "job_done" in done and "Warning" not in done
    counts = await checked(engine, EvaluateJob(["Var.VALUE(record.produced)", "Var.VALUE(record.consumed)"]))
    assert "Var.VALUE(record.produced) = 0x20 (32)" in counts and "Var.VALUE(record.consumed) = 0x1f (31)" in counts
    tasks = await checked(engine, ViewJob("TASK.List.tasks"))
    assert "producer   ready\nconsumer   blocked\nidle       ready" in tasks


async def test_connection_loss_and_reopening_a_previously_owned_probe(lab_backend, monkeypatch):
    engine, conn = lab_backend
    old_session = engine.session
    old_session.instance.alive = False
    old_session.instance.exit_code = "1"
    old_session.instance.process = Mock()
    conn.values["1"] = LinkError("connection reset")
    dead = await engine.execute(EvaluateJob(["1"]))
    assert dead.is_error and "PowerView exited" in dead.text and "session dead" in dead.text
    refused = await engine.execute(CommandJob(["never"]))
    assert refused.is_error and "never" not in conn.commands

    # Exercise normal Engine.open and its probe ownership checks using only test doubles.
    executable = engine.install.executable("arm")
    executable.parent.mkdir(parents=True)
    executable.touch()
    next_instance = Mock(spec=Instance, pid=43)
    next_instance.connect.return_value = type(conn)()
    monkeypatch.setattr(Instance, "launch", Mock(return_value=next_instance))
    serial = old_session.serial
    scan = Mock(side_effect=[[Device(1, serial, "Mock", "USB", in_use)] for in_use in (True, False)])
    reset = Mock(return_value=("returned", 0))
    establish = Mock()
    monkeypatch.setattr(engine_module, "scan", scan)
    monkeypatch.setattr(engine_module, "reset_device", reset)
    monkeypatch.setattr(engine_module, "establish", establish)
    again = await engine.open("arm", "usb", serial, None, None, False)
    assert not again.is_error and "generation 2" in again.text and "was still held, so it was reset" in again.text
    reset.assert_called_once_with(next_instance.connect.return_value, serial)
    establish.assert_called_once_with(next_instance.connect.return_value, serial)
