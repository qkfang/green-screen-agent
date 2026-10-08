from __future__ import annotations

import asyncio
import json
import sys
import threading
import urllib.error
import urllib.request

import anyio
import pytest
from mcp import Client, StdioServerParameters

from green_screen_agent.live import EventHub, LiveView
from green_screen_agent.mcp_server import create_server
from green_screen_agent.terminal import TerminalError
from green_screen_agent.tools import GreenScreenTools

from conftest import DEMO_SECRET, demo_settings, unused_port


def post(url: str, body: dict, **headers: str) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def read_events(url: str, until, timeout: float = 15) -> list[dict]:
    """Read the page's Server-Sent Events stream until ``until(event)`` is true."""
    events = []
    with urllib.request.urlopen(f"{url}/events", timeout=timeout) as response:
        for raw in response:
            line = raw.decode().strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if until(events[-1]):
                    return events
    raise AssertionError(f"stream ended early: {events}")


def screen_text(event: dict) -> str:
    return "\n".join(event["screen"]["rows"])


def test_agent_actions_are_emitted_as_events(tools):
    events: list[dict] = []
    tools.terminal.add_listener(events.append)

    tools.connect()
    tools.type_credential("username", 8, 22)
    tools.type_text("PIN4711", 9, 22)  # text typed into a hidden field must not leak either
    tools.type_credential("password", 9, 22)
    tools.press_key("enter")
    with pytest.raises(TerminalError):
        tools.type_text("x", 1, 1)

    tool_events = [(e["name"], e["phase"]) for e in events if e["type"] == "tool"]
    assert tool_events == [
        ("connect", "start"), ("connect", "end"),
        ("type_credential", "start"), ("type_credential", "end"),
        ("type_text", "start"), ("type_text", "end"),
        ("type_credential", "start"), ("type_credential", "end"),
        ("press_key", "start"), ("press_key", "end"),
        ("type_text", "start"), ("type_text", "end"),
    ]
    start = [e for e in events if e.get("name") == "type_text" and e["phase"] == "start"][-1]
    assert start["arguments"] == {"text_length": 1, "row": 1, "column": 1}
    failed = events[-1]
    assert failed["phase"] == "end" and failed["ok"] is False and "protected" in failed["result"]

    typed = [e for e in events if e["type"] == "type"]
    assert typed[0] | {"time": 0} == {
        "type": "type", "row": 8, "col": 22, "text": "DEMO", "typed": 4, "hidden": False, "clear": True,
        "field_length": 8, "source": "agent", "time": 0,
    }
    assert typed[1]["hidden"] is True and typed[1]["text"] == "" and typed[1]["typed"] == len("PIN4711")
    assert typed[2]["hidden"] is True and typed[2]["text"] == "" and typed[2]["typed"] == len(DEMO_SECRET)

    kinds = [e["type"] for e in events]
    enter = kinds.index("key")
    assert events[enter]["key"] == "enter" and events[enter]["aid"] is True
    menu = next(i for i, e in enumerate(events) if e["type"] == "screen" and "MAIN MENU" in screen_text(e))
    press_end = next(i for i, e in enumerate(events) if e.get("name") == "press_key" and e.get("phase") == "end")
    assert enter < menu < press_end
    assert {e["source"] for e in events} == {"agent"}
    assert DEMO_SECRET not in json.dumps(events) and "PIN4711" not in json.dumps(events)


