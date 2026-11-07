"""The manual tool. A wrong page for a right name is the failure that matters most here."""

import re

import pytest

from t32_mcp import manuals, prompts
from t32_mcp.manuals import CUT, Help, is_prose, load_help, look_up, manual_reply, search_words
from t32_mcp.powerview import Install
from test_language import build_archive

REAL = Install.locate().sys_dir / "help.t32"


@pytest.fixture
def sys_dir(tmp_path):
    load_help.cache_clear()
    build_archive(tmp_path / "help.t32")
    yield tmp_path
    load_help.cache_clear()


def test_a_name_gets_its_page(sys_dir):
    text, failed = look_up(sys_dir, "Register.view")
    assert not failed and text.startswith("Register.view: Display registers") and "SpotLight | Stack" in text
    assert "Displays the core registers" in text


def test_a_page_keeps_long_option_lines_as_syntax():
    assert not is_prose("Byte | Word | Long | Quad | Octa | TByte | PByte | HByte | SByte | Float | Double | Short | Half")
    assert is_prose("The specified formula is interpreted and the according values are displayed.")


def test_a_command_comes_before_a_function_of_the_same_name(sys_dir):
    text, _ = look_up(sys_dir, "PERF.METHOD")
    assert text.startswith("PERF.METHOD: Set the method") and "RealTime" in text and "Also a function: PERF.METHOD()" in text


def test_a_function_only_in_the_alphabetical_index_is_still_found(sys_dir):
    text, failed = look_up(sys_dir, "Data.Long")
    assert not failed and text.startswith("Data.Long(<address>)") and "Reads a long" in text


def test_a_function_whose_anchor_is_its_syntax_line_has_a_format(sys_dir):
    assert "Format:\n  STATE.RUN()" in look_up(sys_dir, "STATE.RUN")[0]


def test_a_command_with_pages_per_target_shows_none_until_told_which(sys_dir):
    text, _ = look_up(sys_dir, "SYStem.MemAccess")
    assert "differs by target" in text and "cortexm" in text and "sparc" in text
    assert "NEXUS" not in text and "AHB" not in text


def test_naming_the_target_opens_its_page(sys_dir):
    text, _ = look_up(sys_dir, "SYStem.MemAccess cortexm")
    assert "AHB" in text and "NEXUS" not in text and "[debugger_cortexm]" in text


def test_a_general_page_wins_over_specific_ones(sys_dir):
    text = look_up(sys_dir, "Go")[0]
    assert "from the general manual" in text and "only for sparc" not in text
    assert 'manual("Go <target>")' in text and "cortexm" in text


def test_words_that_start_with_a_group_name_are_still_words(sys_dir):
    assert look_up(sys_dir, "perf modify")[0] != look_up(sys_dir, "PERF")[0]


def test_every_word_of_a_question_gets_a_turn(sys_dir):
    text = look_up(sys_dir, "how do i display the profiling zzzzzz")[0]
    assert "PERF: Sample-based profiling" in text and "Register.view" in text


def test_a_rare_word_outranks_a_common_one(sys_dir):
    rows = look_up(sys_dir, "display mutex zzzzzz")[0].splitlines()
    assert rows[1].startswith("TASK.MuTeX"), rows


def test_a_syntax_search_stops_at_the_next_heading(sys_dir):
    assert "Format" not in look_up(sys_dir, "Short.Page")[0]


def test_a_whole_call_is_read_as_its_name(sys_dir):
    assert look_up(sys_dir, "Data.Long(D:0x20000000)")[0].startswith("Data.Long(<address>)")


def test_a_whole_command_line_is_read_as_its_name(sys_dir):
    assert look_up(sys_dir, "Register.view /all")[0].startswith("Register.view: Display")


def test_a_wrong_name_says_it_is_wrong(sys_dir):
    text, failed = look_up(sys_dir, "Trace.RECORD")
    assert not failed and text.startswith("Not in the manuals: Trace.RECORD.") and "Trace.RECORDS()" in text


def test_a_wrong_call_reads_as_a_wrong_name(sys_dir):
    assert look_up(sys_dir, "Trace.RECORD()")[0].startswith("Not in the manuals: Trace.RECORD.")


def test_a_typo_in_the_group_still_finds_the_name(sys_dir):
    assert "Trace.RECORDS()" in look_up(sys_dir, "Tarce.RECORDS")[0]


def test_a_group_lists_its_subcommands(sys_dir):
    text, _ = look_up(sys_dir, "PERF")
    assert "Subcommands: PERF.Arm, PERF.METHOD, PERF.Mode" in text and "PERF.Secret" not in text


