"""LLM tool layer over :class:`~green_screen_agent.terminal.Tn3270Terminal`.

The same tool set is exposed to Microsoft Foundry agents (function tools) and to
GitHub Copilot / VS Code (MCP server). Every tool returns plain text written for
an LLM: the rendered screen or a short confirmation, or ``ERROR: ...``.
"""

from __future__ import annotations

import functools
import inspect
import itertools
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, TypeVar

from .terminal import SCREEN_SIZES, VALID_KEYS_HELP, Tn3270Terminal, TerminalError

MAX_WAIT_SECONDS = 120.0

_F = TypeVar("_F", bound=Callable[..., str])
_CALL_IDS = itertools.count(1)
_REDACTED_ARGUMENTS = {"type_text": "text"}
"""Arguments that may hold secrets (text typed into a hidden field). Events carry only their
length; viewers see typed text through the terminal's ``type`` event, which blanks hidden fields."""


def _observed(method: _F) -> _F:
    """Emit ``tool`` start/end events on the terminal so viewers can follow each action."""
    signature = inspect.signature(method)
    redact = _REDACTED_ARGUMENTS.get(method.__name__)

    @functools.wraps(method)
    def wrapper(self: GreenScreenTools, *args: Any, **kwargs: Any) -> str:
        try:
            bound = signature.bind(self, *args, **kwargs)
        except TypeError:
            return method(self, *args, **kwargs)
        arguments = {k: v for k, v in bound.arguments.items() if k != "self"}
        if redact in arguments:
            arguments[f"{redact}_length"] = len(str(arguments.pop(redact)))
        event = {"type": "tool", "id": next(_CALL_IDS), "name": method.__name__}
        self.terminal.emit({**event, "phase": "start", "arguments": arguments})
        started = time.monotonic()
        try:
            result = method(self, *args, **kwargs)
        except Exception as exc:
            self.terminal.emit({**event, "phase": "end", "ok": False, "result": f"ERROR: {exc}",
                                "duration_ms": round((time.monotonic() - started) * 1000)})
            raise
        self.terminal.emit({**event, "phase": "end", "ok": True, "result": result,
                            "duration_ms": round((time.monotonic() - started) * 1000)})
        return result

    return wrapper  # type: ignore[return-value]


class ToolError(Exception):
    """A tool call was rejected (bad arguments, policy or configuration)."""


def _parse_bool(value: str, name: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def _parse_number(value: str, name: str, kind: type[int] | type[float]) -> Any:
    try:
        return kind(value)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {value!r}") from None


@dataclass(frozen=True)
class Settings:
    """Connection settings, normally read from ``TN3270_*`` environment variables."""

    host: str = "127.0.0.1"
    port: int = 3270
    tls: bool = False
    tls_verify: bool = True
    allowed_hosts: frozenset[str] | None = None
    """Hosts the agent may connect to (``host`` or ``host:port``). ``None`` = only ``host``."""
    terminal_type: str = "IBM-DYNAMIC"
    screen_size: tuple[int, int] = (24, 80)
    codepage: str = "cp037"
    timeout: float = 30.0
    settle: float = 0.3
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, dotenv: bool = True) -> Settings:
        """Build settings from the environment (and a ``.env`` file when ``dotenv`` is true)."""
        env = dict(os.environ if environ is None else environ)
        if dotenv:
            env = {**_dotenv_values(), **{k: v for k, v in env.items() if v}}

        def get(name: str) -> str | None:
            value = env.get(name)
            return value.strip() if value and value.strip() else None

        tls = _parse_bool(get("TN3270_TLS") or "false", "TN3270_TLS")
        size_name = (get("TN3270_SCREEN_SIZE") or "24x80").lower()
        if size_name not in SCREEN_SIZES:
            raise ValueError(f"TN3270_SCREEN_SIZE must be one of {', '.join(SCREEN_SIZES)}, got {size_name!r}")
        allowed = get("TN3270_ALLOWED_HOSTS")
        return cls(
            host=get("TN3270_HOST") or cls.host,
            port=_parse_number(get("TN3270_PORT") or ("992" if tls else "3270"), "TN3270_PORT", int),
            tls=tls,
            tls_verify=_parse_bool(get("TN3270_TLS_VERIFY") or "true", "TN3270_TLS_VERIFY"),
            allowed_hosts=frozenset(h.strip().lower() for h in allowed.split(",") if h.strip()) if allowed else None,
            terminal_type=get("TN3270_TERMINAL_TYPE") or cls.terminal_type,
            screen_size=SCREEN_SIZES[size_name],
            codepage=get("TN3270_CODEPAGE") or cls.codepage,
            timeout=_parse_number(get("TN3270_TIMEOUT") or "30", "TN3270_TIMEOUT", float),
            settle=_parse_number(get("TN3270_SETTLE_SECONDS") or "0.3", "TN3270_SETTLE_SECONDS", float),
            username=get("TN3270_USERNAME"),
            password=get("TN3270_PASSWORD"),
        )

    def is_allowed(self, host: str, port: int) -> bool:
        if self.allowed_hosts is None:
            return host.lower() == self.host.lower()
        return "*" in self.allowed_hosts or host.lower() in self.allowed_hosts or (
            f"{host.lower()}:{port}" in self.allowed_hosts
        )