def test_event_hub_dedupes_screens_and_replays_state():
    screen = {"rows": ["HELLO"], "cols": 80}

    async def scenario():
        hub = EventHub()
        hub.publish({"type": "tool", "phase": "start", "id": 1, "name": "read_screen"})
        hub.publish({"type": "screen", "screen": screen})
        hub.publish({"type": "screen", "screen": dict(screen)})
        queue, initial = hub.subscribe()
        assert hub.has_subscribers
        hub.publish({"type": "type", "row": 1, "col": 1, "text": "x"})
        thread = threading.Thread(target=hub.publish, args=({"type": "screen", "screen": screen},))
        thread.start()
        thread.join()
        received = [await asyncio.wait_for(queue.get(), 5) for _ in range(2)]
        hub.unsubscribe(queue)
        return hub, initial, received

    hub, initial, received = asyncio.run(scenario())
    assert [e["type"] for e in initial] == ["hello", "tool", "screen"]
    assert initial[0]["seq"] == 2  # the duplicate screen was dropped
    # A screen identical to the last one is still sent after typing, so viewers can resync.
    assert [e["type"] for e in received] == ["type", "screen"]
    assert not hub.has_subscribers


def test_live_view_shares_one_session_between_operator_and_mcp_agent(simulator):
    with GreenScreenTools(demo_settings(*simulator)) as tools:
        live = LiveView(tools, port=unused_port(), mcp_server=create_server(tools))
        url = live.start(fallback_port=False)
        try:
            with urllib.request.urlopen(f"{url}/", timeout=10) as response:
                assert "TN3270 LIVE" in response.read().decode()

            # The human operator connects and types from the page...
            assert post(f"{url}/api/tool", {"name": "connect"})["ok"]
            assert post(f"{url}/api/keyboard", {"text": "DEMO"}) == {"ok": True}
            assert post(f"{url}/api/keyboard", {"key": "tab"}) == {"ok": True}
            assert post(f"{url}/api/keyboard", {"cursor": [8, 22]}) == {"ok": True}
            rejected = post(f"{url}/api/tool", {"name": "type_text", "arguments": {"text": "x", "row": 1, "column": 1}})
            assert not rejected["ok"] and "protected" in rejected["result"]

            # ...and the agent continues on the same connection over MCP streamable HTTP.
            async def agent():
                async with Client(f"{url}/mcp") as client:
                    connected = await client.call_tool("connect", {})
                    await client.call_tool("type_credential", {"credential": "password", "row": 9, "column": 22})
                    menu = await client.call_tool("press_key", {"key": "enter"})
                    return connected.content[0].text, menu.content[0].text

            connected, menu = anyio.run(agent)
            assert connected.startswith("Already connected") and 'value "DEMO"' in connected
            assert "MAIN MENU" in menu

            events = read_events(url, lambda e: e["type"] == "screen")
            assert [e["type"] for e in events][0] == "hello" and events[0]["mcp"] is True
            tools_seen = [(e["source"], e["name"]) for e in events if e["type"] == "tool" and e["phase"] == "start"]
            assert tools_seen == [("operator", "connect"), ("operator", "type_text"), ("agent", "connect"),
                                  ("agent", "type_credential"), ("agent", "press_key")]
            assert "MAIN MENU" in screen_text(events[-1])
            assert DEMO_SECRET not in json.dumps(events)

            with pytest.raises(urllib.error.HTTPError) as forbidden:
                post(f"{url}/api/tool", {"name": "disconnect"}, Origin="http://evil.example")
            assert forbidden.value.code == 403
            assert tools.terminal.connected
        finally:
            live.close()


def test_stdio_mcp_server_serves_live_view_of_its_session(simulator, tmp_path):
    host, port = simulator
    live_port = unused_port()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "green_screen_agent.mcp_server", "--live", str(live_port)],
        env={"TN3270_HOST": host, "TN3270_PORT": str(port)},
        cwd=tmp_path,
    )

    async def scenario():
        async with Client(params) as client:
            await client.call_tool("connect", {})
            return await anyio.to_thread.run_sync(
                read_events, f"http://127.0.0.1:{live_port}", lambda e: e["type"] == "screen")

    events = anyio.run(scenario)
    assert events[0]["type"] == "hello" and events[0]["mcp"] is False
    assert any(e["type"] == "tool" and e["name"] == "connect" and e["source"] == "agent" for e in events)
    assert "SIGN ON" in screen_text(events[-1])
