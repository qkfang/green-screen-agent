from __future__ import annotations

import sys

import anyio
from mcp import Client, StdioServerParameters

from green_screen_agent.mcp_server import create_server
from green_screen_agent.prompts import AGENT_INSTRUCTIONS
from green_screen_agent.tools import TOOL_NAMES, TOOL_SPECS, GreenScreenTools

from conftest import DEMO_SECRET, demo_settings


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content)


def test_mcp_tools_mirror_tool_specs():
    async def scenario():
        async with Client(create_server(GreenScreenTools(demo_settings("127.0.0.1", 3270)))) as client:
            return await client.list_tools()

    listed = {tool.name: tool for tool in anyio.run(scenario).tools}
    assert tuple(listed) == TOOL_NAMES
    for spec in TOOL_SPECS:
        tool = listed[spec["name"]]
        assert tool.description == spec["description"]
        properties = tool.input_schema["properties"]
        assert list(properties) == list(spec["parameters"]["properties"])
        for name, prop in spec["parameters"]["properties"].items():
            assert properties[name]["description"] == prop["description"]
            assert (name in tool.input_schema.get("required", [])) == ("null" not in prop["type"])
    assert listed["read_screen"].annotations.read_only_hint is True
    assert listed["press_key"].annotations.destructive_hint is True


def test_mcp_sign_on_flow(simulator):
    async def scenario(tools):
        async with Client(create_server(tools)) as client:
            results = [
                await client.call_tool("connect", {}),
                await client.call_tool("type_credential", {"credential": "password", "row": 8, "column": 22}),
                await client.call_tool("type_credential", {"credential": "username", "row": 8, "column": 22}),
                await client.call_tool("type_credential", {"credential": "password", "row": 9, "column": 22}),
                await client.call_tool("press_key", {"key": "enter"}),
                await client.call_tool("disconnect", {}),
            ]
            return [(r.is_error, text_of(r)) for r in results]

    with GreenScreenTools(demo_settings(*simulator)) as tools:
        results = anyio.run(scenario, tools)
    assert [is_error for is_error, _ in results] == [False, True, False, False, False, False]
    assert "GREEN SCREEN DEMO - SIGN ON" in results[0][1]
    assert "Refusing to type a secret at row 8, col 22" in results[1][1]
    assert "MAIN MENU" in results[4][1]
    assert results[5][1].startswith("Disconnected from")
    assert not any(DEMO_SECRET in text for _, text in results)


def test_mcp_stdio_server(simulator, tmp_path):
    host, port = simulator
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "green_screen_agent.mcp_server"],
        env={"TN3270_HOST": host, "TN3270_PORT": str(port)},
        cwd=tmp_path,
    )

    async def scenario():
        async with Client(params) as client:
            tools = await client.list_tools()
            connected = await client.call_tool("connect", {})
            screen = await client.call_tool("read_screen", {})
            return (client.server_info.name, client.instructions), tools, connected, screen

    (name, instructions), tools, connected, screen = anyio.run(scenario)
    assert name == "tn3270"
    assert instructions == AGENT_INSTRUCTIONS
    assert len(tools.tools) == len(TOOL_NAMES)
    assert not connected.is_error and "SIGN ON" in text_of(connected)
    assert '[2] row 9, col 22, len 8, hidden (non-display)' in text_of(screen)
