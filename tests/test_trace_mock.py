"""Exercise the public jobs with synthetic trace statistics, window output and stop positions."""

import pytest

from t32_mcp.jobs import CommandJob, EvaluateJob, RunJob
from t32_mcp.views import ViewJob


pytestmark = pytest.mark.anyio


async def test_trace_and_symbol_windows_are_delivered(lab_backend):
    engine, conn = lab_backend
    conn.windows.update({
        ("Trace.FindAll , FLOWERROR", "ASCIIE"): "No matching records",
        ("sYmbol.List.PROgram", "ASCIIE"): "worker_demo.elf",
    })
    found = await engine.execute(ViewJob("Trace.FindAll , FLOWERROR"))
    symbols = await engine.execute(ViewJob("sYmbol.List.PROgram"))
    assert not found.is_error and "No matching records" in found.text
    assert not symbols.is_error and "worker_demo.elf" in symbols.text


async def test_trace_statistics_and_tree_formats(lab_backend):
    engine, conn = lab_backend
    load = 'Trace.LOAD "synthetic.ad"'
    flow = "Trace.FLOWPROCESS"
    analyze = "Trace.STATistic.Func ALL"
    count = "Trace.STATistic.COUNT(dispatch_item)"
    conn.command_effects[load] = lambda: conn.values.update({"Trace.RECORDS()": 256})
    conn.command_effects[flow] = lambda: conn.values.update({"Trace.FLOW.ERRORS()": 0, "Trace.FLOW.FIFOFULL()": 0})
    conn.command_effects[analyze] = lambda: conn.values.update({count: 3})
    tree = "Trace.STATistic.TREE"
    conn.windows[(tree, "ASCIIE")] = "Function         Inclusive\ndispatch_item    0.375ms"
    conn.windows[(tree, "CSV")] = '"range","tree"\n"0.375ms","dispatch_item"'

    setup = await engine.execute(CommandJob([load, flow, analyze]))
    assert not setup.is_error, setup.text
    assert [line for line in conn.commands if line in (load, flow, analyze)] == [load, flow, analyze]
    stats = await engine.execute(EvaluateJob(["Trace.RECORDS()", "Trace.FLOW.ERRORS()", "Trace.FLOW.FIFOFULL()", count]))
    assert not stats.is_error
    assert "Trace.RECORDS() = 0x100 (256)" in stats.text
    assert "Trace.FLOW.ERRORS() = 0x0 (0)" in stats.text and "Trace.FLOW.FIFOFULL() = 0x0 (0)" in stats.text
    assert f"{count} = 0x3 (3)" in stats.text
    text = await engine.execute(ViewJob(tree))
    csv = await engine.execute(ViewJob(tree, format="csv"))
    assert not text.is_error and "dispatch_item    0.375ms" in text.text
    assert not csv.is_error and '"range","tree"\n"0.375ms","dispatch_item"' in csv.text


async def test_coverage_metrics_and_list_window(lab_backend):
    engine, conn = lab_backend
    setup = ["COVerage.RESet", "COVerage.METHOD INCremental", "COVerage.Option.SourceMetric ObjectCode",
             'Trace.LOAD "synthetic.ad"', "Trace.FLOWPROCESS", "COVerage.ADD /NoDataTrace"]
    short = "COVerage.BDONE(Var.RANGE(short_path))"
    long = "COVerage.BDONE(Var.RANGE(long_path))"
    for line in setup[:-1]:
        conn.command_effects[line] = lambda: None
    conn.command_effects[setup[-1]] = lambda: conn.values.update({short: 12, long: 0})
    conn.windows[("COVerage.ListFunc", "ASCIIE")] = "Function      Covered bytes\nReset_Handler  12"

    reply = await engine.execute(CommandJob(setup))
    assert not reply.is_error, reply.text
    assert [line for line in conn.commands if line in setup] == setup
    values = await engine.execute(EvaluateJob([short, long]))
    assert not values.is_error and f"{short} = 0xc (12)" in values.text and f"{long} = 0x0 (0)" in values.text
    window = await engine.execute(ViewJob("COVerage.ListFunc"))
    assert not window.is_error and "Reset_Handler  12" in window.text


@pytest.mark.parametrize("stopped_at, reached", [("scheduler_ready", True), ("early_break", False)])
async def test_run_checks_the_requested_stop_and_can_step(lab_backend, stopped_at, reached):
    engine, conn = lab_backend
    go = "Go scheduler_ready"
    compare = "ADDRESS.OFFSET(PP())==ADDRESS.OFFSET(scheduler_ready)"
    conn.values[compare] = reached
    conn.command_effects[go] = lambda: conn.set_stop(stopped_at, "ST:0x20000000")
    conn.command_effects["Step"] = lambda: conn.set_stop("next_instruction", "ST:0x20000002")
    reply = await engine.execute(RunJob("go", "scheduler_ready"))
    assert not reply.is_error and f"Stopped at ST:0x20000000 {stopped_at}" in reply.text
    if reached:
        assert "Warning" not in reply.text
        step = await engine.execute(RunJob("step"))
        assert not step.is_error and "Stopped at ST:0x20000002 next_instruction" in step.text
    else:
        assert "Warning: stopped before reaching scheduler_ready" in reply.text
    assert conn.commands.count(go) == 1
