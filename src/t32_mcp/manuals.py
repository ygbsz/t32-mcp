"""The TRACE32 manuals as data: help.t32, the online help as text.

The archive has no published format, so any surprise in it means no answer, never a wrong one.
The order of the rules below matters more than the search: a wrong page for a correct name is
worse than a poor list of candidates, because an agent has no reason to doubt it.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import zip_longest
from pathlib import Path

HEADER_BYTES = 128
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")
INDEX_ROW = re.compile(r'^"([^"]*)","([^"]*)","([^"]*)","(G\d+) *","([^"]*)","(\d+)"')
ALIAS = re.compile(r"^(\S+) \[([^\]]+)\]$")
STX_ROW = re.compile(r"^G\d+\s+(\S)\s(.*)$")
SKIPPED_ROW = re.compile(r"^\[build |\(deprecated\) *$")
SECTION = re.compile(r"^(.+) \[([a-z0-9_]+)\]$")
ENDING = re.compile(r"(ing|ers?|ed|es|s|e|y)$")
CUT = "  ... (more options in the manual)"
INTERNAL = "internal usage"
SYNTAX_MARKERS = ("Format:", "Syntax:")
SYNTAX_SEARCH_ROWS = 4
MAX_FORMAT_LINES = 10
PAGE_FORMAT_LINES = 36
PAGE_BYTES = 8000
MAX_PROSE = 320
MAX_SUMMARY = 68
MAX_MATCHES = 10
MAX_SECTIONS = 6
MAX_SECTION_ROWS = 24
MAX_CHILDREN = 40
MAX_EXAMPLES = 3
MAX_TARGETS = 8
STOP_WORDS = frozenset("a an and are at by can do does for from how i in into is it of on or the to what when where "
                       "which while with without you".split())
# The manuals' word for a word agents use. Both are searched, so names with the agent's word still match.
ALSO_SEARCH = {"watchpoint": "breakpoint", "watchpoints": "breakpoint", "pmu": "benchmark"}


def is_prose(row: str) -> bool:
    """A sentence of the manual rather than a line of syntax. Long option lists have bars and no full stop."""
    words = len(row.split())
    return "|" not in row and ((row.endswith(".") and words >= 4) or words > 12)


def key_of(name: str) -> str:
    return name.strip().split("(", 1)[0].rstrip(".").lower()


@dataclass(frozen=True)
class Entry:
    name: str  # spelled as the manual spells it: Trace.STATistic.Func, Trace.RECORDS()
    summary: str
    kind: str  # command or function
    anchor: str
    scope: str
    source: str  # the manual it is in, without extension: general_ref_b, debugger_cortexm

    @property
    def bare(self) -> str:
        return self.name.split("(", 1)[0]

    @property
    def general(self) -> bool:
        return self.source.startswith(("general_", "ide_"))

    @property
    def rank(self) -> tuple:
        return not self.general, self.scope != "*", self.source

    @property
    def shown(self) -> str:
        return f"{self.name} ({self.summary[:48]})"

    @property
    def row(self) -> str:
        where = "" if self.general else f" [{self.source}]"
        return f"{self.name}: {self.summary[:MAX_SUMMARY]}{where}"


@dataclass(frozen=True)
class Pick:
    """What one name means, across every manual that has a page for it."""

    command: Entry | None
    function: Entry | None
    pages: tuple[Entry, ...]  # the command's pages, best first

    @property
    def targets(self) -> list[str]:
        return sorted({e.source.removeprefix("debugger_") for e in self.pages if not e.general})

    @property
    def differs_by_target(self) -> bool:
        """No general page and several specific ones: choosing one would be guessing the architecture."""
        return bool(self.command) and not self.command.general and len(self.targets) > 1


class Help:
    """C:\\T32\\help.t32: 128-byte directory rows, then the members."""

    def __init__(self, path: Path):
        self.data = path.read_bytes()
        first = self.data[:HEADER_BYTES].decode("latin-1").split()
        if first[0] != "help.t32":
            raise ValueError("not a help archive")
        self.members: dict[str, tuple[int, int]] = {}
        for number in range(1, int(first[3]) + 1):
            fields = self.data[number * HEADER_BYTES:(number + 1) * HEADER_BYTES].decode("latin-1").split()
            self.members[fields[0]] = (int(fields[1]), int(fields[2]))
        self.manuals = [row.split(",", 1)[0].strip('"').removesuffix(".pdf")
                        for row in self.member("_filelist.txt").splitlines()]
        self.names: dict[str, list[Entry]] = {}
        for row in self.member("_index.txt").splitlines():
            self._read_row(row)
        if not self.names:
            raise ValueError("empty index")
        self.catalog = self._build_catalog()

    def member(self, name: str) -> str:
        offset, size = self.members[name]
        return self.data[offset:offset + size].decode("latin-1")

    def _read_row(self, row: str) -> None:
        match = INDEX_ROW.match(row)
        if not match:
            return
        title, summary, kind, anchor, scope, manual = match.groups()
        kind = kind.replace("!", "").strip()
        if kind not in ("command", "function"):
            return
        alias = ALIAS.match(title)
        key, name = alias.groups() if alias else (title, title)
        if not summary.strip() and not (alias and "(" in name):
            return  # a row of the alphabetical index, written for people: "Nesting ... [Trace.STATistic.Func]"
        entry = Entry(name, summary.strip(), kind, anchor, scope, self.manuals[int(manual)])
        for spelling in {key_of(key), key_of(name)}:
            found = self.names.setdefault(spelling, [])
            if entry not in found:
                found.append(entry)

    def _build_catalog(self) -> list[Entry]:
        """One entry for each thing, without the copies each trace method has of every command."""
        best: dict[str, Entry] = {}
        for entries in self.names.values():
            for e in sorted(entries, key=lambda x: x.rank):
                if e.name.startswith("<") or INTERNAL in e.summary.lower():
                    continue
                best.setdefault(e.bare.lower(), e)
        distinct: dict[tuple, Entry] = {}
        for e in sorted(best.values(), key=lambda x: (not x.bare.startswith("Trace."), len(x.bare), x.bare)):
            distinct.setdefault((e.bare.lower().rsplit(".", 1)[-1], e.summary.lower()), e)
        return list(distinct.values())

    def pick(self, name: str, target: str = "") -> Pick:
        """The page for a name. A command before a function of that name, a general manual before a specific one."""
        entries = self.names.get(key_of(name), [])
        commands = sorted((e for e in entries if e.kind == "command"), key=lambda e: e.rank)
        functions = sorted((e for e in entries if e.kind == "function"), key=lambda e: e.rank)
        if target:
            exact = [e for e in commands if e.source.removeprefix("debugger_") == target]
            commands = exact or [e for e in commands if target in e.source] or commands
        return Pick(commands[0] if commands else None, functions[0] if functions else None, tuple(commands))

    def find(self, name: str, kind: str = "command") -> Entry | None:
        picked = self.pick(name)
        return picked.command if kind == "command" else picked.function

    def _rows_from(self, entry: Entry) -> list[tuple[str, str]]:
        """Style and text of the rows under an anchor, up to the next heading."""
        text = self.member(entry.source + ".stx")
        anchor = re.search(rf"^{entry.anchor}\s", text, re.M)
        if not anchor:
            return []
        rows: list[tuple[str, str]] = []
        for number, row in enumerate(text[anchor.start():anchor.start() + PAGE_BYTES].splitlines()):
            match = STX_ROW.match(row)
            if not match:
                break
            if match.group(1) == "H" and number > 0:
                break
            rows.append((match.group(1), match.group(2).strip()))
        return rows

    def format_of(self, entry: Entry, limit: int | None = None) -> list[str]:
        """The syntax block, or nothing when the page does not look as expected. Cut means it says so."""
        limit = limit or MAX_FORMAT_LINES
        rows = self._rows_from(entry)
        start = next((i for i, (_, text) in enumerate(rows[:SYNTAX_SEARCH_ROWS + 1]) if text in SYNTAX_MARKERS), None)
        if start is None:
            if entry.kind == "function" and rows and rows[0][0] == "X":  # some anchors point straight at the syntax
                return ["Format:", f"  {rows[0][1]}"]
            return []
        lines = ["Format:"]
        for style, text in rows[start + 1:]:
            if style != "X" or is_prose(text):
                break
            if len(lines) > limit:
                lines.append(CUT)
                break
            if not SKIPPED_ROW.search(text):
                lines.append(f"  {text}")
        return lines if len(lines) > 1 else []

    def about(self, entry: Entry) -> str:
        """The first sentence under a heading."""
        return next((text[:MAX_PROSE] for style, text in self._rows_from(entry)[1:] if style == "X" and is_prose(text)), "")

    def examples(self, entry: Entry) -> list[str]:
        """Lines of the page's examples that use this command."""
        wanted = entry.bare.lower().rsplit(".", 1)[-1]
        rows = [text.split(";", 1)[0].strip() for style, text in self._rows_from(entry) if style == "E"]
        return [row[:90] for row in rows if wanted in row.lower()][:MAX_EXAMPLES]

    def children(self, name: str) -> list[str]:
        """The commands one level below a command group."""
        prefix = key_of(name) + "."
        everywhere, general = {}, {}
        for key, entries in self.names.items():
            if key.startswith(prefix) and "." not in key[len(prefix):]:
                for e in entries:
                    if e.kind == "command" and e.bare.lower() == key and INTERNAL not in e.summary.lower():
                        everywhere.setdefault(key, e.bare)
                        if e.general:
                            general.setdefault(key, e.bare)
        return sorted((general or everywhere).values(), key=str.lower)  # TASK has hundreds, one set per RTOS

    def close_to(self, name: str, kind: str) -> list[Entry]:
        """Names that look like this one, nearest first. Looks only: Data.Assemble is close to Data.Disassemble."""
        wanted = key_of(name)
        head = wanted.split(".", 1)[0] + "."
        everything = {e.bare.lower() for entries in self.names.values() for e in entries if e.kind == kind}
        group = [n for n in everything if n.startswith(head)]
        names = difflib.get_close_matches(wanted, group or sorted(everything), n=6, cutoff=0.6)
        return [e for n in names if (e := self.find(n, kind))]

    def described_as(self, name: str, kind: str) -> list[Entry]:
        """Entries whose summary uses the words of a failed name, sharing its last word first."""
        parts = key_of(name).split(".")
        # The name of a real command group says nothing: "trace" would match every summary about tracing.
        words = parts[1:] if any(key.startswith(parts[0] + ".") for key in self.names) else parts
        stems = [word_stem(word) for word in words if len(word) >= 5]
        if not stems:
            return []
        wanted = re.compile(r"\b(" + "|".join(map(re.escape, stems)) + ")")
        found = [e for e in self.catalog if e.kind == kind and wanted.search(e.summary.lower())]
        return sorted(found, key=lambda e: (e.bare.lower().rsplit(".", 1)[-1] != parts[-1], e.rank, len(e.bare)))[:12]

    def search(self, query: str) -> tuple[list[Entry], list[Entry]]:
        """What the words describe: entries with all of them, then entries with most of them."""
        words = search_words(query)
        rarity = {word: _rarity(self.catalog, word) for word in words}
        scored = []
        for e in self.catalog:
            hits = {word: hit for word in words if (hit := _hit(e, word))}
            if hits:
                weight = sum(hit * rarity[word] for word, hit in hits.items()) + (e.scope == "*") * 0.5
                scored.append((-weight, not e.general, len(e.bare), e.bare, e, hits))
        scored.sort(key=lambda row: row[:4])
        full = [row[4] for row in scored if len(row[5]) == len(words)][:MAX_MATCHES]
        # The words take turns, the rarest first, or two of them fill the list and the third is never seen.
        turns = [[row[4] for row in scored if len(row[5]) < len(words) and word in row[5]]
                 for word in sorted(words, key=lambda w: -rarity[w])]
        partial: list[Entry] = []
        for entry in (e for round_ in zip_longest(*turns) for e in round_ if e):
            if entry not in partial:
                partial.append(entry)
            if len(partial) >= MAX_MATCHES - len(full):
                break
        return full, partial

    def mentions(self, query: str) -> list[str]:
        """Sections with all the words on one line, for what no summary names. Each one can be opened by its label."""
        patterns = [re.compile(rf"\b{word}", re.I) for word in search_words(query)]
        if not patterns:
            return []
        hits: dict[str, int] = {}
        manuals = sorted((m for m in self.members if m.endswith(".stx") and not m.startswith("release")),
                         key=lambda m: not m.startswith(("general_", "ide_")))
        for manual in manuals:
            heading = ""
            for row in self.member(manual).splitlines():
                match = STX_ROW.match(row)
                if not match:
                    continue
                if match.group(1) == "H":
                    heading = match.group(2).strip()
                elif heading and all(p.search(match.group(2)) for p in patterns):
                    label = f"{heading} [{manual.removesuffix('.stx')}]"
                    hits[label] = hits.get(label, 0) + 1
        return sorted(hits, key=lambda label: (-hits[label], label))[:MAX_SECTIONS]

    def rtos_manual(self, query: str) -> str | None:
        """The RTOS manual a query names, by its full name or just the RTOS: rtos_freertos, freertos."""
        for word in re.findall(r"\w+", query.lower()):
            stem = word if word.startswith("rtos_") else f"rtos_{word}"
            if stem + ".stx" in self.members:
                return stem
        return None

    def rtos_overview(self, manual: str) -> list[str]:
        """What an RTOS adds: how to set it up, and its commands. TASK alone cannot say, it covers them all."""
        rows = [m.group(2).strip() for row in self.member(manual + ".stx").splitlines() if (m := STX_ROW.match(row))]
        setups = [row for row in rows if row.startswith("TASK.CONFIG ") and ".t32" in row]
        setup = next((row for row in setups if row.endswith(".t32")), setups[0] if setups else None)  # the one that needs no addresses
        entries = {e.bare: e for found in self.names.values() for e in found if e.source == manual and e.kind == "command"}
        lines = [f"{manual}:"]
        if setup:
            lines.append(f"Set up: {setup} (~~ is the TRACE32 directory, <arch> is arm for Arm cores)")
        return lines + [e.row.removesuffix(f" [{manual}]") for _, e in sorted(entries.items())]

    def section(self, heading: str, manual: str) -> list[str]:
        member = manual + ".stx"
        if member not in self.members:
            return []
        rows = self.member(member).splitlines()
        start = next((i for i, row in enumerate(rows)
                      if (m := STX_ROW.match(row)) and m.group(1) == "H" and m.group(2).strip() == heading), None)
        if start is None:
            return []
        lines = []
        for row in rows[start + 1:start + 1 + MAX_SECTION_ROWS]:
            match = STX_ROW.match(row)
            if not match or match.group(1) == "H":
                break
            lines.append(match.group(2).strip())
        return lines