def test_a_trailing_dot_is_the_same_name(sys_dir):
    assert "Subcommands" in look_up(sys_dir, "PERF.")[0]


def test_a_long_group_says_how_many_it_left_out(sys_dir, monkeypatch):
    monkeypatch.setattr(manuals, "MAX_CHILDREN", 1)
    assert "first 1 of 3" in look_up(sys_dir, "PERF")[0]


def test_a_page_shows_examples_of_itself(sys_dir):
    text = look_up(sys_dir, "Break.Set")[0]
    assert "Examples:" in text and "Break.Set main" in text and "Data.dump 0" not in text


def test_words_find_candidates(sys_dir):
    text, failed = look_up(sys_dir, "modify memory")
    assert not failed and text.startswith("Data.Set: Modify memory")


def test_word_forms_match(sys_dir):
    assert "Trace.RECORDS" in look_up(sys_dir, "recording")[0]


def test_filler_words_are_ignored(sys_dir):
    assert look_up(sys_dir, "how do I modify the memory")[0].startswith("Data.Set: Modify memory")


def test_a_partial_match_is_labelled(sys_dir):
    text, failed = look_up(sys_dir, "modify memory zzzzzz")
    assert not failed and text.startswith("Nothing has all the words") and "Data.Set: Modify memory" in text


def test_an_agents_word_finds_the_manuals_word(sys_dir):
    assert "Break.Set" in look_up(sys_dir, "watchpoint")[0]


def test_internal_entries_and_trace_method_copies_are_left_out(sys_dir):
    text = look_up(sys_dir, "trace statistic")[0].split("Sections")[0]
    assert "Hidden.Thing" not in look_up(sys_dir, "secret")[0] and "<trace>" not in text and "Analyzer.STATistic" not in text
    assert "Trace.STATistic" in text


@pytest.mark.parametrize("query", ["freertos", "FreeRTOS", "rtos_freertos", "RTOS FreeRTOS", "FreeRTOS task list mutex lock"])
def test_an_rtos_name_shows_its_setup_and_commands(sys_dir, query):
    text, failed = look_up(sys_dir, query)
    assert not failed and "TASK.CONFIG ~~/demo/<arch>/kernel/freertos/freertos.t32" in text
    assert "TASK.Semaphore: Display semaphores" in text and "TASK.Queue" in text


def test_a_name_asked_about_for_an_rtos_gets_both_pages(sys_dir):
    text, _ = look_up(sys_dir, "TASK.Queue FreeRTOS setup")
    assert text.startswith("TASK.Queue: Display queues") and "freertos.t32" in text and "TASK.Semaphore" in text


@pytest.mark.parametrize("query", ["TASK.MuTeX freertos", "TASK.MuTeX rtos_freertos"])
def test_a_name_the_rtos_does_not_have_says_so(sys_dir, query):
    text, _ = look_up(sys_dir, query)
    assert text.startswith("TASK.MuTeX is not in rtos_freertos.") and "TASK.Semaphore" in text


def test_a_target_note_does_not_suggest_a_target_nobody_asked_for(sys_dir):
    text = look_up(sys_dir, "SYStem.MemAccess")[0]
    assert 'manual("SYStem.MemAccess <target>")' in text


def test_a_section_found_by_its_text_can_be_opened(sys_dir):
    text, _ = look_up(sys_dir, "semihosting")
    assert "Target Console [m]" in text
    opened, failed = look_up(sys_dir, "Target Console [m]")
    assert not failed and "semihosting works over the debug port" in opened


def test_release_notes_are_not_searched(sys_dir):
    assert look_up(sys_dir, "zebrafish")[1]


def test_nothing_found_says_so(sys_dir):
    text, failed = look_up(sys_dir, "zzzzzz")
    assert failed and text.startswith("Failed:") and "Next:" in text
    assert look_up(sys_dir, "   ")[1]


def test_a_query_in_another_language_is_told_the_manuals_are_english(sys_dir):
    text, failed = look_up(sys_dir, "断点")
    assert failed and "The manuals are in English" in text


def test_a_cut_format_block_says_so(sys_dir, monkeypatch):
    monkeypatch.setattr(manuals, "PAGE_FORMAT_LINES", 2)
    assert CUT in look_up(sys_dir, "Register.view")[0]


def test_without_the_archive_it_points_to_the_pdfs(tmp_path):
    load_help.cache_clear()
    text, failed = look_up(tmp_path, "Break.Set")
    assert failed and str(tmp_path / "pdf") in text
    load_help.cache_clear()


def test_case_does_not_matter_on_names(sys_dir):
    assert look_up(sys_dir, "register.VIEW")[0].startswith("Register.view")


def test_stems_of_short_words_are_left_alone():
    assert search_words("the ice") == ["ice"]


