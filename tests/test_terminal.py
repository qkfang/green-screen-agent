from __future__ import annotations

import pytest

from green_screen_agent.simulator import BackgroundSimulator
from green_screen_agent.terminal import TerminalError, Tn3270Terminal, column_ruler, normalize_key

from conftest import DEMO_SECRET, DEMO_USER, unused_port


@pytest.fixture
def terminal(simulator):
    with Tn3270Terminal(timeout=10, settle=0.1) as term:
        term.connect(*simulator)
        yield term


def sign_on(term: Tn3270Terminal) -> None:
    term.type_text(DEMO_USER, 8, 22)
    term.type_text(DEMO_SECRET, 9, 22)
    assert term.press("enter").screen.contains("MAIN MENU")


@pytest.mark.parametrize(
    ("name", "expected"),
    [("F3", "pf3"), ("PF03", "pf3"), ("pf 12", "pf12"), ("PF24", "pf24"), ("Enter", "enter"), ("RETURN", "enter"),
     ("PA1", "pa1"), ("clear", "clear"), ("erase_eof", "eraseeof"), ("Tab", "tab"), ("ATTN", "attn")],
)
def test_normalize_key(name, expected):
    assert normalize_key(name) == expected


@pytest.mark.parametrize("name", ["pf25", "f0", "pa4", "ctrl-c", ""])
def test_normalize_key_rejects_unknown_keys(name):
    with pytest.raises(TerminalError, match="Unknown key"):
        normalize_key(name)


def test_column_ruler():
    assert column_ruler(12) == "....+....1.."
    assert len(column_ruler(132)) == 132


def test_connect_renders_sign_on_screen(terminal):
    screen = terminal.screen()
    assert screen.contains("green screen demo - sign on")
    assert (screen.cursor_row, screen.cursor_col) == (8, 22)
    assert not screen.keyboard_locked and screen.connected and screen.formatted
    assert [(f.row, f.col, f.length, f.hidden) for f in screen.fields] == [(8, 22, 8, False), (9, 22, 8, True)]
    rendered = screen.render()
    assert "cursor at row 8, col 22 | keyboard unlocked" in rendered
    assert "01| GSSIGN" in rendered
    assert '[1] row 8, col 22, len 8, value "" -- label "Userid . . . . :"' in rendered
    assert '[2] row 9, col 22, len 8, hidden (non-display) -- label "Password . . . :"' in rendered


def test_hidden_field_content_is_never_exposed(terminal):
    terminal.type_text(DEMO_SECRET, 9, 22)
    screen = terminal.screen()
    password_field = screen.fields[1]
    assert password_field.hidden and password_field.modified and password_field.value == ""
    assert DEMO_SECRET not in screen.text
    assert DEMO_SECRET not in screen.render()


def test_typing_on_protected_text_fails(terminal):
    with pytest.raises(TerminalError, match="protected.*row 8 col 22"):
        terminal.type_text("X", 1, 1)


def test_typing_on_attribute_byte_moves_into_field(terminal):
    result = terminal.type_text(DEMO_USER, 8, 21)
    assert result.adjusted and (result.row, result.col) == (8, 22)
    assert result.field is not None and result.field.value == DEMO_USER


def test_typing_is_truncated_to_the_field(terminal):
    result = terminal.type_text("ABCDEFGHIJ", 8, 22)
    assert (result.typed, result.requested) == (8, 10)
    assert terminal.screen().fields[0].value == "ABCDEFGH"
    # Nothing spilled into the password field: the host sees an empty password.
    assert "Type your userid and password" in terminal.press("enter").screen.text


def test_type_text_validation(terminal):
    with pytest.raises(TerminalError, match="single line"):
        terminal.type_text("A\nB", 8, 22)
    with pytest.raises(TerminalError, match="outside"):
        terminal.type_text("A", 25, 1)
    with pytest.raises(TerminalError, match="both row and column"):
        terminal.type_text("A", 8, None)
    with pytest.raises(TerminalError, match="cannot be sent"):
        terminal.type_text("\u4e2d", 8, 22)
    with pytest.raises(TerminalError, match="not a hidden"):
        terminal.type_text(DEMO_SECRET, 8, 22, require_hidden=True)


def test_type_at_cursor_and_clear_field(terminal):
    terminal.type_text("ABCDEFGH")  # cursor starts in the userid field
    terminal.type_text("XY", 8, 22, clear_field=False)
    assert terminal.screen().fields[0].value == "XYCDEFGH"
    terminal.type_text("Q", 8, 22)
    assert terminal.screen().fields[0].value == "Q"


def test_local_editing_keys(terminal):
    result = terminal.press("tab")
    assert result.host_responded is None
    assert (result.screen.cursor_row, result.screen.cursor_col) == (9, 22)


def test_end_to_end_customer_update(terminal):
    sign_on(terminal)
    terminal.type_text("1", 10, 15)
    terminal.press("enter")
    terminal.type_text("100003", 4, 25)
    screen = terminal.press("enter").screen
    assert "CASCADE OUTDOOR SUPPLY" in screen.text
    assert [f.value for f in screen.fields] == ["100003", "541-555-0103", "HOLD"]
    terminal.type_text("ACTIVE", 10, 25)
    screen = terminal.press("pf5").screen
    assert "Customer 100003 updated." in screen.rows[22]
    assert screen.fields[2].value == "ACTIVE"

    terminal.press("pf3")
    terminal.type_text("2", 10, 15)
    assert "Page 1 of 3" in terminal.press("enter").screen.text
    assert "Page 2 of 3" in terminal.press("F8").screen.text
    terminal.press("PF3")
    assert "Sign-off complete." in terminal.press("pf3").screen.text

    result = terminal.press("pf3")  # exit: the host ends the session
    assert not result.screen.connected and not terminal.connected
    assert "DISCONNECTED" in result.screen.render()
    with pytest.raises(TerminalError, match="was closed"):
        terminal.press("enter")


def test_wait_for_text(terminal):
    found, screen = terminal.wait_for_text("sign on", timeout=1)
    assert found and screen.contains("SIGN ON")
    found, _ = terminal.wait_for_text("NOT ON THIS SCREEN", timeout=0.3)
    assert not found


def test_slow_host_keeps_keyboard_locked():
    with BackgroundSimulator(latency=1.0) as address, Tn3270Terminal(timeout=0.2, settle=0.05) as term:
        term.connect(*address)
        term.type_text(DEMO_USER, 8, 22)
        term.type_text(DEMO_SECRET, 9, 22)
        result = term.press("enter")
        assert result.host_responded is False and result.screen.keyboard_locked
        assert "keyboard LOCKED" in result.screen.render()
        with pytest.raises(TerminalError, match="locked"):
            term.type_text("1", 10, 15)
        with pytest.raises(TerminalError, match="locked"):
            term.press("enter")
        found, screen = term.wait_for_text("MAIN MENU", timeout=5)
        assert found and not screen.keyboard_locked


def test_connect_errors(simulator):
    with Tn3270Terminal(timeout=3) as term:
        with pytest.raises(TerminalError, match="Not connected"):
            term.screen()
        with pytest.raises(TerminalError, match="Not connected"):
            term.press("enter")
        with pytest.raises(TerminalError, match="Could not connect"):
            term.connect("127.0.0.1", unused_port())
        assert not term.disconnect()
        term.connect(*simulator)
        with pytest.raises(TerminalError, match="Already connected"):
            term.connect(*simulator)
        assert term.disconnect()
        assert not term.connected
    with pytest.raises(TerminalError, match="closed"):
        term.screen()
