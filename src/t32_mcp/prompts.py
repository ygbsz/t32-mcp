"""Every string that reaches the model lives here or in replies built from here.

Rules, pinned by tests/test_pure.py: English only, no bold, no emoji, and
descriptions stay under 2048 characters, because some clients truncate longer ones silently.
"""

INSTRUCTIONS = """\
t32-mcp controls a Lauterbach TRACE32 debugger: PRACTICE commands and scripts, \
registers, memory, windows, trace and coverage analysis, and target run control.

Call session_open first. It connects only: it does not reset, download or run. \
Target setup is yours to write. session_status reports the demo directory; find a \
start-up script for your chip there, read it, adapt it, then run it with script. \
Demo scripts can reset the target or program flash.

A command that returns OK may not have taken effect. Read settings back with \
evaluate. Save raw trace with Trace.SAVE before analysing it.

If PowerView dies, call session_open again. Target state is lost, so rerun setup.

Numbers are hex; 32. with a dot is decimal. An address needs an access class: D:0x20000000 \
for data, P:0x08000000 for code, EAD:0x20000000 to read data while the target runs.

When you are not sure what a command is called or what it takes, ask manual before guessing.

Common windows and functions: Var.VALUE(a.b), Var.View %Open a, List.Mix <symbol> for code, \
Frame.view /Locals /Caller for the stack, sYmbol.NAME(P:0x08000000) for the symbol at an \
address. Break.Set <function> stops at code; Var.Break.Set a.b /Write stops on a write to a \
variable or struct member.

Other things TRACE32 does, which you will not stumble on: PERF profiles by sampling while \
the target keeps running: PERF.RESet, PERF.Mode PC, PERF.METHOD RealTime, PERF.Arm, then \
view PERF.ListFunc. TASK shows RTOS tasks after TASK.CONFIG with a file from \
demo/arm/kernel. PER shows peripheral registers. BMC counts core events. COVerage measures \
code coverage from trace.
"""

SESSION_OPEN = """\
Start or join a TRACE32 PowerView. Connects only: it does not reset, download or run.

connection:
- usb: start a PowerView on the probe.
- sim: start the instruction set simulator, licensed by the probe when one is free.
- attach: join a PowerView the user already opened. Read-only unless access="control". \
Open it read-only and look first. Stopping the target or writing to it changes what the user \
is debugging, so ask the user before asking for control.

Installed targets: {targets}. TRACE32 directory: {sys_dir}.
"""

SESSION_STATUS = """\
Report the session: PowerView version, probe, license, CPU, target state, script \
depth, and the demo, manual and session directories. Works while an operation is blocked.
"""

SESSION_CLOSE = """\
End the session. A PowerView started here quits, unless leave_running is true, which \
hands it to the user. An attached PowerView is only disconnected.
"""

COMMAND = """\
Run PRACTICE commands in order. Stops at the first failure and reports what ran.

OK means TRACE32 accepted the command, so read settings back with evaluate. \
Use script for DO, session_close for QUIT, and run for Go when you need to wait for \
the target to stop. Commands can execute host programs through OS.Command. Wait for this \
reply before calling run: calls sent together run even if the setup failed.
"""

EVALUATE = """\
Evaluate PRACTICE functions, for example Register(PC), Data.Long(D:0x20000000), \
Var.VALUE(counter), Trace.RECORDS(). Each expression succeeds or fails on its own, so \
pass everything you need in one call. An expression that calls a target function runs \
it on the target.
"""

SCRIPT = """\
Run a PRACTICE script from a file or from text and wait for it to end. Output from \
PRINT is returned. If it stops on an error the reply gives the failing command, and \
commands before it have already taken effect. A script that opens a dialog cannot \
continue, because nobody can click it.
"""

VIEW = """\
Read a TRACE32 window as text, for example Register.view, Frame.view /Locals /Caller, \
Data.dump D:0x20000000++0xFF, Break.List, Trace.STATistic.Func, TASK.List.tasks, \
PER.view. Output past max_chars stays in a file, and its path is in the reply.

Use full command names. Only display windows are accepted; for anything else use \
command with PRinTer.FILE and WinPrint.

Give Trace.List a record range such as Trace.List (-1000.)--(-1.): a full list can be \
gigabytes. Reading PER windows can touch registers that have read side effects.
"""

RUN = """\
Control the target: go, step, step_over, go_up, go_return, break. go waits until the \
target stops, also when it is already running. break stops the target now, wherever it \
is, so to wait for a breakpoint use go, not break. With until, go runs to that address \
and the reply says whether it got there. Reaching the deadline leaves the target \
running: nothing stops it for you.
"""

MANUAL = """\
Look something up in the TRACE32 manuals installed here. Give a name for its syntax, \
options and subcommands, or a few English words for candidates: Break.Set, Data.Long, PERF, \
flash programming. Add the target when a page differs by target: SYStem.MemAccess cortexm. \
An RTOS name alone lists its commands and setup: freertos. \
Needs no session. The manuals cover every target, so a name found here may not exist on \
yours: PRACTICE.CoMmanD.AVAILable(<name>) and PRACTICE.FUNCtion.AVAILable(<name>) tell.
"""

OPERATION_GET = """\
Wait for an operation that replied Running, and return its final reply. Finished \
operations return the same reply again.
"""

OPERATION_CANCEL = """\
Cancel an operation. A queued one is dropped, a script is stopped, a run stops waiting \
and leaves the target running. A blocked command can only be ended with kill=true, \
which kills PowerView and loses target state.
"""

PARAMS = {
    "target": "PowerView flavour, from the installed targets.",
    "connection": "usb, sim or attach.",
    "serial": "Probe serial number. Needed when several probes are connected.",
    "port": "Remote API TCP port of the PowerView to attach to. Found automatically when omitted.",
    "access": "attach only. observe is read-only and the default; control also allows command, script and "
              "run. Ask the user before using control.",
    "takeover": "Reset a probe held by an unknown PowerView. Ends that PowerView's session.",
    "leave_running": "Keep the PowerView open for the user instead of quitting it.",
    "lines": "PRACTICE commands, one per item.",
    "stop_on_error": "Skip the remaining lines after a failure.",
    "wait_s": "How long this call waits before replying Running. The work continues either way.",
    "deadline_s": "Limit for the whole operation. Raise it for slow work such as flash programming.",
    "expressions": "PRACTICE function expressions.",
    "path": "Absolute path of a .cmm script.",
    "text": "Script text, used when there is no file.",
    "args": "Arguments passed to the script.",
    "window": "Command that opens the window, with its arguments.",
    "format": "text, or csv for tables with raw numbers and fewer characters.",
    "max_chars": "Characters returned in the reply.",
    "action": "What to do to the target.",
    "until": "Address expression where go should stop, such as a symbol.",
    "operation": "Operation number from the reply header.",
    "query": "A command or function name, or a few words for what you want to do.",
    "get_wait_s": "How long to wait, up to 30 seconds.",
    "kill": "Kill PowerView to end a blocked command. Target state is lost.",
}

NO_SESSION_NEXT = "Next: call session_open."
ENABLE_API = "SETUP.API.RCL.Enable /PRotocol TCP /Port AUTO /RemoteHost DENY"
