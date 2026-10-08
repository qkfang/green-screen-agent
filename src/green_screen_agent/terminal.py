"""Headless TN3270 terminal for AI agents, built on IBM's ``tnz`` library.

``Tn3270Terminal`` keeps the 3270 screen buffer locally, exactly like a 3270
emulator does, and exposes it as plain text plus a list of input fields so an
LLM can "see" and operate a green-screen application. There is no GUI, no
screenshots and no OCR: the agent works with the same field-level data the
host sends over the TN3270 protocol.

``tnz`` drives its own asyncio event loop and is not thread-safe, so every
terminal owns one worker thread with a private event loop. All ``tnz`` calls
are marshalled to that thread, which also serialises concurrent tool calls.

Observers (such as the live web view) can subscribe with
:meth:`Tn3270Terminal.add_listener` to receive every screen, keystroke and key
press as it happens.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from tnz import tnz as _tnz

T = TypeVar("T")
Listener = Callable[[dict[str, Any]], None]

log = logging.getLogger(__name__)

action_source: contextvars.ContextVar[str] = contextvars.ContextVar("tn3270_action_source", default="agent")
"""Who is driving the terminal (``"agent"`` or ``"operator"``); attached to every emitted event."""

SCREEN_SIZES: dict[str, tuple[int, int]] = {
    "24x80": (24, 80),  # model 2
    "32x80": (32, 80),  # model 3
    "43x80": (43, 80),  # model 4
    "27x132": (27, 132),  # model 5
}

AID_KEYS = ("enter", "clear", "pa1", "pa2", "pa3", *(f"pf{i}" for i in range(1, 25)))
"""Attention keys: they send the modified fields to the host and wait for its reply."""

LOCAL_KEYS = {
    "tab": "key_tab",
    "backtab": "key_backtab",
    "home": "key_home",
    "eraseeof": "key_eraseeof",
    "eraseinput": "key_eraseinput",
    "newline": "key_newline",
}
"""Editing keys that only change the local screen buffer."""

CURSOR_KEYS = {
    "left": "key_curleft",
    "right": "key_curright",
    "up": "key_curup",
    "down": "key_curdown",
    "end": "key_end",
    "backspace": "key_backspace",
    "delete": "key_delete",
}
"""Local cursor/editing keys used by a human operator's keyboard (live view)."""

VALID_KEYS_HELP = "enter, clear, pf1-pf24, pa1-pa3, attn, tab, backtab, home, eraseeof, eraseinput, newline"

_KEY_ALIASES = {"return": "enter", "ereof": "eraseeof", "eof": "eraseeof", "erasefield": "eraseeof", "nl": "newline"}


class TerminalError(Exception):
    """An operation is not possible in the current terminal state."""


class _Tnz(_tnz.Tnz):
    """``tnz`` session with correct 3270 behaviour on unformatted screens.

    On a screen without field attributes (e.g. the blank CICS/KICKS screen after CLEAR),
    ``tnz`` marks the MDT by writing an attribute byte at buffer address -1, which turns
    the last screen position into a field, and then sends the input with a leading SBA
    order. A real 3270 sends unformatted input as plain data after the cursor address,
    and CICS-style hosts read the transaction id from there, so they reject the SBA form
    (KICKS abends every transaction with APCT).
    """

    def key_data(self, text: str, onerow: bool = False, zti: Any = None) -> int:
        unformatted = not any(self.plane_fa)
        try:
            return super().key_data(text, onerow, zti)
        finally:
            if unformatted:
                self.plane_fa[-1] = 0

    def send_aid(self, aid: int, short: bool | None = None) -> None:
        if short is None:
            short = 0x6B <= aid <= 0x6F  # PAx or CLEAR
        if short or self.inpid or any(self.plane_fa):
            super().send_aid(aid, short)
            return
        data = bytes(self.plane_dc).replace(b"\x00", b"")
        self.aid = aid
        self.send_3270_data(bytes([aid]) + self.address_bytes(self.curadd) + data)


