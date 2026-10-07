from __future__ import annotations

import dataclasses
import inspect
import json

import pytest

from green_screen_agent.tools import TOOL_NAMES, TOOL_SPECS, GreenScreenTools, Settings

from conftest import DEMO_SECRET, DEMO_USER, demo_settings


def test_settings_defaults():
    settings = Settings.from_env({}, dotenv=False)
    assert (settings.host, settings.port, settings.tls, settings.tls_verify) == ("127.0.0.1", 3270, False, True)
    assert settings.allowed_hosts is None
    assert (settings.screen_size, settings.codepage, settings.terminal_type) == ((24, 80), "cp037", "IBM-DYNAMIC")
    assert settings.username is None and settings.password is None


def test_settings_from_env_values():
    settings = Settings.from_env(
        {
            "TN3270_HOST": "mainframe.example.com",
            "TN3270_TLS": "true",
            "TN3270_TLS_VERIFY": "no",
            "TN3270_ALLOWED_HOSTS": "Mainframe.example.com, test.example.com:2323",
            "TN3270_SCREEN_SIZE": "32X80",
            "TN3270_CODEPAGE": "cp1047",
            "TN3270_TIMEOUT": "12.5",
            "TN3270_USERNAME": "",  # empty means unset
        },
        dotenv=False,
    )
    assert (settings.host, settings.port, settings.tls, settings.tls_verify) == ("mainframe.example.com", 992, True, False)
    assert settings.allowed_hosts == {"mainframe.example.com", "test.example.com:2323"}
    assert (settings.screen_size, settings.codepage, settings.timeout) == ((32, 80), "cp1047", 12.5)
    assert settings.username is None


@pytest.mark.parametrize(
    "env",
    [{"TN3270_TLS": "maybe"}, {"TN3270_PORT": "abc"}, {"TN3270_SCREEN_SIZE": "10x10"}, {"TN3270_TIMEOUT": "soon"}],
)
def test_settings_reject_invalid_values(env):
    with pytest.raises(ValueError):
        Settings.from_env(env, dotenv=False)


def test_settings_read_dotenv_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TN3270_HOST=from-dotenv.example\nTN3270_PORT=2323\n")
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env({})
    assert (settings.host, settings.port) == ("from-dotenv.example", 2323)
    assert Settings.from_env({"TN3270_HOST": "from-env.example"}).host == "from-env.example"
    assert Settings.from_env({}, dotenv=False).host == "127.0.0.1"


def test_settings_repr_hides_credentials():
    settings = demo_settings("127.0.0.1", 3270)
    assert settings.password == DEMO_SECRET
    assert DEMO_SECRET not in repr(settings)


def test_host_allow_list():
    settings = Settings(host="mainframe.example.com")
    assert settings.is_allowed("MAINFRAME.example.com", 23)
    assert not settings.is_allowed("evil.example.com", 23)
    settings = Settings(allowed_hosts=frozenset({"a.example.com", "b.example.com:992"}))
    assert settings.is_allowed("a.example.com", 1) and settings.is_allowed("b.example.com", 992)
    assert not settings.is_allowed("b.example.com", 23) and not settings.is_allowed("127.0.0.1", 3270)
    assert Settings(allowed_hosts=frozenset({"*"})).is_allowed("anything.example.com", 23)


def test_tool_specs_are_strict_function_schemas():
    assert TOOL_NAMES == (
        "connect",
        "read_screen",
        "type_text",
        "type_credential",
        "press_key",
        "wait_for_text",
        "disconnect",
    )
    assert len(set(TOOL_NAMES)) == len(TOOL_SPECS) == 7
    for spec in TOOL_SPECS:
        params = spec["parameters"]
        assert spec["description"]
        assert params["type"] == "object" and params["additionalProperties"] is False
        assert params["required"] == list(params["properties"])
        for prop in params["properties"].values():
            assert prop["description"]
        method = getattr(GreenScreenTools, spec["name"])
        assert list(inspect.signature(method).parameters)[1:] == list(params["properties"])


