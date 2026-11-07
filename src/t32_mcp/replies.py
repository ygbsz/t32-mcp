"""Reply text. One fixed shape."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Reply:
    """What a job has to say. The engine adds the header."""

    lines: list[str] = field(default_factory=list)
    content: str | None = None  # raw TRACE32 output, set off from the tail
    tail: list[str] = field(default_factory=list)
    is_error: bool = False
    label: str = ""


def ok(*lines: str, label: str = "") -> Reply:
    return Reply(lines=list(lines), label=label)


def warning(text: str) -> str:
    """TRACE32 words some of its messages as warnings already."""
    return text if text.lower().startswith("warning:") else f"Warning: {text}"


def failed(what: str, *detail: str, next_step: str | None = None, label: str = "") -> Reply:
    lines = [f"Failed: {what}", *detail]
    if next_step:
        lines.append(f"Next: {next_step}")
    return Reply(lines=lines, is_error=True, label=label)


def duration(ms: float) -> str:
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def render(op: int, generation: int, target: str, ms: float | None, reply: Reply) -> str:
    fields = [f"op {op}"]
    if generation:
        fields.append(f"g{generation}")
    fields.append(target)
    if ms is not None:
        fields.append(duration(ms))
    if reply.label:
        fields.append(reply.label)
    parts = ["[" + " · ".join(fields) + "]"]
    parts.extend(reply.lines)
    if reply.content is not None:
        parts.append("")
        parts.append(reply.content)
        if reply.tail:
            parts.append("")
            parts.append("---")
    parts.extend(reply.tail)
    return "\n".join(parts)


def format_value(value: object) -> str:
    """Spare the model the arithmetic: integers come in both bases."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:#x} ({value})" if value >= 0 else str(value)
    return str(value)


def tidy(text: str) -> str:
    """Drop the padding PowerView prints; it costs tokens and says nothing."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