def normalize_key(key: str) -> str:
    """Normalise user/LLM key names such as ``"F3"``, ``"PF03"`` or ``"Enter"``."""
    name = re.sub(r"[\s_\-]", "", str(key).strip().lower())
    name = _KEY_ALIASES.get(name, name)
    match = re.fullmatch(r"p?f0*(\d{1,2})", name)
    if match and 1 <= int(match.group(1)) <= 24:
        return f"pf{int(match.group(1))}"
    match = re.fullmatch(r"pa0*(\d)", name)
    if match and 1 <= int(match.group(1)) <= 3:
        return f"pa{int(match.group(1))}"
    if name in AID_KEYS or name in LOCAL_KEYS or name in CURSOR_KEYS or name == "attn":
        return name
    raise TerminalError(f"Unknown key {key!r}. Valid keys: {VALID_KEYS_HELP}.")


def column_ruler(cols: int) -> str:
    """ISPF-style ruler: ``....+....1....+....2`` (digit = tens of the column)."""
    return "".join(
        str(c // 10 % 10) if c % 10 == 0 else "+" if c % 5 == 0 else "." for c in range(1, cols + 1)
    )


@dataclass(frozen=True)
class InputField:
    """An unprotected (input) field. ``row``/``col`` are 1-based and point at its first character."""

    index: int
    row: int
    col: int
    length: int
    value: str
    hidden: bool
    numeric: bool
    modified: bool
    label: str


@dataclass(frozen=True)
class Screen:
    """Snapshot of the terminal screen."""

    rows: tuple[str, ...]
    cols: int
    cursor_row: int
    cursor_col: int
    fields: tuple[InputField, ...]
    formatted: bool
    keyboard_locked: bool
    connected: bool
    host: str
    bright: tuple[tuple[int, int, int], ...] = ()
    """Intensified, displayable fields as ``(row, col, length)`` of their first character."""

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly snapshot for viewers (hidden fields are already blanked)."""
        return {
            "rows": list(self.rows),
            "cols": self.cols,
            "cursor": [self.cursor_row, self.cursor_col],
            "fields": [
                {"row": f.row, "col": f.col, "length": f.length, "hidden": f.hidden,
                 "numeric": f.numeric, "modified": f.modified}
                for f in self.fields
            ],
            "bright": [list(b) for b in self.bright],
            "formatted": self.formatted,
            "keyboard_locked": self.keyboard_locked,
            "connected": self.connected,
            "host": self.host,
        }

    @property
    def text(self) -> str:
        """Screen text, one line per row (non-display fields are blanked)."""
        return "\n".join(self.rows)

    def contains(self, text: str) -> bool:
        """Case-insensitive search of the visible screen text."""
        return text.lower() in self.text.lower()

    def field_at(self, row: int, col: int) -> InputField | None:
        """Return the input field containing the 1-based position, if any."""
        address = (row - 1) * self.cols + (col - 1)
        size = len(self.rows) * self.cols
        for f in self.fields:
            start = (f.row - 1) * self.cols + (f.col - 1)
            if (address - start) % size < f.length:
                return f
        return None

    def render(self, *, max_fields: int = 40, max_value: int = 60) -> str:
        """Render the screen for an LLM: header, ruler, numbered rows and the input field list."""
        if not self.connected:
            state = "DISCONNECTED (last screen received)"
        else:
            state = "keyboard LOCKED (host is busy)" if self.keyboard_locked else "keyboard unlocked"
        lines = [
            f"Screen {len(self.rows)}x{self.cols} from {self.host or 'n/a'} | "
            f"cursor at row {self.cursor_row}, col {self.cursor_col} | {state}",
            "   " + column_ruler(self.cols),
        ]
        lines += [f"{i:02d}|{row}" for i, row in enumerate(self.rows, 1)]
        if not self.formatted:
            lines.append("Unformatted screen (no fields): type_text types at the cursor position.")
        elif not self.fields:
            lines.append("No input fields on this screen. Use press_key (e.g. enter, pf3, pf7/pf8, clear).")
        else:
            lines.append("Input fields (row/col = first character; use type_text):")
            for f in self.fields[:max_fields]:
                parts = [f"  [{f.index}] row {f.row}, col {f.col}, len {f.length}"]
                if f.hidden:
                    parts.append("hidden (non-display)")
                else:
                    value = f.value if len(f.value) <= max_value else f.value[:max_value] + "..."
                    parts.append(f'value "{value}"')
                if f.numeric:
                    parts.append("numeric")
                if f.modified:
                    parts.append("modified")
                line = ", ".join(parts)
                if f.label:
                    line += f' -- label "{f.label}"'
                lines.append(line)
            if len(self.fields) > max_fields:
                lines.append(f"  ... {len(self.fields) - max_fields} more input fields not shown")
        return "\n".join(lines)


@dataclass(frozen=True)
class TypeResult:
    """Outcome of :meth:`Tn3270Terminal.type_text`."""

    row: int
    col: int
    typed: int
    requested: int
    field: InputField | None
    adjusted: bool


@dataclass(frozen=True)
class KeyResult:
    """Outcome of :meth:`Tn3270Terminal.press`."""

    key: str
    screen: Screen
    host_responded: bool | None
    """``None`` for local editing keys, otherwise whether the host answered before the timeout."""


class Tn3270Terminal:
    """A thread-safe, headless TN3270 terminal session.

    Args:
        terminal_type: Telnet terminal type sent to the host. ``IBM-DYNAMIC`` lets the
            host query the screen size; ``IBM-3278-2-E`` is a common fixed alternative.
        screen_size: Alternate screen size offered to the host, e.g. ``(24, 80)``.
        codepage: EBCDIC code page for the host data (``cp037`` = US/Canada).
        timeout: Default seconds to wait for the host after an attention key.
        settle: Seconds without new host data before a screen is considered complete.
    """

    def __init__(
        self,
        *,
        terminal_type: str = "IBM-DYNAMIC",
        screen_size: tuple[int, int] = (24, 80),
        codepage: str = "cp037",
        timeout: float = 30.0,
        settle: float = 0.3,
    ) -> None:
        self.terminal_type = terminal_type
        self.screen_size = screen_size
        self.codepage = codepage
        self.timeout = timeout
        self.settle = settle
        self._tn: Any = None
        self._address = ""
        self._closed = False
        self._worker: threading.Thread | None = None
        self._listeners: list[Listener] = []
        self._listeners_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tn3270", initializer=self._init_worker
        )

    # ------------------------------------------------------------ public API

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener(event)`` for every terminal event; returns an unsubscribe function.

        Events are dicts with a ``type`` of ``screen`` (``screen`` = :meth:`Screen.to_dict`),
        ``type`` (text typed into a field; empty ``text`` for hidden fields), ``key``,
        ``disconnected`` or any type passed to :meth:`emit`. Listeners run on the terminal's
        worker thread (or the caller's thread for :meth:`emit`), so they must be quick and
        thread-safe.
        """
        with self._listeners_lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._listeners_lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    def emit(self, event: dict[str, Any]) -> None:
        """Send ``event`` to the listeners, tagged with the current :data:`action_source`."""
        with self._listeners_lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        event = {**event, "source": action_source.get(), "time": time.time()}
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - an observer must never break the session
                log.exception("TN3270 event listener failed")

    @property
    def connected(self) -> bool:
        return self._call(self._is_connected)

    @property
    def address(self) -> str:
        """``host:port`` of the current (or last) connection."""
        return self._address

    def connect(self, host: str, port: int, *, tls: bool = False, verify_tls: bool = True,
                timeout: float | None = None) -> tuple[Screen, bool]:
        """Connect and wait for the first screen. Returns ``(screen, host_ready)``."""
        return self._call(self._connect, host, port, tls, verify_tls, self._timeout(timeout))

    def screen(self) -> Screen:
        """Process any pending host data and return the current (or last received) screen."""
        return self._call(self._current_screen)

    def type_text(self, text: str, row: int | None = None, col: int | None = None, *,
                  clear_field: bool = True, require_hidden: bool = False) -> TypeResult:
        """Type into the input field at ``row``/``col`` (1-based) or at the cursor.

        Nothing is sent to the host until an attention key is pressed.
        """
        return self._call(self._type_text, text, row, col, clear_field, require_hidden)

    def press(self, key: str, *, timeout: float | None = None) -> KeyResult:
        """Press a key. Attention keys wait for the host's reply (keyboard unlock + settle)."""
        return self._call(self._press, key, self._timeout(timeout))

    def wait_for_text(self, text: str, timeout: float | None = None) -> tuple[bool, Screen]:
        """Wait until ``text`` appears on the screen (case-insensitive)."""
        return self._call(self._wait_for_text, text, self._timeout(timeout))

    def move_cursor(self, row: int, col: int) -> Screen:
        """Move the cursor to the 1-based position (a local action, like clicking in an emulator)."""
        return self._call(self._move_cursor, row, col)

    def disconnect(self) -> bool:
        """Close the connection. Returns ``False`` if there was no connection."""
        return self._call(self._disconnect)

    def close(self) -> None:
        """Disconnect and stop the worker thread."""
        if self._closed:
            return
        try:
            self._call(self._shutdown_worker)
        finally:
            self._closed = True
            self._executor.shutdown(wait=True)

    def __enter__(self) -> Tn3270Terminal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------- worker plumbing

    def _init_worker(self) -> None:
        self._worker = threading.current_thread()
        asyncio.set_event_loop(asyncio.new_event_loop())

    def _call(self, fn: Callable[..., T], *args: Any) -> T:
        if self._closed:
            raise TerminalError("The terminal has been closed.")
        if threading.current_thread() is self._worker:
            return fn(*args)
        # Run in the caller's context so events carry its action_source.
        return self._executor.submit(contextvars.copy_context().run, fn, *args).result()

    def _timeout(self, timeout: float | None) -> float:
        return self.timeout if timeout is None else max(0.0, float(timeout))

    def _shutdown_worker(self) -> None:
        self._disconnect()
        loop = asyncio.get_event_loop()
        loop.close()

    # --------------------------------------------------------- implementation

    def _is_connected(self) -> bool:
        return self._tn is not None and not self._tn.seslost

    def _require(self) -> Any:
        if self._tn is None:
            raise TerminalError("Not connected to a host. Use connect first.")
        if self._tn.seslost:
            raise TerminalError(
                f"The connection to {self._address} was closed ({_seslost_reason(self._tn.seslost)}). "
                "Use connect to start a new session."
            )
        return self._tn

    @staticmethod
    def _locked(tn: Any) -> bool:
        return bool(tn.pwait or tn.system_lock_wait)

    def _current_screen(self) -> Screen:
        if self._tn is None:
            raise TerminalError("Not connected to a host. Use connect first.")
        return self._snapshot()

    def _connect(self, host: str, port: int, tls: bool, verify_tls: bool, timeout: float) -> tuple[Screen, bool]:
        if self._is_connected():
            raise TerminalError(f"Already connected to {self._address}. Disconnect first.")
        self._disconnect()
        tn = _Tnz(name=f"{host}:{port}")
        tn.terminal_type = self.terminal_type
        tn.amaxrow, tn.amaxcol = self.screen_size
        tn.encoding = self.codepage
        tn.connect(host, port, secure=tls, verifycert=verify_tls)
        self._tn = tn
        self._address = f"{host}:{port}"
        ready = self._wait_for_host(timeout)
        if tn.seslost:
            reason = _seslost_reason(tn.seslost)
            self._disconnect()
            raise TerminalError(f"Could not connect to {host}:{port}: {reason}")
        return self._snapshot(), ready

    def _disconnect(self) -> bool:
        tn = self._tn
        if tn is None:
            return False
        self._tn = None
        try:
            tn.shutdown()
        finally:
            # Let the event loop process the closed transport.
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                loop.run_until_complete(asyncio.sleep(0))
            self.emit({"type": "disconnected", "host": self._address})
        return True

    def _pump(self) -> None:
        """Process host data that already arrived, without blocking."""
        tn = self._tn
        for _ in range(1000):
            if tn is None or tn.seslost or not tn.wait(0):
                return

    def _wait_for_host(self, timeout: float) -> bool:
        """Wait until the keyboard is unlocked, then until the host stops sending."""
        tn = self._tn
        deadline = time.monotonic() + timeout
        while self._locked(tn) and not tn.seslost:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            tn.wait(remaining)
        self._settle(deadline)
        return not self._locked(tn)

    def _settle(self, deadline: float) -> None:
        tn = self._tn
        settle_deadline = min(deadline, time.monotonic() + max(3.0, self.settle * 10))
        while not tn.seslost:
            remaining = settle_deadline - time.monotonic()
            if remaining <= 0 or not tn.wait(min(self.settle, remaining)):
                return

    def _press(self, key: str, timeout: float) -> KeyResult:
        tn = self._require()
        name = normalize_key(key)
        if name in LOCAL_KEYS or name in CURSOR_KEYS:
            getattr(tn, LOCAL_KEYS.get(name) or CURSOR_KEYS[name])()
            self.emit({"type": "key", "key": name, "aid": False})
            return KeyResult(name, self._snapshot(), None)
        if name == "attn":
            self.emit({"type": "key", "key": name, "aid": True})
            tn.attn()
            deadline = time.monotonic() + timeout
            tn.wait(min(timeout, 5.0))
            self._settle(deadline)
            return KeyResult(name, self._snapshot(), not self._locked(tn))
        self._pump()
        if self._locked(tn):
            raise TerminalError(
                "The keyboard is locked because the host has not finished the previous request. "
                "Use wait_for_text or read_screen and try again."
            )
        self.emit({"type": "key", "key": name, "aid": True})
        try:
            getattr(tn, name)()
        except _tnz.TnzError as exc:
            raise TerminalError(f"Cannot press {name}: {exc}") from exc
        responded = self._wait_for_host(timeout)
        return KeyResult(name, self._snapshot(), responded)

    def _wait_for_text(self, text: str, timeout: float) -> tuple[bool, Screen]:
        tn = self._require()
        deadline = time.monotonic() + timeout
        while True:
            screen = self._snapshot()
            if screen.contains(text):
                if self._locked(tn):
                    self._wait_for_host(max(0.0, deadline - time.monotonic()))
                    screen = self._snapshot()
                return True, screen
            remaining = deadline - time.monotonic()
            if remaining <= 0 or tn.seslost:
                return False, screen
            tn.wait(min(remaining, 1.0))

    def _type_text(self, text: str, row: int | None, col: int | None, clear_field: bool,
                   require_hidden: bool) -> TypeResult:
        tn = self._require()
        self._pump()
        if self._locked(tn):
            raise TerminalError("The keyboard is locked (host busy). Use wait_for_text or read_screen first.")
        text = text.rstrip("\r\n")
        if any(ch in text for ch in "\r\n\t"):
            raise TerminalError("Text must be a single line. Type one field at a time.")
        try:
            text.encode(self.codepage)
        except UnicodeEncodeError as exc:
            raise TerminalError(f"Text contains characters that cannot be sent to the host: {exc}") from exc

        size, cols = tn.buffer_size, tn.maxcol
        if (row is None) != (col is None):
            raise TerminalError("Provide both row and column, or neither to type at the cursor.")
        if row is not None and col is not None:
            if not (1 <= row <= tn.maxrow and 1 <= col <= cols):
                raise TerminalError(f"Position row {row}, col {col} is outside the {tn.maxrow}x{cols} screen.")
            address = (row - 1) * cols + (col - 1)
        else:
            address = tn.curadd

        adjusted = False
        faddr, fattr = tn.field(address)
        if faddr >= 0 and faddr == address:
            # On the attribute byte that precedes a field: move to its first character.
            if tn.is_protected_attr(fattr):
                raise TerminalError(self._not_input_message(address))
            address = (address + 1) % size
            adjusted = True
        if faddr >= 0 and tn.is_protected_attr(fattr):
            raise TerminalError(self._not_input_message(address))
        if require_hidden and (faddr < 0 or tn.is_displayable_attr(fattr)):
            raise TerminalError(
                f"Refusing to type a secret at row {address // cols + 1}, col {address % cols + 1}: "
                "it is not a hidden (non-display) field."
            )

        if faddr >= 0:
            next_faddr, _ = tn.next_field(address)
            available = (next_faddr - address) % size or size
        else:
            available = size - address  # unformatted screen: up to the end of the buffer
        tn.set_cursor_address(address)
        if clear_field and faddr >= 0:
            tn.key_eraseeof()
        try:
            typed = tn.key_data(text[:available])
        except _tnz.TnzError as exc:
            raise TerminalError(f"Typing failed: {exc}") from exc
        r, c = address // cols + 1, address % cols + 1
        hidden = faddr >= 0 and not tn.is_displayable_attr(fattr)
        self.emit({
            "type": "type", "row": r, "col": c, "text": "" if hidden else text[:typed], "typed": typed,
            "hidden": hidden, "clear": bool(clear_field and faddr >= 0),
            "field_length": available if faddr >= 0 else None,
        })
        screen = self._snapshot()
        return TypeResult(r, c, typed, len(text), screen.field_at(r, c), adjusted)

    def _move_cursor(self, row: int, col: int) -> Screen:
        tn = self._require()
        if not (1 <= row <= tn.maxrow and 1 <= col <= tn.maxcol):
            raise TerminalError(f"Position row {row}, col {col} is outside the {tn.maxrow}x{tn.maxcol} screen.")
        tn.set_cursor_address((row - 1) * tn.maxcol + (col - 1))
        return self._snapshot()

    def _not_input_message(self, address: int) -> str:
        cols = self._tn.maxcol
        screen = self._snapshot()
        where = ", ".join(f"row {f.row} col {f.col} (len {f.length})" for f in screen.fields[:10])
        hint = f" Input fields: {where}." if where else " This screen has no input fields."
        return f"Row {address // cols + 1}, col {address % cols + 1} is protected (not an input field).{hint}"

    def _snapshot(self) -> Screen:
        tn = self._tn
        if tn is None:
            rows, cols = self.screen_size
            return Screen(tuple("" for _ in range(rows)), cols, 1, 1, (), False, False, False, self._address)
        self._pump()
        size, cols, nrows = tn.buffer_size, tn.maxcol, tn.maxrow
        chars = list(tn.scrstr(0, 0, rstrip=False).ljust(size)[:size])
        attrs = list(tn.fields())
        for i, (faddr, fattr) in enumerate(attrs):
            chars[faddr] = " "
            if not tn.is_displayable_attr(fattr):
                start, end = (faddr + 1) % size, attrs[(i + 1) % len(attrs)][0]
                for offset in range((end - start) % size):
                    chars[(start + offset) % size] = " "
        rows = tuple("".join(chars[r * cols:(r + 1) * cols]).rstrip() for r in range(nrows))

        fields: list[InputField] = []
        for i, (faddr, fattr) in enumerate(attrs):
            if tn.is_protected_attr(fattr):
                continue
            start = (faddr + 1) % size
            length = (attrs[(i + 1) % len(attrs)][0] - start) % size
            if length == 0:
                continue
            value = "".join(chars[(start + k) % size] for k in range(length)).rstrip()
            hidden = not tn.is_displayable_attr(fattr)
            fields.append(
                InputField(
                    index=len(fields) + 1,
                    row=start // cols + 1,
                    col=start % cols + 1,
                    length=length,
                    value="" if hidden else value,
                    hidden=hidden,
                    numeric=bool(tn.is_numeric_attr(fattr)),
                    modified=bool(tn.is_modified_attr(fattr)),
                    label=self._label(chars, attrs, i, cols, size),
                )
            )
        bright = []
        for i, (faddr, fattr) in enumerate(attrs):
            if tn.is_displayable_attr(fattr) and tn.is_intensified_attr(fattr):
                start = (faddr + 1) % size
                length = (attrs[(i + 1) % len(attrs)][0] - start) % size
                if length:
                    bright.append((start // cols + 1, start % cols + 1, length))
        screen = Screen(
            rows=rows,
            cols=cols,
            cursor_row=tn.curadd // cols + 1,
            cursor_col=tn.curadd % cols + 1,
            fields=tuple(fields),
            formatted=bool(attrs),
            keyboard_locked=self._locked(tn),
            connected=not tn.seslost,
            host=self._address,
            bright=tuple(bright),
        )
        if self._listeners:
            self.emit({"type": "screen", "screen": screen.to_dict()})
        return screen

    @staticmethod
    def _label(chars: list[str], attrs: list[tuple[int, int]], i: int, cols: int, size: int) -> str:
        """Text of the preceding field on the same row (e.g. ``"Userid . . . :"``)."""
        faddr = attrs[i][0]
        row_start = faddr - faddr % cols
        prev = attrs[i - 1][0] if len(attrs) > 1 else faddr
        begin = prev + 1 if row_start <= prev < faddr else row_start
        label = "".join(chars[begin:faddr]).strip()
        return label[-40:]


def _seslost_reason(seslost: Any) -> str:
    if isinstance(seslost, tuple) and len(seslost) > 1 and seslost[1] is not None:
        return str(seslost[1]) or seslost[0].__name__
    return "connection closed by the host"
