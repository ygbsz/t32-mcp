"""MCP entry point: tool registration only. The work happens in engine.py."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import prompts
from .engine import Engine, ToolReply
from .jobs import CommandJob, EvaluateJob, RunJob
from .manuals import look_up
from .powerview import Install
from .scripts import ScriptJob
from .views import ViewJob

P = prompts.PARAMS
WaitS = Annotated[float | None, Field(ge=0, le=30, description=P["wait_s"])]
DeadlineS = Annotated[float | None, Field(gt=0, description=P["deadline_s"])]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
CONTROL = ToolAnnotations(read_only_hint=False, destructive_hint=False)
RAW = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def result(reply: ToolReply) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=reply.text)], is_error=reply.is_error)


def build(install: Install) -> MCPServer:
    @asynccontextmanager
    async def lifespan(server):
        engine = Engine(install)
        try:
            yield engine
        finally:
            await engine.shutdown()

    server = MCPServer("t32-mcp", instructions=prompts.INSTRUCTIONS, lifespan=lifespan)

    def engine_of(ctx: Context) -> Engine:
        return ctx.request_context.lifespan_context

    @server.tool(description=prompts.SESSION_OPEN.format(targets=", ".join(install.targets()) or "none found",
                                                         sys_dir=install.sys_dir),
                 annotations=CONTROL, structured_output=False)
    async def session_open(
        ctx: Context,
        target: Annotated[str, Field(description=P["target"])] = "arm",
        connection: Annotated[Literal["usb", "sim", "attach"], Field(description=P["connection"])] = "usb",
        serial: Annotated[str | None, Field(description=P["serial"])] = None,
        port: Annotated[int | None, Field(description=P["port"])] = None,
        access: Annotated[Literal["observe", "control"] | None, Field(description=P["access"])] = None,
        takeover: Annotated[bool, Field(description=P["takeover"])] = False,
    ) -> CallToolResult:
        return result(await engine_of(ctx).open(target, connection, serial, port, access, takeover))

    @server.tool(description=prompts.SESSION_STATUS, annotations=READ_ONLY, structured_output=False)
    async def session_status(ctx: Context) -> CallToolResult:
        return result(await engine_of(ctx).status())

    @server.tool(description=prompts.SESSION_CLOSE, annotations=CONTROL, structured_output=False)
    async def session_close(
        ctx: Context,
        leave_running: Annotated[bool, Field(description=P["leave_running"])] = False,
    ) -> CallToolResult:
        return result(await engine_of(ctx).close(leave_running))

    @server.tool(description=prompts.COMMAND, annotations=RAW, structured_output=False)
    async def command(
        lines: Annotated[list[str], Field(description=P["lines"])],
        ctx: Context,
        stop_on_error: Annotated[bool, Field(description=P["stop_on_error"])] = True,
        wait_s: WaitS = None,
        deadline_s: DeadlineS = None,
    ) -> CallToolResult:
        return result(await engine_of(ctx).execute(CommandJob(lines, stop_on_error), wait_s, deadline_s))

    @server.tool(description=prompts.EVALUATE, annotations=READ_ONLY, structured_output=False)
    async def evaluate(
        expressions: Annotated[list[str], Field(description=P["expressions"])],
        ctx: Context,
    ) -> CallToolResult:
        return result(await engine_of(ctx).execute(EvaluateJob(expressions)))

    @server.tool(description=prompts.SCRIPT, annotations=RAW, structured_output=False)
    async def script(
        ctx: Context,
        path: Annotated[str | None, Field(description=P["path"])] = None,
        text: Annotated[str | None, Field(description=P["text"])] = None,
        args: Annotated[str | None, Field(description=P["args"])] = None,
        wait_s: WaitS = None,
        deadline_s: DeadlineS = None,
    ) -> CallToolResult:
        return result(await engine_of(ctx).execute(ScriptJob(path, text, args), wait_s, deadline_s))

    @server.tool(description=prompts.VIEW, annotations=READ_ONLY, structured_output=False)
    async def view(
        window: Annotated[str, Field(description=P["window"])],
        ctx: Context,
        format: Annotated[Literal["text", "csv"], Field(description=P["format"])] = "text",
        max_chars: Annotated[int, Field(ge=100, le=200000, description=P["max_chars"])] = 12000,
        deadline_s: DeadlineS = None,
    ) -> CallToolResult:
        return result(await engine_of(ctx).execute(ViewJob(window, format, max_chars), None, deadline_s))

    @server.tool(description=prompts.RUN, annotations=CONTROL, structured_output=False)
    async def run(
        action: Annotated[Literal["go", "step", "step_over", "go_up", "go_return", "break"],
                          Field(description=P["action"])],
        ctx: Context,
        until: Annotated[str | None, Field(description=P["until"])] = None,
        wait_s: WaitS = None,
        deadline_s: DeadlineS = None,
    ) -> CallToolResult:
        return result(await engine_of(ctx).execute(RunJob(action, until), wait_s, deadline_s))

    @server.tool(description=prompts.MANUAL, annotations=READ_ONLY, structured_output=False)
    async def manual(query: Annotated[str, Field(description=P["query"])]) -> CallToolResult:
        text, is_error = await asyncio.to_thread(look_up, install.sys_dir, query)
        return result(ToolReply(text, is_error))

    @server.tool(description=prompts.OPERATION_GET, annotations=READ_ONLY, structured_output=False)
    async def operation_get(
        operation: Annotated[int, Field(description=P["operation"])],
        ctx: Context,
        wait_s: Annotated[float, Field(ge=0, le=30, description=P["get_wait_s"])] = 30,
    ) -> CallToolResult:
        return result(await engine_of(ctx).get(operation, wait_s))

    @server.tool(description=prompts.OPERATION_CANCEL, annotations=CONTROL, structured_output=False)
    async def operation_cancel(
        operation: Annotated[int, Field(description=P["operation"])],
        ctx: Context,
        kill: Annotated[bool, Field(description=P["kill"])] = False,
    ) -> CallToolResult:
        return result(await engine_of(ctx).cancel(operation, kill))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for Lauterbach TRACE32")
    parser.add_argument("--sys", default=None, help=r"TRACE32 system directory, default T32SYS or C:\T32")
    args = parser.parse_args()
    build(Install.locate(args.sys)).run(transport="stdio")


if __name__ == "__main__":
    main()