def word_stem(word: str) -> str:
    stem = ENDING.sub("", word)
    return stem if len(stem) >= 4 else word


def search_words(query: str) -> list[str]:
    """Regular expressions, one for each word that means something: profile also finds profiling."""
    words = []
    for word in re.findall(r"[a-z0-9_]+", query.lower()):
        if word in STOP_WORDS or len(word) < 3:
            continue
        also = ALSO_SEARCH.get(word)
        words.append(f"(?:{word_stem(word)}|{also})" if also else re.escape(word_stem(word)))
    return words


def _rarity(catalog: list[Entry], word: str) -> float:
    """A word few entries use says more: in a whole question, profile matters and target does not."""
    pattern = re.compile(rf"\b{word}")
    uses = sum(bool(pattern.search(e.summary.lower()) or pattern.search(e.bare.lower().replace(".", " "))) for e in catalog)
    return math.log((len(catalog) + 1) / (uses + 1)) + 0.1


def _hit(entry: Entry, word: str) -> int:
    """How much a word says about an entry: in the summary or the last word of its name most."""
    name = entry.bare.lower()
    if re.search(rf"\b{word}", entry.summary.lower()) or re.match(word, name.rsplit(".", 1)[-1]):
        return 3
    return 2 if re.search(rf"\b{word}", name.replace(".", " ")) else 0


