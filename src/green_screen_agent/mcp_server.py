"""MCP server that lets GitHub Copilot (CLI, VS Code, coding agent) drive a TN3270 green screen.

Run it over stdio (the transport Copilot CLI and VS Code use for local servers)::

    python -m green_screen_agent.mcp_server

Configuration comes from ``TN3270_*`` environment variables or a ``.env`` file
in the working directory (see ``.env.example``). Each server process owns one
terminal session, so every Copilot session gets its own 3270 connection.
"""

import argparse
import logging
import sys
from typing import Annotated, Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .prompts import AGENT_INSTRUCTIONS
from .terminal import TerminalError
from .tools import TOOL_SPECS, GreenScreenTools, ToolError

_SPECS = {spec["name"]: spec for spec in TOOL_SPECS}


def _doc(tool: str) -> str:
    return _SPECS[tool]["description"]


def _arg(tool: str, name: str) -> str:
    return _SPECS[tool]["parameters"]["properties"][name]["description"]


def _run(fn: Callable[..., str], *args: Any) -> str:
    try:
        return fn(*args)
    except (ToolError, TerminalError, ValueError) as exc:
        # MCP only forwards the message of its own ToolError to the model.
        raise MCPToolError(str(exc)) from exc


def create_server(tools: GreenScreenTools | None = None) -> MCPServer:
    """Create the MCP server; tool descriptions and parameters mirror :data:`TOOL_SPECS`."""
    tools = tools if tools is not None else GreenScreenTools()
    server = MCPServer(name="tn3270", title="TN3270 green screen", version=__version__,
                       instructions=AGENT_INSTRUCTIONS)

    def connect(
        host: Annotated[str | None, Field(description=_arg("connect", "host"))] = None,
        port: Annotated[int | None, Field(description=_arg("connect", "port"))] = None,
        use_tls: Annotated[bool | None, Field(description=_arg("connect", "use_tls"))] = None,
    ) -> str:
        return _run(tools.connect, host, port, use_tls)

    def read_screen() -> str:
        return _run(tools.read_screen)

    def type_text(
        text: Annotated[str, Field(description=_arg("type_text", "text"))],
        row: Annotated[int | None, Field(description=_arg("type_text", "row"))] = None,
        column: Annotated[int | None, Field(description=_arg("type_text", "column"))] = None,
        clear_field: Annotated[bool | None, Field(description=_arg("type_text", "clear_field"))] = None,
    ) -> str:
        return _run(tools.type_text, text, row, column, clear_field)

    def type_credential(
        credential: Annotated[Literal["username", "password"], Field(description=_arg("type_credential", "credential"))],
        row: Annotated[int | None, Field(description=_arg("type_credential", "row"))] = None,
        column: Annotated[int | None, Field(description=_arg("type_credential", "column"))] = None,
    ) -> str:
        return _run(tools.type_credential, credential, row, column)

    def press_key(key: Annotated[str, Field(description=_arg("press_key", "key"))]) -> str:
        return _run(tools.press_key, key)

    def wait_for_text(
        text: Annotated[str, Field(description=_arg("wait_for_text", "text"))],
        timeout_seconds: Annotated[float | None, Field(description=_arg("wait_for_text", "timeout_seconds"))] = None,
    ) -> str:
        return _run(tools.wait_for_text, text, timeout_seconds)

    def disconnect() -> str:
        return _run(tools.disconnect)

    local = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
    registrations = [
        (connect, "Connect to the host",
         ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)),
        (read_screen, "Read the screen", read_only),
        (type_text, "Type into a field", local),
        (type_credential, "Type a configured credential", local),
        (press_key, "Press a 3270 key",
         ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)),
        (wait_for_text, "Wait for text on the screen", read_only),
        (disconnect, "Disconnect from the host",
         ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)),
    ]
    for fn, title, annotations in registrations:
        server.add_tool(fn, name=fn.__name__, title=title, description=_doc(fn.__name__),
                        annotations=annotations, structured_output=False)
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TN3270 green-screen MCP server (stdio transport).")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--live", nargs="?", type=int, const=3271, default=None, metavar="PORT",
        help="Also serve the live green-screen web view of this session on 127.0.0.1:PORT (default 3271; "
        "a free port is used if it is taken).",
    )
    args = parser.parse_args(argv)
    # stdout carries the MCP protocol; diagnostics go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    tools = GreenScreenTools()
    live = None
    if args.live is not None:
        from .live import LiveView

        live = LiveView(tools, port=args.live)
        print(f"Live green screen: {live.start()}/", file=sys.stderr, flush=True)
    try:
        create_server(tools).run("stdio")
    finally:
        if live is not None:
            live.close()
        tools.close()


if __name__ == "__main__":
    main()
