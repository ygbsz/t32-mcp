"""Check TRACE32 windows using an ELF and matching saved trace in an Arm simulator.

Use a free probe to license the simulator. Supply the CPU used for the capture.

    python tools/window_sweep.py app.elf run.ad --cpu CortexM7
    python tools/window_sweep.py app.elf run.ad coverage.ad report.json --function main
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from t32_mcp.engine import Engine  # noqa: E402
from t32_mcp.jobs import CommandJob  # noqa: E402
from t32_mcp.powerview import Install  # noqa: E402
from t32_mcp.views import ViewJob, check_window  # noqa: E402

GENERAL = """AREA.view|Register.view|Data.dump PP()|Data.List PP()|List.auto|Frame.view|Frame.view /Locals /Caller|sYmbol.Browse
sYmbol.List.Map|Break.List|MAP.List|PER.view|VERSION.view|VERSION.SOFTWARE|VERSION.HARDWARE|VERSION.ENVironment|LICENSE.List|SYStem.state
Trace.state|Trace.List (-100.)--(-1.)|Trace.Chart.sYmbol|Trace.Chart.Func|Trace.Timing|Trace.STATistic.Func|Trace.STATistic.TREE
Trace.STATistic.sYmbol|Trace.STATistic.Ignore
Trace.STATistic.DIStance|Trace.ListNesting|Trace.PROfileChart.sYmbol|Trace.PROfileSTATistic.sYmbol|CTS.state|CTS.List|ISTATistic.state
PERF.state|PERF.ListFunc|BMC.state|ETM.state|ITM.state|TPIU.state|SNOOPer.state|SNOOPer.List|RTS.state
Data.dump nosuchsymbol_xyz|Data.List nosuchsymbol_xyz|List.Mix nosuchsymbol_xyz|Var.View nosuchvar_xyz|sYmbol.INFO nosuchsymbol_xyz
Trace.STATistic.ChildTREE nosuchsymbol_xyz"""
TASK = "TASK.List.tasks|TASK.TaskList|TASK.Queue|TASK.Semaphore|TASK.STacK.view|Trace.Chart.TASK|Trace.STATistic.TASK|Trace.STATistic.TASK ALL|Trace.STATistic.TASKFunc|PERF.ListTASK|Frame.view /Task \"nosuchtask\""
COVERAGE = "COVerage.state|COVerage.ListModule|COVerage.ListFunc|COVerage.ListLine|COVerage.ListVar"
LIMIT_S = 10.0


def trace_commands(args, trace: Path, coverage: bool) -> list[str]:
    commands = [f"SYStem.CPU {args.cpu}", "SYStem.Up", f'Data.LOAD.Elf "{args.elf}"']
    if args.task_config:
        commands.append(f'TASK.CONFIG "{args.task_config}"')
    if coverage:
        commands.extend(["COVerage.RESet", "COVerage.METHOD INCremental", "COVerage.Option.SourceMetric ObjectCode"])
    commands.extend([f'Trace.LOAD "{trace}"', "Trace.FLOWPROCESS"])
    if coverage:
        commands.append("COVerage.ADD /NoDataTrace")
    return commands


async def setup(engine: Engine, commands: list[str]) -> None:
    opened = await engine.open("arm", "sim", None, None, None, False)
    if opened.is_error:
        raise RuntimeError(opened.text)
    reply = await engine.execute(CommandJob(commands), wait_s=65, deadline_s=60)
    if reply.is_error:
        raise RuntimeError(reply.text)


def classify(text: str, is_error: bool) -> str:
    if "session dead" in text:
        return "killed" if "limit" in text else "crash"
    if is_error:
        return "error"
    return "empty" if "no output" in text else "ok"


async def sweep(args) -> None:
    out = args.report.resolve()
    engine = Engine(Install.locate(), state_root=out.parent / "sweep-sessions")
    windows = [w for line in GENERAL.splitlines() for w in line.split("|")]
    if args.task_config:
        windows.extend(TASK.split("|"))
    if args.function:
        windows.extend(f"{name} {args.function}" for name in ("Data.dump", "List.Mix", "sYmbol.INFO",
                                                               "Trace.STATistic.ChildTREE", "Trace.STATistic.LINKage"))
    if args.variable:
        windows.extend(f"{name} %E {args.variable}" for name in ("Var.View", "Var.Watch"))
    rows: dict[str, dict] = {}
    scenes = [(windows, trace_commands(args, args.trace, False))]
    if args.coverage_trace:
        scenes.append((COVERAGE.split("|"), trace_commands(args, args.coverage_trace, True)))
    try:
        for names, commands in scenes:
            await setup(engine, commands)
            for window in names:
                row = rows.setdefault(window, {})
                if check_window(window):
                    row["refused"] = True
                    continue
                for fmt in ("text", "csv"):
                    start = time.perf_counter()
                    reply = await engine.execute(ViewJob(window, fmt, max_chars=200000), wait_s=LIMIT_S + 5, deadline_s=LIMIT_S)
                    size = len(reply.text)
                    row[fmt] = {"status": classify(reply.text, reply.is_error), "chars": size,
                                "ms": round((time.perf_counter() - start) * 1000), "warning": "Warning:" in reply.text}
                    if row[fmt]["status"] in ("crash", "killed"):
                        out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
                        await setup(engine, commands)
                print(f"{window[:46]:46} " + " ".join(f"{f}={v['status']}/{v['chars']}" for f, v in row.items() if isinstance(v, dict)), flush=True)
                out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
            await engine.close()
    finally:
        await engine.shutdown()
    for fmt in ("text", "csv"):
        print(fmt, dict(collections.Counter(r[fmt]["status"] for r in rows.values() if fmt in r)))


def existing_file(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"file not found: {value}")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("elf", type=existing_file, help="ELF with symbols matching the supplied trace files")
    parser.add_argument("trace", type=existing_file, help="TRACE32 capture saved with Trace.SAVE")
    parser.add_argument("coverage_trace", nargs="?", type=existing_file, help="optional capture for ObjectCode coverage")
    parser.add_argument("report", nargs="?", type=Path, default=Path("window_sweep.json"), help="output JSON path")
    parser.add_argument("--cpu", default="CortexM7", help="TRACE32 processor name (default: CortexM7)")
    parser.add_argument("--task-config", type=existing_file, help="OS Awareness definition for task windows")
    parser.add_argument("--function", help="ELF function for symbol and call-tree windows")
    parser.add_argument("--variable", help="ELF variable for variable windows")
    args = parser.parse_args()
    if args.report.resolve() in (args.elf, args.trace, args.coverage_trace, args.task_config):
        parser.error("choose a report path distinct from the input files")
    asyncio.run(sweep(args))


if __name__ == "__main__":
    main()