@lru_cache(maxsize=2)
def load_help(path: Path) -> Help | None:
    try:
        return Help(path)
    except (OSError, ValueError, IndexError, KeyError):
        return None


def _target_note(name: str, picked: Pick) -> str:
    shown = ", ".join(picked.targets[:MAX_TARGETS])
    more = f" and {len(picked.targets) - MAX_TARGETS} more" if len(picked.targets) > MAX_TARGETS else ""
    return f'The syntax differs by target, so no page is shown. Pages: {shown}{more}. Say which: manual("{name} <target>").'


def _page(library: Help, name: str, picked: Pick) -> list[str]:
    entry = picked.command or picked.function
    lines = [entry.row if entry.summary else entry.name]
    if picked.differs_by_target:
        return [*lines, _target_note(entry.bare, picked)]
    lines += library.format_of(entry, PAGE_FORMAT_LINES)
    about = library.about(entry)
    if about:
        lines.append(about)
    examples = library.examples(entry)
    if examples:
        lines += ["Examples:", *[f"  {example}" for example in examples]]
    if picked.command and picked.function:
        lines.append(f"Also a function: {picked.function.name}")
    if entry.general and len(picked.targets) > 1:
        lines.append(f'{len(picked.targets)} targets add to this page: manual("{entry.bare} <target>"), '
                     f"for example {picked.targets[0]}.")
    children = library.children(name)
    if children:
        more = f" (first {MAX_CHILDREN} of {len(children)})" if len(children) > MAX_CHILDREN else ""
        lines.append(f"Subcommands{more}: " + ", ".join(children[:MAX_CHILDREN]))
    return lines