def test_connect_is_restricted_to_allowed_hosts(tools):
    result = tools.call("connect", {"host": "example.com", "port": 23, "use_tls": None})
    assert result.startswith("ERROR: Connecting to example.com:23 is not allowed")
    assert tools.call("connect", {"host": None, "port": 70000, "use_tls": None}).startswith("ERROR: Invalid port")


def test_sign_on_with_credentials_never_reveals_them(tools):
    outputs = [
        tools.call("connect", '{"host": null, "port": null, "use_tls": null}'),
        tools.call("type_credential", {"credential": "username", "row": 8, "column": 22}),
        tools.call("press_key", {"key": "tab"}),
        tools.call("type_credential", {"credential": "password", "row": None, "column": None}),  # at the cursor
        tools.call("read_screen", {}),
        tools.call("press_key", {"key": "Enter"}),
    ]
    assert outputs[0].startswith("Connected to 127.0.0.1:")
    assert outputs[1].startswith('Typed the configured username into input field [1] "Userid . . . . :"')
    assert outputs[3].startswith('Typed the configured password into input field [2] "Password . . . :"')
    assert outputs[5].startswith("Pressed enter.\n") and "MAIN MENU" in outputs[5]
    assert f"Welcome, {DEMO_USER}" in outputs[5]
    assert not any(DEMO_SECRET in output for output in outputs)


def test_password_only_goes_into_hidden_fields(tools):
    tools.connect()
    result = tools.call("type_credential", {"credential": "password", "row": 8, "column": 22})
    assert result.startswith("ERROR: Refusing to type a secret")
    assert DEMO_SECRET not in result
    assert tools.terminal.screen().fields[0].value == ""


def test_missing_credentials_are_reported(simulator):
    settings = dataclasses.replace(demo_settings(*simulator), username=None)
    with GreenScreenTools(settings) as tools:
        tools.connect()
        result = tools.call("type_credential", {"credential": "username", "row": 8, "column": 22})
        assert result.startswith("ERROR: No username is configured (TN3270_USERNAME)")
        assert tools.call("type_credential", {"credential": "pin", "row": None, "column": None}).startswith("ERROR:")


def test_call_reports_errors_as_text(tools):
    assert tools.call("read_screen").startswith("ERROR: Not connected to a host")
    assert tools.call("format_disk", {}).startswith("ERROR: Unknown tool 'format_disk'")
    assert tools.call("press_key", "{not json").startswith("ERROR:")
    assert tools.call("press_key", "[1]").startswith("ERROR: Tool arguments must be a JSON object")
    assert tools.call("press_key", {"key": "enter", "force": True}).startswith("ERROR: Unknown argument(s)")
    assert tools.call("press_key", {}).startswith("ERROR:")
    tools.connect()
    assert tools.call("press_key", {"key": "pf99"}).startswith("ERROR: Unknown key 'pf99'")
    assert tools.call("type_text", {"text": "X", "row": 1, "column": 1, "clear_field": None}).startswith(
        "ERROR: Row 1, col 1 is protected"
    )
    assert tools.call("wait_for_text", {"text": " ", "timeout_seconds": None}).startswith("ERROR: text must not be empty")


def test_tool_flow_messages(tools):
    assert "Already connected" in tools.connect() + tools.connect()
    result = tools.type_text("TOOLONGUSERID", 8, 22, None)
    assert result.startswith('Typed 8 of 13 characters into input field [1] "Userid . . . . :" at row 8, col 22 (len 8).')
    assert "Pressed tab (local editing key" in tools.press_key("tab")
    assert tools.wait_for_text("SIGN ON", 1).startswith('Found "SIGN ON".')
    assert tools.wait_for_text("MAIN MENU", 0.2).startswith('"MAIN MENU" did not appear within 0.2s.')
    assert "The host closed the connection." in tools.press_key("pf3")
    assert "DISCONNECTED" in tools.read_screen()
    assert tools.disconnect().startswith("Disconnected from 127.0.0.1:")
    assert tools.disconnect() == "Not connected."


def test_tool_outputs_are_plain_strings(tools):
    output = tools.call("connect", json.dumps({"host": None, "port": None, "use_tls": None}))
    assert isinstance(output, str) and "Input fields" in output
