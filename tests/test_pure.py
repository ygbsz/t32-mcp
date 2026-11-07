"""L0: no PowerView involved."""

import inspect
import re

import pytest

from t32_mcp import prompts
from t32_mcp.jobs import CommandJob
from t32_mcp.rapi import Message, T32Error
from t32_mcp.replies import Reply, failed, format_value, render, tidy, warning
from t32_mcp.scripts import (DIALOG, DONE, DONE_WITH_ERRORS, ON_ERROR, PAUSED, RUNNING, classify)
from t32_mcp.session import Session
from t32_mcp.views import check_window


@pytest.mark.parametrize("window", [
    "Register.view", "Data.dump 0x20000000", "List.Mix main", "List.auto", "Var.View %E current_task",
    "Frame.view /Locals /Caller", "sYmbol.Browse", "sYmbol.INFO main", "sYmbol.List.Map", "Break.List",
    "MAP.List", "PER.view", "VERSION.view", "VERSION.HARDWARE", "LICENSE.List", "SYStem.state", "AREA.view",
    "TASK.List.tasks", "TASK.TaskList", "TASK.Queue", "TASK.STacK.view", "Trace.state", "Trace.List (-40.)--(-1.)",
    "Trace.Chart.sYmbol", "Trace.STATistic.TREE", "Trace.STATistic.TASK ALL", "Trace.Timing",
    "Trace.PROfileChart.sYmbol", "CTS.List", "PERF.ListFunc", "BMC.state", "COVerage.ListFunc", "COVerage.ListLine",
])
def test_display_windows_pass(window):
    assert check_window(window) is None


# Each of these really runs when prefixed with WinPrint.
@pytest.mark.parametrize("window", [
    "Go", "Step", "Data.Set 0x24000000 %Long 0x11223344", "Register.Set R0 0x55", "Break.Set 0x24020400",
    "OS.Hidden cmd /c echo x", "OS.Command calc", 'DO "x.cmm"', "SYStem.Down", "SYStem.Up", "SYStem.RESet", "QUIT",
    "Trace.SAVE x.ad", "Trace.Init", "TASK.CONFIG x.t32", "TASK.STacK.Init", "COVerage.ADD", "COVerage.RESet",
    "Data.LOAD.Elf x.elf", "FLASH.Erase ALL", "CONNECTION.ResetDevice 0.", "SETUP.API.RCL.Enable",
    "Trace.List.Set x", "Data.dump 0x0\nGo", "&macro", "", "   ",
])
def test_everything_else_is_refused(window):
    reply = check_window(window)
    assert reply is not None and reply.is_error


def test_known_hang_names_the_alternative():
    assert "Var.View" in "\n".join(check_window("Var.DRAW pxCurrentTCB").lines)


# The two rows that were measured separately are the point of this table.
@pytest.mark.parametrize("state,depth,occurred,expected", [
    (1, 1, False, RUNNING),
    (0, 1, True, ON_ERROR),
    (2, 1, False, DIALOG),
    (2, 1, True, DIALOG),
    (2, 0, False, DONE),  # stale: the state stays 2 after the dialog is gone
    (0, 1, False, PAUSED),
    (0, 0, True, DONE_WITH_ERRORS),
    (0, 0, False, DONE),
])
def test_script_verdicts(state, depth, occurred, expected):
    assert classify(state, depth, 0, occurred) == expected


def test_depth_is_relative_to_the_script_that_started():
    assert classify(0, 2, 2, False) == DONE


def test_reply_shape():
    text = render(12, 1, "target stopped", 23, Reply(content="body", tail=["Warning: x"], label="Trace.List"))
    assert text == "[op 12 · g1 · target stopped · 23 ms · Trace.List]\n\nbody\n\n---\nWarning: x"


def test_reply_without_content_has_no_separator():
    text = render(7, 1, "target down", 3, failed("bad", next_step="do this."))
    assert text == "[op 7 · g1 · target down · 3 ms]\nFailed: bad\nNext: do this."


def test_durations():
    assert "5.0 s" in render(1, 1, "x", 5000, Reply())


@pytest.mark.parametrize("value,expected", [(True, "true"), (False, "false"), (16, "0x10 (16)"), (0, "0x0 (0)"),
                                            (-1, "-1"), ("ST:0x24", "ST:0x24"), (1.5, "1.5")])
def test_values(value, expected):
    assert format_value(value) == expected


def test_tidy_removes_padding_only():
    assert tidy("a   \r\n\r\nb  \r\n\r\n\r\n") == "a\n\nb"


# Text that reaches the model.

MODEL_TEXT = {k: v for k, v in vars(prompts).items() if k.isupper() and isinstance(v, str)}
MODEL_TEXT.update({f"PARAMS.{k}": v for k, v in prompts.PARAMS.items()})


@pytest.mark.parametrize("name", sorted(MODEL_TEXT))
def test_model_text_is_plain_english(name):
    text = MODEL_TEXT[name]
    assert not re.search(r"[\u4e00-\u9fff]", text), "Chinese"
    assert "**" not in text, "bold"
    assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text), "emoji"