def _dotenv_values() -> dict[str, str]:
    try:
        from dotenv import dotenv_values, find_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a dependency
        return {}
    path = find_dotenv(usecwd=True)
    return {k: v for k, v in dotenv_values(path).items() if v} if path else {}


_ROW = {"type": ["integer", "null"], "description": "1-based row of the input field (from the field list). null = cursor position."}
_COLUMN = {"type": ["integer", "null"], "description": "1-based column of the field's first character. null = cursor position."}


def _schema(**properties: dict[str, Any]) -> dict[str, Any]:
    # Strict function-calling schema: every property required, optional ones nullable.
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "connect",
        "description": "Connect to the TN3270 (IBM 3270 green-screen) host and return the first screen. "
        "Leave host/port/use_tls null to use the configured system.",
        "parameters": _schema(
            host={"type": ["string", "null"], "description": "Host name or IP address. null = configured host."},
            port={"type": ["integer", "null"], "description": "TCP port. null = configured port."},
            use_tls={"type": ["boolean", "null"], "description": "Use TLS. null = configured setting."},
        ),
    },
    {
        "name": "read_screen",
        "description": "Return the current screen: rows, cursor, keyboard state and input fields.",
        "parameters": _schema(),
    },
    {
        "name": "type_text",
        "description": "Type text into an input field (local only - press an attention key such as enter to send). "
        "Replaces the field's current content unless clear_field is false. Never use for passwords.",
        "parameters": _schema(
            text={"type": "string", "description": "Text to type (single line; truncated to the field length)."},
            row=_ROW,
            column=_COLUMN,
            clear_field={"type": ["boolean", "null"], "description": "Erase the rest of the field first. null = true."},
        ),
    },
    {
        "name": "type_credential",
        "description": "Type the configured sign-on username or password into an input field without revealing it. "
        "The password can only be typed into a hidden (non-display) field.",
        "parameters": _schema(
            credential={"type": "string", "enum": ["username", "password"], "description": "Which credential to type."},
            row=_ROW,
            column=_COLUMN,
        ),
    },
    {
        "name": "press_key",
        "description": "Press a 3270 key and return the resulting screen. Attention keys (enter, clear, pf1-pf24, "
        "pa1-pa3) send the typed input to the host and wait for its reply.",
        "parameters": _schema(
            key={"type": "string", "description": f"Key name: {VALID_KEYS_HELP}. Example: pf3 for F3."},
        ),
    },
    {
        "name": "wait_for_text",
        "description": "Wait until the given text appears on the screen (case-insensitive), then return the screen.",
        "parameters": _schema(
            text={"type": "string", "description": "Text to wait for."},
            timeout_seconds={"type": ["number", "null"], "description": f"Maximum wait (up to {MAX_WAIT_SECONDS:g}). null = default."},
        ),
    },
    {
        "name": "disconnect",
        "description": "Close the TN3270 connection. Sign off the application first when it has a sign-off.",
        "parameters": _schema(),
    },
]
TOOL_NAMES = tuple(spec["name"] for spec in TOOL_SPECS)