def _unknown_name(library: Help, name: str) -> str | None:
    close = library.close_to(name, "command") + library.close_to(name, "function")
    if not close:
        return None
    return "\n".join([f"Not in the manuals: {name}. Close names, by spelling only:", *[e.row for e in close[:MAX_MATCHES]]])


def manual_reply(library: Help, query: str) -> tuple[str, bool]:
    """The text of the manual tool, and True when nothing was found."""
    query = query.strip()
    section = SECTION.match(query)
    if section:
        lines = library.section(*section.groups())
        if lines:
            return "\n".join(lines)[:2500], False
    head, *rest = query.split() or [""]
    name = head.split("(", 1)[0].rstrip(".")
    target = rest[0].lower() if len(rest) == 1 and re.fullmatch(r"\w+", rest[0]) else ""
    written_as_a_name = not rest or "." in head or "(" in head or any(not word.isalpha() for word in rest)
    picked = library.pick(name, target)
    if target and not any(target in e.source for e in picked.pages):
        rtos = library.rtos_manual(target)
        if rtos and (picked.command or picked.function):
            return "\n".join([f"{name} is not in {rtos}.", *library.rtos_overview(rtos)]), False
        target = ""
    if (written_as_a_name or target) and (picked.command or picked.function):
        rtos = library.rtos_manual(" ".join(rest))  # TASK.CONFIG FreeRTOS setup: the general page has no file name
        return "\n".join(_page(library, name, picked) + (library.rtos_overview(rtos) if rtos else [])), False
    if "." in name and NAME.match(name):  # a name got wrong, which words would only lead away from
        close = _unknown_name(library, name)
        if close:
            return close, False
    rtos = library.rtos_manual(query)
    if rtos:
        return "\n".join(library.rtos_overview(rtos)), False
    full, partial = library.search(query)
    lines = [e.row for e in full]
    if partial:
        lines += ["Partly matching:" if lines else "Nothing has all the words. Partly matching:", *[e.row for e in partial]]
    sections = library.mentions(query) if len(full) < 3 else []
    if sections:
        lines += ["Sections that mention it, open one by its whole line:", *[f"  {section}" for section in sections]]
    if not lines:
        english = "" if query.isascii() else " The manuals are in English."
        return f"Failed: nothing in the manuals matches {query!r}.{english}\nNext: try another word for it.", True
    return "\n".join(lines), False


def look_up(sys_dir: Path, query: str) -> tuple[str, bool]:
    """The manual tool. It reads files only, so it works with no session and with PowerView dead."""
    library = load_help(sys_dir / "help.t32")
    if not library:
        return f"Failed: cannot read {sys_dir / 'help.t32'}.\nNext: the manuals are in {sys_dir / 'pdf'}.", True
    return manual_reply(library, query)
