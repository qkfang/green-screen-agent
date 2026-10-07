from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from green_screen_agent import foundry_agent
from green_screen_agent.foundry_agent import confirm_attention_keys, run_turn
from green_screen_agent.prompts import AGENT_INSTRUCTIONS
from green_screen_agent.tools import TOOL_NAMES, TOOL_SPECS, GreenScreenTools

from conftest import DEMO_SECRET, demo_settings


def call(name: str, call_id: str, **arguments) -> SimpleNamespace:
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id)


def response(*calls: SimpleNamespace, text: str = "") -> SimpleNamespace:
    output = list(calls) or [SimpleNamespace(type="message")]
    return SimpleNamespace(status="completed", output=output, output_text=text)


class FakeOpenAI:
    """Plays back scripted model turns and records what the agent loop sends."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        step = self.script.pop(0)
        return step(kwargs["input"]) if callable(step) else step


def outputs_of(request: dict) -> dict[str, str]:
    return {item["call_id"]: item["output"] for item in request["input"]}


@pytest.fixture
def tools(simulator):
    with GreenScreenTools(demo_settings(*simulator)) as green_screen_tools:
        yield green_screen_tools


def test_function_tools_and_agent_definition():
    pytest.importorskip("azure.ai.projects")
    tools = foundry_agent.function_tools()
    assert [tool.name for tool in tools] == list(TOOL_NAMES)
    for tool, spec in zip(tools, TOOL_SPECS):
        assert tool.strict is True
        assert tool.parameters == spec["parameters"]
        assert tool.description == spec["description"]
    definition = foundry_agent.agent_definition("gpt-4.1")
    assert definition.model == "gpt-4.1"
    assert definition.instructions == AGENT_INSTRUCTIONS
    assert len(definition.tools) == len(TOOL_NAMES)


def test_run_turn_executes_tool_calls_until_the_answer(tools):
    def after_connect(inputs):
        assert "GREEN SCREEN DEMO - SIGN ON" in outputs_of({"input": inputs})["c1"]
        return response(
            call("type_credential", "c2", credential="username", row=8, column=22),
            call("type_credential", "c3", credential="password", row=9, column=22),
            call("press_key", "c4", key="enter"),
        )

    def after_sign_on(inputs):
        outputs = outputs_of({"input": inputs})
        assert "MAIN MENU" in outputs["c4"]
        return response(
            call("type_text", "c5", text="1", row=10, column=15, clear_field=None),
            call("press_key", "c6", key="enter"),
            call("type_text", "c7", text="100003", row=4, column=25, clear_field=None),
            call("press_key", "c8", key="enter"),
        )

    def after_inquiry(inputs):
        assert "CASCADE OUTDOOR SUPPLY" in outputs_of({"input": inputs})["c8"]
        return response(text="Customer 100003 (CASCADE OUTDOOR SUPPLY) has status HOLD.")

    client = FakeOpenAI([response(call("connect", "c1", host=None, port=None, use_tls=None)),
                         after_connect, after_sign_on, after_inquiry])
    events = []
    answer = run_turn(client, agent_name="gs-agent", conversation_id="conv_1", user_input="Status of 100003?",
                      tools=tools, on_event=lambda kind, text: events.append((kind, text)))

    assert answer == "Customer 100003 (CASCADE OUTDOOR SUPPLY) has status HOLD."
    assert client.requests[0]["input"] == "Status of 100003?"
    for request in client.requests:
        assert request["conversation"] == "conv_1"
        assert request["extra_body"] == {"agent_reference": {"name": "gs-agent", "type": "agent_reference"}}
    assert [kind for kind, _ in events].count("call") == 8
    assert not any(DEMO_SECRET in text for _, text in events)
    assert not any(DEMO_SECRET in json.dumps(request["input"]) for request in client.requests)


def test_run_turn_skips_the_rest_of_a_batch_after_an_error(tools):
    client = FakeOpenAI([
        response(call("connect", "c1", host=None, port=None, use_tls=None)),
        response(call("type_text", "c2", text="X", row=1, column=1, clear_field=None),
                 call("press_key", "c3", key="enter")),
        response(text="I could not type there."),
    ])
    assert run_turn(client, agent_name="a", conversation_id="c", user_input="go", tools=tools) == (
        "I could not type there."
    )
    outputs = outputs_of(client.requests[2])
    assert outputs["c2"].startswith("ERROR: Row 1, col 1 is protected")
    assert outputs["c3"].startswith("ERROR: Skipped")
    assert "SIGN ON" in tools.read_screen()  # enter was never pressed


def test_run_turn_respects_operator_rejection(tools):
    client = FakeOpenAI([
        response(call("connect", "c1", host=None, port=None, use_tls=None), call("press_key", "c2", key="pf3")),
        response(text="Stopped."),
    ])
    asked = []
    approve = confirm_attention_keys(tools, ask=lambda prompt: asked.append(prompt) or "n")
    run_turn(client, agent_name="a", conversation_id="c", user_input="exit", tools=tools, approve=approve)
    assert outputs_of(client.requests[1])["c2"].startswith("ERROR: The operator rejected this action")
    assert asked == ["Agent wants to press PF3 and send this screen to the host. Allow? [y/N] "]
    assert tools.terminal.connected  # PF3 would have ended the session


def test_confirm_attention_keys_only_asks_for_host_keys(tools):
    answers = iter(["y", ""])
    approve = confirm_attention_keys(tools, ask=lambda prompt: next(answers))
    assert approve("connect", {}) and approve("type_text", {"text": "x"})
    assert approve("press_key", {"key": "tab"}) and approve("press_key", {"key": "bogus"})
    assert approve("press_key", {"key": "enter"}) is True
    assert approve("press_key", {"key": "F3"}) is False


def test_run_turn_stops_after_max_tool_rounds(tools):
    client = FakeOpenAI([response(call("read_screen", f"c{i}")) for i in range(4)])
    with pytest.raises(RuntimeError, match="did not finish within 3 tool rounds"):
        run_turn(client, agent_name="a", conversation_id="c", user_input="loop", tools=tools, max_tool_rounds=3)


def test_run_turn_raises_on_failed_response(tools):
    failed = SimpleNamespace(status="failed", error={"code": "server_error"}, output=[], output_text="")
    with pytest.raises(RuntimeError, match="server_error"):
        run_turn(FakeOpenAI([failed]), agent_name="a", conversation_id="c", user_input="x", tools=tools)


def test_main_requires_foundry_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_MODEL_NAME", raising=False)
    with pytest.raises(SystemExit) as exc:
        foundry_agent.main([])
    assert exc.value.code == 2
    assert "FOUNDRY_PROJECT_ENDPOINT" in capsys.readouterr().err