@pytest.fixture(scope="module")
def library():
    return Help(REAL)


def names_in_prompts() -> set[str]:
    text = " ".join(str(v) for k, v in vars(prompts).items() if k.isupper())
    found = set(re.findall(r"\b[A-Za-z]+(?:\.[A-Za-z]+)+", text))
    return {n for n in found if re.search(r"[A-Z]", n) and not n.endswith((".cmm", ".pdf", ".t32", ".exe"))
            and n not in ("OS.Command", "D.S")}


@pytest.mark.skipif(not REAL.exists(), reason="needs TRACE32 installed")
class TestRealManuals:
    """Hand-checked answers. Rerun on a new TRACE32: the format of help.t32 is not published."""

    @pytest.mark.parametrize("name", sorted(names_in_prompts()))
    def test_every_name_the_prompts_teach_has_a_page(self, library, name):
        text, failed = manual_reply(library, name)
        assert not failed and not text.startswith("Not in the manuals"), text

    @pytest.mark.parametrize("query, part", [
        ("Data.Long", "Data.Long("), ("STATE.RUN", "STATE.RUN()"), ("Data.Long(D:0x20000000)", "Data.Long("),
        ("PERF.METHOD", "RealTime"), ("TASK.CONFIG", "<os_awareness_file>"), ("SYStem.CPU", "<cpu>"),
        ("Break.Set", "CONDition"), ("Break.Set", "COUNT <value>"), ("Data.dump", "Track"), ("Var.Break.Set", "<expression>"),
        ("Register.view", "SpotLight"), ("Trace.List", "<record_range>"), ("T.FA", "FindAll"), ("Var.VALUE", "Var.VALUE("),
        ("sYmbol.NAME", "sYmbol.NAME("), ("Trace.RECORDS", "Trace.RECORDS()"), ("Trace.STATistic.Sort", "<sort"),
        ("PERF", "PERF.METHOD"), ("TASK.", "TASK.CONFIG"), ("SYStem.MemAccess cortexm", "DAP"), ("Break.Set 0x1 /Write", "CONDition"),
        ("TASK.Semaphore freertos", "[rtos_freertos]"), ("freertos", "TASK.Semaphore"), ("freertos", "freertos.t32 (~~"),
        ("FreeRTOS mutex", "TASK.Semaphore"), ("TASK.CONFIG FreeRTOS setup", "freertos.t32 (~~"), ("TASK.MuTeX rtos_freertos", "is not in rtos_freertos"), ("SYStem.Mode", "manual(\"SYStem.Mode <target>\")"),
        ("SYStem.MemAccess", "<cpu_specific>"), ("SYStem.MemAccess", "manual(\"SYStem.MemAccess <target>\")"),
    ])
    def test_the_right_page(self, library, query, part):
        text, failed = manual_reply(library, query)
        assert not failed and part in text, text

    @pytest.mark.parametrize("query, wrong", [("SYStem.MemAccess", "NEXUS"), ("SYStem.MemAccess cortexm", "NEXUS"),
                                              ("PERF.METHOD", "Returns the recording method"), ("Break", "Only with")])
    def test_not_the_wrong_page(self, library, query, wrong):
        assert wrong not in manual_reply(library, query)[0]

    @pytest.mark.parametrize("query, top3", [
        ("profile", "PERF"), ("sampling", "PERF"), ("watchpoint", "Break"), ("memory compare", "Data.ComPare"),
        ("stack usage", "TASK.STacK"), ("flash programming", "FLASH."), ("symbol at address", "sYmbol."),
        ("how do I profile without stopping the target", "PERF"), ("coverage", "COVerage"), ("reset target", "SYStem."),
    ])
    def test_the_answer_is_near_the_top(self, library, query, top3):
        text = manual_reply(library, query)[0]
        assert any(top3 in row.split(":", 1)[0] for row in text.splitlines()[:11]), text

    @pytest.mark.parametrize("query", ["Tarce.List", "d.dump", "Trace.RECORD"])
    def test_a_wrong_name_never_reads_as_a_right_one(self, library, query):
        assert manual_reply(library, query)[0].startswith("Not in the manuals")

    def test_a_group_lists_only_what_every_target_has(self, library):
        text = manual_reply(library, "TASK.")[0]
        assert "TASK.CONFIG" in text and "first 40" not in text

    def test_a_long_page_stays_readable(self, library):
        text = manual_reply(library, "Trace.STATistic.Func")[0]
        assert "Sort" in text and len(text) < 3000

    def test_release_notes_are_not_offered_as_sections(self, library):
        assert not any("release" in s for s in library.mentions("semihosting"))
