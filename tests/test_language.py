"""The reply to a line that failed. A tiny archive pins the logic; the real one pins its format."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from t32_mcp import language, manuals
from t32_mcp.language import explain_command, explain_function, explain_unknown_window
from t32_mcp.manuals import load_help
from t32_mcp.rapi import LinkError, T32Error
from t32_mcp.scripts import ON_ERROR, ScriptJob

INDEX = '''"Data.Set","Modify memory","command","G100     ","*","1"
"Data.Assemble","Built-in assembler","command","G101     ","*","1"
"D.S [Data.Set]","Modify memory","command","G100     ","*","1"
"Modify memory [Data.Set]"," ","command","G100     ","*","1"
"Register.view","Display registers","command","G102     ","*","1"
"List.Mix","Show disassembled mnemonics","command","G103     ","*","1"
"TASK.MuTeX","Display mutex","command","G104     ","rtosfoo","1"
"Trace.RECORDS [Trace.RECORDS()]","Get number of used trace records","function","G105     ","*","1"
"Trace.RECORD.TIME [Trace.RECORD.TIME(<record_number>)]","Get timestamp of trace record","function","G106     ","*","1"
"TITLE [TITLE()]","Caption of the TRACE32 main window","function","G107     ","*","1"
"SYStem.MemAccess","Select memory access","command","G200     ","*","3"
"SYStem.MemAccess","Select memory access","command","G201     ","bdmcortexm","2"
"Go","Start the program","command","G210     ","*","3"
"Go","Start the program","command","G211     ","*","4"
"Go","Start the program","command","G212     ","bdmcortexm","2"
"PERF","Sample-based profiling","command !","G220     ","*","1"
"PERF.METHOD","Set the method","command","G221     ","*","1"
"PERF.METHOD [PERF.METHOD()]","Recording method","function","G222     ","*","1"
"PERF.Arm","Activate the analyzer","command","G223     ","*","1"
"PERF.Mode","Select the mode","command","G224     ","*","1"
"PERF.Secret","For internal usage only","command","G225     ","*","1"
"D.L [Data.Long(<address>)]"," ","function","G230     ","*","1"
"STATE.RUN [STATE.RUN()]"," ","function","G231     ","*","1"
"Short.Page","No syntax here","command","G240     ","*","1"
"Break.Set","Set breakpoints","command !","G241     ","*","1"
"<trace>.STATistic.Func","Nesting function runtime analysis","command","G250     ","*","1"
"Trace.STATistic.Func","Nesting function runtime analysis","command","G250     ","*","1"
"Analyzer.STATistic.Func","Nesting function runtime analysis","command","G250     ","*","1"
"Hidden.Thing","A secret, for internal usage only","command","G260     ","*","1"
"TASK.Semaphore","Display semaphores","command","G300     ","rtosfreertos","6"
"TASK.Queue","Display queues","command","G301     ","rtosfreertos","6"
'''

STX = '''G102      H Register.view Display registers
G900      X [Options]
G901      X Format:
G902      X Register.view [/<option>]
G903      X <option>:
G904      X SpotLight | Stack
G905      X [build 187291 - DVD 02/2026]
G906      X Register.view /Old (deprecated)
G907      X Displays the core registers of the selected core in a window.
G908      H Next heading
G100      H Data.Set Modify memory
G910      X This one has no syntax block near its heading.
G911      X Nor here.
G912      X Nor here.
G913      X Format:
G103      H List.Mix Show disassembled mnemonics
G920      X Format:
G921      X This page goes straight on in sentences and never gives any syntax at all.
G221      H PERF.METHOD Set the method
G226      X Format:
G227      X PERF.METHOD RealTime | StopAndGo
G222      H PERF.METHOD() Recording method
G228      X Syntax:
G229      X PERF.METHOD()
G230      H Data.Long() Reads memory
G233      X Syntax:
G234      X Data.Long(<address>)
G235      X Reads a long word from memory at the address.
G231      X STATE.RUN()
G232      X Returns the state of the run flag here.
G240      H Short.Page No syntax here
G242      H Another heading
G243      X Format:
G244      X Another.Page
G241      H Break.Set Set breakpoints
G245      X Format:
G246      X Break.Set [<address>]
G247      E Break.Set main ; stop in main
G248      E Data.dump 0
G250      H <trace>.STATistic.Func Nesting function runtime analysis
G251      X Format:
G252      X <trace>.STATistic.Func [<items>]
G270      H Target Console
G271      X Note that semihosting works over the debug port here.
'''
CORTEXM_STX = '''G201      H SYStem.MemAccess Select memory access
G281      X Format:
G282      X SYStem.MemAccess AHB | DAP
G212      H Go Start the program
G287      X Starts the program, and this page is only for cortexm.
'''
SPARC_STX = '''G200      H SYStem.MemAccess Select memory access
G283      X Format:
G284      X SYStem.MemAccess NEXUS | Enable
G210      H Go Start the program
G285      X Starts the program, and this page is only for sparc.
'''
GENERAL_STX = '''G211      H Go Start the program
G286      X Starts the program, and this text is from the general manual.
'''


def build_archive(path: Path, index: str = INDEX) -> Path:
    names = ("x", "m", "debugger_cortexm", "debugger_sparc", "general_ref_g", "release_new", "rtos_freertos")
    members = {"_filelist.txt": "".join(f'"{n}.pdf","X","Manuals","*","0","1","2","3"\r\n' for n in names),
               "_index.txt": index.replace("\n", "\r\n"), "m.stx": STX.replace("\n", "\r\n"),
               "debugger_cortexm.stx": CORTEXM_STX.replace("\n", "\r\n"),
               "debugger_sparc.stx": SPARC_STX.replace("\n", "\r\n"), "general_ref_g.stx": GENERAL_STX.replace("\n", "\r\n"),
               "release_new.stx": "G290      H Build 1\r\nG291      X A zebrafish feature landed in this build today.\r\n",
               "rtos_freertos.stx": "G295      H Configuration\r\nG296      X TASK.CONFIG ~~/demo/<arch>/kernel/freertos/freertos.t32\r\n"
                                    "G300      H TASK.Semaphore Display semaphores\r\nG301      H TASK.Queue Display queues\r\n"}
    offset = 128 * (len(members) + 1)
    rows, bodies = [], b""
    for name, body in members.items():
        data = body.encode("latin-1")
        rows.append(f"{name:<48}{offset + len(bodies):>16}{len(data):>16} 01-01-26 00:00:00".ljust(126).encode() + b"\r\n")
        bodies += data
    first = f"{'help.t32':<48}{0:>16}{offset + len(bodies):>16}{len(members):>16} 01-01-26 00:00:00".ljust(126).encode()
    path.write_bytes(first + b"\r\n" + b"".join(rows) + bodies)
    return path


class Conn:
    """TRACE32 answers for exactly the names given."""

    def __init__(self, names=(), failure=None):
        self.names = {n.lower() for n in names}
        self.failure = failure
        self.asked = []

    def fnc(self, expression):
        if self.failure:
            raise self.failure
        self.asked.append(expression)
        name = expression.split("(", 1)[1].rstrip(")").lower()
        return "True" if name in self.names else "False"


@pytest.fixture
def session(tmp_path):
    load_help.cache_clear()
    build_archive(tmp_path / "help.t32")

    def make(*names, failure=None):
        return SimpleNamespace(conn=Conn(names, failure), install=SimpleNamespace(sys_dir=tmp_path))

    yield make
    load_help.cache_clear()


def joined(lines):
    return "\n".join(lines)


def test_a_wrong_option_gets_the_format_block_and_no_prose(session):
    text = joined(explain_command(session("Register.view"), "Register.view /all", "unknown command"))
    assert "exists here" in text and "SpotLight | Stack" in text
    assert "Displays the core" not in text and "build 187291" not in text and "deprecated" not in text
    assert "Next heading" not in text


def test_a_page_without_a_format_block_points_to_the_manual_tool(session):
    text = joined(explain_command(session("Data.Set"), "Data.Set 1", "syntax error"))
    assert "exists here" in text and "Format" not in text and 'manual("Data.Set")' in text


def test_a_format_heading_with_nothing_under_it_is_no_format(session):
    text = joined(explain_command(session("List.Mix"), "List.Mix /x", "unknown command"))
    assert "exists here" in text and "Format" not in text and 'manual("List.Mix")' in text


def test_a_file_that_is_not_the_archive_is_refused(session, tmp_path):
    path = tmp_path / "help.t32"
    path.write_bytes(path.read_bytes().replace(b"help.t32", b"nope.t32", 1))
    assert load_help(path) is None


def test_a_name_that_does_not_exist_gets_candidates_that_do(session):
    text = joined(explain_command(session("Data.Set", "Data.Assemble"), "Data.Sett 1", "unknown command"))
    assert "Data.Sett does not exist" in text and "Data.Set (Modify memory)" in text


def test_a_candidate_trace32_denies_is_never_offered(session):
    text = joined(explain_command(session("Data.Set"), "Data.Assembl", "unknown command"))
    assert "Data.Assemble" not in text


def test_a_name_with_no_group_is_searched_by_its_words(session):
    text = joined(explain_command(session("Data.Set"), "Memory.Set 1 1", "unknown command"))
    assert "Memory.Set does not exist" in text and "Data.Set (Modify memory)" in text


def test_a_group_name_does_not_match_every_summary(session):
    text = joined(explain_function(session("TITLE", "Trace.RECORDS"), "no function 'TRACE.FIND.COUNT' exists"))
    assert "TITLE" not in text and "Trace.RECORDS" not in text


def test_a_short_word_is_matched_whole(session):
    text = joined(explain_function(session("TITLE"), "no function 'X.MAIN' exists"))
    assert "TITLE" not in text


def test_in_the_manual_but_not_here_says_so(session):
    text = joined(explain_command(session(), "TASK.MuTeX", "unknown command"))
    assert "in the manual" in text and "not available in this session" in text


@pytest.mark.parametrize("error", ["symbol not found", "debug port fail", None])
def test_other_errors_get_no_invented_advice(session, error):
    assert explain_command(session("Register.view"), "Register.view", error) == []


@pytest.mark.parametrize("line", ["", "B::", "%hex 1", "Register.view("])
def test_a_line_without_a_plain_name_is_not_guessed_at(session, line):
    conn = session("Register.view")
    assert explain_command(conn, line, "unknown command") == []
    assert conn.conn.asked == []


@pytest.mark.parametrize("failure", [T32Error("nope"), LinkError("gone")])
def test_a_lookup_that_fails_is_only_a_pointer_to_the_manuals(session, failure):
    assert joined(explain_command(session(failure=failure), "Data.Sett", "unknown command")).startswith("Next: ")


def test_a_function_that_does_not_exist_gets_candidates(session):
    text = joined(explain_function(session("Trace.RECORDS", "Trace.RECORD.TIME"), "no function 'TRACE.RECORD' exists"))
    assert "Trace.RECORDS() (Get number" in text


def test_an_address_without_a_class_says_so(session):
    assert "access class" in explain_function(session(), "wrong parameter type : expected  ADDRESS")[0]


def test_an_error_about_a_symbol_is_left_alone(session):
    assert explain_function(session(), "symbol not found") == []


def test_a_refused_window_that_exists_adds_nothing(session):
    assert explain_unknown_window(session("Data.Set"), "Data.Set") == []


def test_a_refused_window_that_does_not_exist_gets_candidates(session):
    assert "Data.Set" in joined(explain_unknown_window(session("Data.Set"), "Data.Sett 1"))


def test_a_failed_script_explains_its_line(session):
    s = session("Data.Set")
    errors = {"ERROR.CMDLINE()": "B::Data.Sett 1", "ERROR.MESSAGE()": "unknown command", "PRACTICE.SD()": "0"}
    s.fact = errors.get
    s.owned, s.script_op = False, 1
    s.conn.fnc = lambda e: errors.get(e, "True" if "(data.set)" in e.lower() else "False")
    s.conn.cmd = lambda command: None
    job = ScriptJob(None, "x", None)
    job.base = 0
    reply = job._finish(s, ON_ERROR)
    assert reply.is_error and "Data.Sett does not exist" in joined(reply.lines) and "Data.Set (Modify" in joined(reply.lines)


def test_a_command_whose_syntax_differs_by_target_gets_no_borrowed_format(session):
    text = joined(explain_command(session("SYStem.MemAccess"), "SYStem.MemAccess x", "syntax error"))
    assert "exists here" in text and "differs by target" in text and "NEXUS" not in text and "AHB" not in text


def test_a_function_called_a_command_keeps_the_command_page(session):
    text = joined(explain_command(session("PERF.METHOD"), "PERF.METHOD Foo", "syntax error"))
    assert "RealTime" in text


def test_a_bug_in_the_advice_never_reaches_the_reply(session, monkeypatch):
    monkeypatch.setattr(language, "load_help", lambda path: 1 / 0)
    s = session("Register.view")
    assert explain_command(s, "Register.view /all", "unknown command") == []
    assert explain_function(s, "no function 'X' exists") == []
    assert explain_unknown_window(s, "Nope.view") == []


def test_extension_errors_read_as_unknown_commands(session):
    assert "does not exist" in joined(explain_command(session(), "Memory.Set", "error while loading extension"))


def test_advice_stays_within_its_budget(session, monkeypatch):
    monkeypatch.setattr(language, "MAX_ADVICE", 130)
    lines = explain_command(session("Register.view"), "Register.view /all", "unknown command")
    assert sum(len(line) + 1 for line in lines[:-1]) <= 130


def test_an_option_list_that_stops_early_says_so(session, monkeypatch):
    monkeypatch.setattr(language, "MAX_ADVICE", 130)
    text = joined(explain_command(session("Register.view"), "Register.view /all", "unknown command"))
    assert "more options in the manual" in text and 'manual("Register.view")' in text


def test_a_format_block_cut_at_its_line_limit_says_so(session, monkeypatch):
    monkeypatch.setattr(manuals, "MAX_FORMAT_LINES", 2)
    text = joined(explain_command(session("Register.view"), "Register.view /all", "unknown command"))
    assert "more options in the manual" in text and "SpotLight" not in text


@pytest.mark.parametrize("damage", [b"", b"not an archive", b"help.t32" + b" " * 40 + b"x"])
def test_a_damaged_archive_means_no_advice_and_no_crash(tmp_path, damage):
    load_help.cache_clear()
    (tmp_path / "help.t32").write_bytes(damage)
    s = SimpleNamespace(conn=Conn(), install=SimpleNamespace(sys_dir=tmp_path))
    text = joined(explain_command(s, "Data.Sett", "unknown command"))
    assert "does not exist" in text and str(tmp_path / "pdf") in text
    load_help.cache_clear()


def test_a_missing_archive_points_to_the_pdfs(tmp_path):
    load_help.cache_clear()
    s = SimpleNamespace(conn=Conn(["Register.view"]), install=SimpleNamespace(sys_dir=tmp_path))
    assert "pdf" in joined(explain_command(s, "Register.view /x", "unknown command"))
    load_help.cache_clear()