class GreenScreenTools:
    """The agent's tools for one terminal session."""

    def __init__(self, settings: Settings | None = None, terminal: Tn3270Terminal | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.terminal = terminal or Tn3270Terminal(
            terminal_type=self.settings.terminal_type,
            screen_size=self.settings.screen_size,
            codepage=self.settings.codepage,
            timeout=self.settings.timeout,
            settle=self.settings.settle,
        )

    # ----------------------------------------------------------------- tools

    @_observed
    def connect(self, host: str | None = None, port: int | None = None, use_tls: bool | None = None) -> str:
        host = (host or self.settings.host).strip()
        tls = self.settings.tls if use_tls is None else bool(use_tls)
        port = int(port) if port is not None else (self.settings.port if tls == self.settings.tls else 992 if tls else 3270)
        if not 0 < port < 65536:
            raise ToolError(f"Invalid port {port}.")
        if not self.settings.is_allowed(host, port):
            raise ToolError(
                f"Connecting to {host}:{port} is not allowed. Allowed: "
                f"{', '.join(sorted(self.settings.allowed_hosts or {self.settings.host}))} (TN3270_ALLOWED_HOSTS)."
            )
        if self.terminal.connected:
            if self.terminal.address == f"{host}:{port}":
                return f"Already connected to {host}:{port}.\n{self.terminal.screen().render()}"
            raise ToolError(f"Already connected to {self.terminal.address}. Disconnect first.")
        screen, ready = self.terminal.connect(host, port, tls=tls, verify_tls=self.settings.tls_verify)
        note = "" if ready else " The host has not unlocked the keyboard yet - use wait_for_text or read_screen."
        return f"Connected to {host}:{port}{' using TLS' if tls else ''}.{note}\n{screen.render()}"

    @_observed
    def read_screen(self) -> str:
        return self.terminal.screen().render()

    @_observed
    def type_text(self, text: str, row: int | None = None, column: int | None = None,
                  clear_field: bool | None = None) -> str:
        result = self.terminal.type_text(text, row, column, clear_field=clear_field is not False)
        return self._typed_message(f"Typed {result.typed} of {result.requested} characters", result)

    @_observed
    def type_credential(self, credential: str, row: int | None = None, column: int | None = None) -> str:
        kind = str(credential).strip().lower()
        if kind not in ("username", "password"):
            raise ToolError('credential must be "username" or "password".')
        value = self.settings.username if kind == "username" else self.settings.password
        if not value:
            raise ToolError(
                f"No {kind} is configured (TN3270_{kind.upper()}). Ask the operator to configure it; "
                "do not ask for or guess credentials."
            )
        result = self.terminal.type_text(value, row, column, require_hidden=kind == "password")
        message = self._typed_message(f"Typed the configured {kind}", result)
        if result.typed < result.requested:
            message += f" WARNING: the field is shorter than the configured {kind}; it was truncated."
        return message

    @_observed
    def press_key(self, key: str) -> str:
        result = self.terminal.press(key)
        screen = result.screen
        if result.host_responded is None:
            status = f"Pressed {result.key} (local editing key, nothing was sent to the host)."
        elif not screen.connected:
            status = f"Pressed {result.key}. The host closed the connection."
        elif not result.host_responded:
            status = (f"Pressed {result.key}. The host has not finished (keyboard still locked after "
                      f"{self.terminal.timeout:g}s) - use wait_for_text or read_screen.")
        else:
            status = f"Pressed {result.key}."
        return f"{status}\n{screen.render()}"

    @_observed
    def wait_for_text(self, text: str, timeout_seconds: float | None = None) -> str:
        if not str(text).strip():
            raise ToolError("text must not be empty.")
        timeout = self.terminal.timeout if timeout_seconds is None else float(timeout_seconds)
        timeout = min(max(timeout, 0.0), MAX_WAIT_SECONDS)
        found, screen = self.terminal.wait_for_text(text, timeout)
        status = f'Found "{text}".' if found else f'"{text}" did not appear within {timeout:g}s.'
        return f"{status}\n{screen.render()}"

    @_observed
    def disconnect(self) -> str:
        address = self.terminal.address
        return f"Disconnected from {address}." if self.terminal.disconnect() else "Not connected."

    # -------------------------------------------------------------- dispatch

    def call(self, name: str, arguments: str | Mapping[str, Any] | None = None) -> str:
        """Run a tool by name with JSON (or dict) arguments; errors are returned as ``ERROR: ...``."""
        try:
            if name not in TOOL_NAMES:
                raise ToolError(f"Unknown tool {name!r}. Available tools: {', '.join(TOOL_NAMES)}.")
            if arguments is None:
                arguments = {}
            elif isinstance(arguments, str):
                arguments = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(arguments, Mapping):
                raise ToolError("Tool arguments must be a JSON object.")
            spec = next(s for s in TOOL_SPECS if s["name"] == name)
            unknown = set(arguments) - set(spec["parameters"]["properties"])
            if unknown:
                raise ToolError(f"Unknown argument(s) for {name}: {', '.join(sorted(unknown))}.")
            return getattr(self, name)(**arguments)
        except (ToolError, TerminalError, ValueError, TypeError) as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 - report unexpected failures to the model
            return f"ERROR: {type(exc).__name__}: {exc}"

    def close(self) -> None:
        self.terminal.close()

    def __enter__(self) -> GreenScreenTools:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _typed_message(prefix: str, result: Any) -> str:
        where = f"row {result.row}, col {result.col}"
        if result.field is not None:
            label = f' "{result.field.label}"' if result.field.label else ""
            where = f"input field [{result.field.index}]{label} at {where} (len {result.field.length})"
        message = f"{prefix} into {where}."
        if result.adjusted:
            message += " (The position was the field's attribute byte, so typing started one column later.)"
        return message + " Press an attention key (usually enter) to send it to the host."
