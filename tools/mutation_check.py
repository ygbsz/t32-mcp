"""A test written after the fix proves nothing until it has failed.

Each entry breaks one line of the source, runs the test that should catch it, and restores
the file byte for byte. Anchors carry enough context to hit exactly one place.

    python tools/mutation_check.py          offline tests only
    python tools/mutation_check.py --live   also the ones that need PowerView
    python tools/mutation_check.py --live --only=TEXT   only anchors containing TEXT
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "t32_mcp"

# file, old, new, test selection (None: all offline tests), needs PowerView
MUTATIONS = [
    ("session.py", 'self.conn.cmd("ERROR.RESet")\n            return None', "return None", None, False),
    ("session.py", 'if self.owned:\n            self.conn.cmd("ERROR.RESet")', 'if True:\n            self.conn.cmd("ERROR.RESet")', None, False),
    ("session.py", "if message.type == 0 or message == before:", "if message.type == 0:", None, False),
    ("session.py", "if message.type == 0 or message == before:", "if message == before:", None, False),
    ("session.py", "if occurred or (message and message.is_error):", "if message and message.is_error:", None, False),
    ("session.py", "if message and message.is_warning:\n            return LineResult(line, True, warning=message.text)", "if False:\n            pass", None, False),
    ("views.py", "if not DISPLAY_WORDS.intersection(words) or CHANGE_WORDS.intersection(words):", "if not DISPLAY_WORDS.intersection(words):", None, False),
    ("views.py", "if not DISPLAY_WORDS.intersection(words) or CHANGE_WORDS.intersection(words):", "if CHANGE_WORDS.intersection(words):", None, False),
    ("views.py", 'if any(c in window for c in "\\r\\n"):', "if False:", None, False),
    ("views.py", "if name in KNOWN_HANGS:", "if False:", None, False),
    ("views.py", "if name in DISPLAY_EXCEPTIONS:", "if False:", None, False),
    ("views.py", "software hardware environment findall", "software hardware environment", None, False),
    ("views.py", "advice = explain_command(s, self.window, exc.text)", "advice = None", "test_a_wrong_option", True),
    ("views.py", "refusal.lines.extend(explain_unknown_window(s, self.window))", "pass", "test_a_wrong_name_in_view", True),
    ("scripts.py", "reply.lines.extend(explain_command(s, self.failed_command, self.failed_error))", "pass",
     "test_a_script_error_is_explained", True),
    ("language.py", "if e.bare.lower() not in skip and available(s, e.bare, e.kind):", "if e.bare.lower() not in skip:",
     None, False),
    ("language.py", "if exists is None:\n        return [manuals_next(s, name)]", "if False:\n        return []", None, False),
    ("language.py", "if not error or not any(text in error for text in COMMAND_ERRORS):", "if not error:", None, False),
    ("manuals.py", "words = parts[1:] if any(key.startswith(parts[0] + \".\") for key in self.names) else parts",
     "words = parts", None, False),
    ("language.py", "if used > MAX_ADVICE - len(CUT):", "if False:", None, False),
    ("language.py", "if entry and picked.differs_by_target:", "if False:", None, False),
    ("manuals.py", "if not summary.strip() and not (alias and \"(\" in name):", "if not summary.strip():", None, False),
    ("manuals.py", "if e.name.startswith(\"<\") or INTERNAL in e.summary.lower():", "if False:", None, False),
    ("manuals.py", "distinct.setdefault((e.bare.lower().rsplit(\".\", 1)[-1], e.summary.lower()), e)", "distinct.setdefault(e.bare, e)", None, False),
    ("manuals.py", "return not self.general, self.scope != \"*\", self.source", "return self.scope != \"*\", self.source", None, False),
    ("manuals.py", "return bool(self.command) and not self.command.general and len(self.targets) > 1", "return False", None, False),
    ("manuals.py", "commands = exact or [e for e in commands if target in e.source] or commands", "pass", None, False),
    ("manuals.py", "if match.group(1) == \"H\" and number > 0:\n                break", "pass", None, False),
    ("manuals.py", "return \"|\" not in row and ((row.endswith(\".\") and words >= 4) or words > 12)", "return words > 12 or row.endswith(\".\")", None, False),
    ("manuals.py", "if entry.kind == \"function\" and rows and rows[0][0] == \"X\":", "if False:", None, False),
    ("manuals.py", "if picked.command and picked.function:", "if False:", None, False),
    ("manuals.py", "if word in STOP_WORDS or len(word) < 3:", "if len(word) < 3:", None, False),
    ("manuals.py", "words.append(f\"(?:{word_stem(word)}|{also})\" if also else re.escape(word_stem(word)))", "words.append(re.escape(word))", None, False),
    ("manuals.py", "and not m.startswith(\"release\")", "", None, False),
    ("manuals.py", "english = \"\" if query.isascii() else \" The manuals are in English.\"", "english = \"\"", None, False),
    ("manuals.py", "name = head.split(\"(\", 1)[0].rstrip(\".\")", "name = head", None, False),
    ("manuals.py", "if \".\" in name and NAME.match(name):  # a name got wrong", "if False:  # a name got wrong", None, False),
    ("manuals.py", "if section:\n        lines = library.section(*section.groups())", "if False:\n        lines = library.section(*section.groups())", None, False),
    ("manuals.py", "if len(children) > MAX_CHILDREN else \"\"", "if False else \"\"", None, False),
    ("manuals.py", "return sorted((general or everywhere).values(), key=str.lower)", "return sorted(everywhere.values(), key=str.lower)", None, False),
    ("manuals.py", "for entry in (e for round_ in zip_longest(*turns) for e in round_ if e):", "for entry in (e for turn in turns for e in turn):", None, False),
    ("manuals.py", "written_as_a_name = not rest or", "written_as_a_name = True or", None, False),
    ("manuals.py", "if rtos and (picked.command or picked.function):", "if False:", None, False),
    ("manuals.py", "+ (library.rtos_overview(rtos) if rtos else [])", "", None, False),
    ("manuals.py", "rtos = library.rtos_manual(query)\n    if rtos:", "rtos = None\n    if rtos:", None, False),
    ("manuals.py", "re.fullmatch(r\"\\w+\", rest[0])", "rest[0].isalnum()", None, False),
    ("manuals.py", "if setup:\n            lines.append", "if False:\n            lines.append", None, False),
    ("jobs.py", "if any(NO_CODE.search(line) for line in lines):", "if False:", None, False),
    ("jobs.py", "and s.target_state() == \"running\":\n        notes.append", "and True:\n        notes.append", None, False),
    ("jobs.py", "if self.action == \"go\" and not self.until and s.conn.target_state() == \"running\":", "if False:", None, False),
    ("manuals.py", "if entry.general and len(picked.targets) > 1:", "if False:", None, False),
    ("language.py", "        except Exception:\n            return []\n", "        except ZeroDivisionError.__base__.__base__ if False else KeyError:\n            return []\n", None, False),
    ("engine.py", "reply = job.start(s)\n        except LinkError as exc:\n            self._died(op, exc)\n            return\n        except T32Error as exc:\n            reply = failed(exc.text, label=job.label)\n        except Exception as exc:\n            reply = self._our_bug(op, exc)\n",
     "reply = job.start(s)\n        except LinkError as exc:\n            self._died(op, exc)\n            return\n        except T32Error as exc:\n            reply = failed(exc.text, label=job.label)\n", None, False),
    ("engine.py", "else job.poll(s)\n        except LinkError as exc:\n            self._died(op, exc)\n            return\n        except T32Error as exc:\n            reply = failed(exc.text, label=job.label)\n        except Exception as exc:\n            reply = self._our_bug(op, exc)\n",
     "else job.poll(s)\n        except LinkError as exc:\n            self._died(op, exc)\n            return\n        except T32Error as exc:\n            reply = failed(exc.text, label=job.label)\n", None, False),
    ("engine.py", "s.script_op = None\n        return failed(f\"internal error", "return failed(f\"internal error", None, False),
    ("language.py", "if kept[-1] != CUT:\n                kept.append(CUT)", "pass", None, False),
    ("manuals.py", "lines.append(CUT)\n                break", "break", None, False),
    ("manuals.py", "return lines if len(lines) > 1 else []", "return lines", None, False),
    ("manuals.py", "if first[0] != \"help.t32\":", "if False:", None, False),
    ("jobs.py", 'self.current = f"line {number} of {total} ({line[:60]})"', "pass", None, False),
    ("jobs.py", "*notes, *explain_command(s, line, result.error))", "*notes)", None, False),
    ("jobs.py", "advice.extend(a for a in explain_function(s, exc.text) if a not in advice)", "pass", None, False),
    ("engine.py", "name = job.doing\n", "name = type(job).__name__\n", None, False),
    ("views.py", 'if message and message.is_warning:\n            reply.tail.append(warning(message.text))', "if False:\n            pass", "test_view_keeps_the_warning", True),
    ("views.py", 'if printer:\n                s.conn.cmd(f\'PRinTer.FILE "{printer}"\')', "pass", "test_attach_to", True),
    ("scripts.py", "if practice_state == 2:\n        return DIALOG", "if False:\n        return DIALOG", None, False),
    ("scripts.py", "return ON_ERROR if occurred else PAUSED", "return PAUSED", None, False),
    ("scripts.py", "return DONE_WITH_ERRORS if occurred else DONE", "return DONE", None, False),
    ("scripts.py", "if depth <= base_depth:", "if depth <= 0:", None, False),
    ("scripts.py", "    if depth <= base_depth:  # state 2 outlives", "    if practice_state != 2 and depth <= base_depth:  # state 2 outlives", None, False),
    ("scripts.py", "s.instance.close_dialogs()\n            time.sleep(DIALOG_CLOSE_S)", "time.sleep(DIALOG_CLOSE_S)", "test_script_with_a_dialog", True),
    ("scripts.py", 'if int(s.conn.fnc("PRACTICE.SD()")) > self.base:\n            s.conn.cmd("END")', "pass", "test_script_that_stops_on_an_error", True),
    ("scripts.py", "if state in (ON_ERROR, PAUSED, DIALOG):", "if state in (ON_ERROR, PAUSED):", "test_script_with_a_dialog", True),
    ("jobs.py", '_REFUSED = [(re.compile(r"^\\s*DO(\\s|$)", re.I)', '_REFUSED = [(re.compile(r"^\\s*DO(\\s|$)")', None, False),
    ("jobs.py", "if not result.ok and self.stop_on_error:", "if False:", None, False),
    ("jobs.py", 'return ok(f"Stopped waiting after {limit:.0f} s. The target is still running; nothing stopped it.",', 's.conn.cmd("Break")\n        return ok(f"Stopped waiting after {limit:.0f} s. The target is still running; nothing stopped it.",', None, False),
    ("jobs.py", "if not same:\n                    reply.tail.append", "if False:\n                    reply.tail.append", "test_run_checks_the_requested_stop_and_can_step", False),
    ("engine.py", "if op is not None and op.state == \"blocking\" and time.monotonic() > op.kill_at:", "if False:", None, False),
    ("engine.py", "if s.owned:\n            s.instance.kill()  # the worker wakes up", "if True:\n            s.instance.kill()  # the worker wakes up", None, False),
    ("engine.py", 'if op.state == "done":\n            return  # a late result of an abandoned call', "if False:\n            return", None, False),
    ("engine.py", 'def _continue(self, op: Operation, reply: Reply | None) -> None:\n        if op.state == "done":\n            return', "def _continue(self, op: Operation, reply: Reply | None) -> None:", None, False),
    ("engine.py", 'if job.tier in (CONTROL, RAW) and s.access == "observe":', "if False:", None, False),
    ("engine.py", "if s.dead:\n            self._finish(op, failed(f\"not run: {s.dead}\"", "if False:\n            self._finish(op, failed(f\"not run: {s.dead}\"", None, False),
    ("engine.py", "if s.serial:\n                self._lost_probes.add(s.serial)\n            else:\n                self._lost_simulator = True\n        name = job.doing", "name = job.label", None, False),
    ("engine.py", "if not ours and not takeover:", "if False:", "test_probe_held_by_a_stranger", True),
    ("engine.py", "ours = device.serial in self._lost_probes or self._lost_simulator", "ours = False", "test_blocked_call_is_killed_and_recovered", True),
]


def run_tests(selection: str | None, live: bool) -> bool:
    """True when the tests pass, which here means the mutation survived."""
    shutil.rmtree(SRC / "__pycache__", ignore_errors=True)  # same-second edits fool the pyc timestamp check
    args = [sys.executable, "-m", "pytest", "-q", "-x", "--tb=no", "-p", "no:cacheprovider"]
    args += ["-m", "hw" if live else "not hw"]
    if selection:
        args += ["-k", selection]
    result = subprocess.run(args, cwd=ROOT, capture_output=True, timeout=240)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pytest exited with {result.returncode} for {args}"
                           f"\n{(result.stdout + result.stderr).decode(errors='replace')}")
    return result.returncode == 0


def main() -> int:
    with_live = "--live" in sys.argv
    only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
    if not run_tests(None, False):
        print("The offline tests fail without any mutation, so every mutant would look killed. Fix them first.")
        return 2
    survived = []
    tried = 0
    for name, old, new, selection, live in MUTATIONS:
        if (live and not with_live) or (only and only not in old):
            continue
        path = SRC / name
        original = path.read_bytes()  # bytes: never touch line endings
        text = original.decode("utf-8")
        if text.count(old) != 1:
            print(f"BAD ANCHOR ({text.count(old)} hits): {name}: {old[:60]!r}")
            survived.append(old)
            continue
        tried += 1
        try:
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            passed = run_tests(selection, live)
        finally:
            path.write_bytes(original)
            shutil.rmtree(SRC / "__pycache__", ignore_errors=True)
        print(f"{'SURVIVED' if passed else 'killed  '} {name}: {old.splitlines()[0][:70]}", flush=True)
        if passed:
            survived.append(old)
    print(f"{tried - len(survived)}/{tried} killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