@pytest.mark.parametrize("name", sorted(MODEL_TEXT))
def test_descriptions_fit_the_truncation_line(name):
    text = MODEL_TEXT[name]
    if "{targets}" in text:
        text = text.format(targets="arm, riscv, xtensa, tricore, ppc, x86", sys_dir=r"C:\Program Files\Lauterbach\T32")
    assert len(text) <= 2048


def test_instructions_say_what_this_is_first():
    assert prompts.INSTRUCTIONS.startswith("t32-mcp controls a Lauterbach TRACE32 debugger")


def test_no_chinese_in_any_string_that_reaches_the_model():
    import t32_mcp
    from pathlib import Path
    for path in Path(t32_mcp.__file__).parent.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"[\u4e00-\u9fff]", source), path.name


# Error attribution with a scripted PowerView.

class FakeConn:
    def __init__(self, messages=(), occurred=False, fail=None):
        self.commands, self.messages, self.occurred, self.fail = [], list(messages), occurred, fail

    def cmd(self, command):
        self.commands.append(command)
        if self.fail and command == self.fail:
            raise T32Error("symbol not found")

    def fnc(self, expression):
        return {"ERROR.OCCURRED()": self.occurred, "ERROR.MESSAGE()": "from error state"}[expression]

    def message(self):
        return self.messages.pop(0)


def session(conn, owned=True):
    s = Session.__new__(Session)
    s.conn, s.instance = conn, object() if owned else None
    return s


def test_owned_clears_error_state_before_the_command():
    conn = FakeConn([Message(0, "old text")])
    assert session(conn).run_line("Go").ok
    assert conn.commands == ["ERROR.RESet", "Go"]


def test_owned_type_zero_message_is_ignored():
    result = session(FakeConn([Message(0, "Warning: stale")])).run_line("x")
    assert result.ok and result.warning is None and result.output is None


def test_exception_is_a_failure():
    assert session(FakeConn(fail="x")).run_line("x").error == "symbol not found"


def test_error_state_catches_what_raised_nothing():
    result = session(FakeConn([Message(0, "")], occurred=True)).run_line("x")
    assert not result.ok and result.error == "from error state"


def test_warning_is_kept():
    assert session(FakeConn([Message(8, "Warning: symbol not found")])).run_line("x").warning


def test_attached_never_clears_the_users_error_state():
    conn = FakeConn([Message(0, "old"), Message(0, "old")])
    assert session(conn, owned=False).run_line("x").ok
    assert conn.commands == ["x"]


def test_attached_ignores_an_unchanged_message():
    old = Message(16, "unknown command")
    assert session(FakeConn([old, old]), owned=False).run_line("x").ok


def test_attached_sees_a_new_message():
    conn = FakeConn([Message(0, "old"), Message(16, "target system down")])
    result = session(conn, owned=False).run_line("x")
    assert not result.ok and result.error == "target system down"


# Commands that cannot do what the caller expects never reach PowerView.

@pytest.mark.parametrize("line", ["DO x.cmm", "do x.cmm", "  Do x", "QUIT", "quit"])
def test_refused_lines_stop_everything(line):
    conn = FakeConn()
    reply = CommandJob(["PRINT 1", line]).start(session(conn))
    assert reply.is_error and conn.commands == []


def test_failure_reports_what_ran_and_what_was_skipped():
    conn = FakeConn([Message(0, ""), Message(0, "")], fail="b")
    reply = CommandJob(["a", "b", "c", "d"]).start(session(conn))
    text = "\n".join(reply.lines)
    assert reply.is_error
    assert "line 2 of 4: symbol not found" in text and "Line 1 ran." in text and "2 later lines skipped." in text
    assert "c" not in conn.commands


def test_continue_after_failure_when_asked():
    conn = FakeConn([Message(0, ""), Message(0, "")], fail="a")
    reply = CommandJob(["a", "b"], stop_on_error=False).start(session(conn))
    assert reply.is_error and "b" in conn.commands


def test_parameters_are_documented():
    from t32_mcp.server import P
    source = inspect.getsource(__import__("t32_mcp.server", fromlist=["x"]))
    for key in re.findall(r'P\["(\w+)"\]', source):
        assert key in P


def test_a_trace32_warning_is_not_labelled_twice():
    assert warning("Warning: file x not found") == "Warning: file x not found"
    assert warning("file x not found") == "Warning: file x not found"


@pytest.mark.parametrize("window", ["Trace.FindAll , FLOWERROR", "sYmbol.List.PROgram"])
def test_windows_the_name_check_used_to_refuse(window):
    assert check_window(window) is None


@pytest.mark.parametrize("window", ["FLASH.Program", "Data.PROGRAM", "sYmbol.List.Program.Set"])
def test_the_exceptions_do_not_open_the_list_up(window):
    assert check_window(window).is_error


def test_a_refused_window_says_how_to_print_it_anyway():
    text = "\n".join(check_window("Trace.Find").lines)
    assert "not on the list" in text and "WinPrint" in text
