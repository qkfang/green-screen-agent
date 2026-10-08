"""Live green-screen web view that shares one TN3270 session with MCP agents.

Standalone (the web view owns the connection; agents attach over MCP HTTP)::

    python -m green_screen_agent.live            # UI http://127.0.0.1:3271, MCP http://127.0.0.1:3271/mcp

Embedded in the stdio MCP server (the agent's process owns the connection)::

    python -m green_screen_agent.mcp_server --live

The browser receives every agent action as it happens over Server-Sent Events:
tool calls, each field typed (animated keystroke by keystroke), AID keys and the
screens the host sends back. A human operator can use the same session from the
page (keyboard, PF keys, connect/disconnect); those actions are tagged
``operator`` in the action log. Passwords never reach the browser: hidden fields
are blanked in screen snapshots and typing events.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import socket
import sys
import threading
from collections import deque
from importlib import resources
from typing import Any, AsyncIterator, Callable

import anyio.to_thread
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import __version__
from ..terminal import TerminalError, action_source
from ..tools import GreenScreenTools

log = logging.getLogger(__name__)

DEFAULT_PORT = 3271
KEEPALIVE_SECONDS = 15.0
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class EventHub:
    """Thread-safe fan-out of terminal events to any number of browser streams.

    Keeps the latest screen and recent tool calls so a newly opened page starts in sync.
    Repeated identical screens (e.g. from polling) are dropped.
    """

    def __init__(self, history: int = 400) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._screen: dict[str, Any] | None = None
        self._screen_dirty = False
        self._seq = 0
        self.info: dict[str, Any] = {}
        """Extra fields for the ``hello`` event (target host, MCP URL)."""

    @property
    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        with self._lock:
            if kind == "screen":
                if not self._screen_dirty and self._screen is not None and self._screen["screen"] == event["screen"]:
                    return
                self._screen_dirty = False
            elif kind in ("type", "key"):
                self._screen_dirty = True
            self._seq += 1
            event = {**event, "seq": self._seq}
            if kind == "screen":
                self._screen = event
            elif kind == "disconnected" and self._screen is not None:
                self._screen = {**self._screen, "screen": {**self._screen["screen"], "connected": False,
                                                           "keyboard_locked": False}}
            elif kind == "tool":
                self._history.append(event)
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # the stream's event loop has already closed
                pass

    def subscribe(self) -> tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Register the running event loop; returns the queue and the catch-up events."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
            initial = [{"type": "hello", "seq": self._seq, "version": __version__, **self.info},
                       *self._history]
            if self._screen is not None:
                initial.append(self._screen)
        return queue, initial

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = [(loop, q) for loop, q in self._subscribers if q is not queue]


def _as_operator(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    token = action_source.set("operator")
    try:
        return fn(*args, **kwargs)
    finally:
        action_source.reset(token)


def _hostname(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header[1:].split("]", 1)[0]
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def _request_allowed(request: Request, bind_host: str) -> bool:
    """Block DNS rebinding and cross-site requests: the page drives a live mainframe session."""
    host_header = request.headers.get("host", "")
    if bind_host not in ("0.0.0.0", "::"):
        allowed = _LOOPBACK | {bind_host} if bind_host in _LOOPBACK else {bind_host}
        if _hostname(host_header).lower() not in allowed:
            return False
    origin = request.headers.get("origin")
    if request.method != "GET" and origin is not None and origin != f"{request.url.scheme}://{host_header}":
        return False
    return True


def create_live_app(tools: GreenScreenTools, hub: EventHub, *, mcp_server: Any = None,
                    host: str = "127.0.0.1") -> Starlette:
    """Build the web app. With ``mcp_server`` the MCP streamable HTTP endpoint is served at ``/mcp``."""
    page = resources.files(__package__).joinpath("static", "index.html").read_text(encoding="utf-8")

    def guarded(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
        @functools.wraps(handler)
        async def wrapper(request: Request) -> Response:
            if not _request_allowed(request, host):
                await request.body()  # drain it, or Windows resets the socket before the 403 is read
                return JSONResponse({"ok": False, "error": "Forbidden host or origin."}, status_code=403)
            return await handler(request)

        return wrapper

    async def index(request: Request) -> Response:
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    async def events(request: Request) -> Response:
        queue, initial = hub.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                for event in initial:
                    yield _sse(event)
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(event)
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def run_tool(request: Request) -> Response:
        body = await _json_body(request)
        if body is None or not isinstance(body.get("name"), str):
            return JSONResponse({"ok": False, "error": 'Expected {"name": ..., "arguments": {...}}.'}, status_code=400)
        result = await anyio.to_thread.run_sync(
            functools.partial(_as_operator, tools.call, body["name"], body.get("arguments") or {}))
        return JSONResponse({"ok": not result.startswith("ERROR:"), "result": result})

    async def keyboard(request: Request) -> Response:
        """Raw operator keystrokes: ``{"text": "a"}``, ``{"key": "tab"}`` or ``{"cursor": [row, col]}``."""
        body = await _json_body(request)
        terminal = tools.terminal
        try:
            if body is None:
                raise ValueError("Expected a JSON object.")
            if isinstance(body.get("text"), str):
                call = functools.partial(terminal.type_text, body["text"], clear_field=False)
            elif isinstance(body.get("key"), str):
                call = functools.partial(terminal.press, body["key"])
            elif isinstance(body.get("cursor"), list) and len(body["cursor"]) == 2:
                call = functools.partial(terminal.move_cursor, int(body["cursor"][0]), int(body["cursor"][1]))
            else:
                raise ValueError('Expected "text", "key" or "cursor".')
            await anyio.to_thread.run_sync(functools.partial(_as_operator, call))
        except (TerminalError, ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})

    routes = [
        ("/", index, ["GET"]),
        ("/events", events, ["GET"]),
        ("/api/tool", run_tool, ["POST"]),
        ("/api/keyboard", keyboard, ["POST"]),
    ]
    if mcp_server is None:
        return Starlette(routes=[Route(path, guarded(fn), methods=methods) for path, fn, methods in routes])
    for path, fn, methods in routes:
        mcp_server.custom_route(path, methods=methods, include_in_schema=False)(guarded(fn))
    return mcp_server.streamable_http_app(host=host)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


class LiveView:
    """Wires a :class:`GreenScreenTools` session to the web view.

    Also polls the host about once a second while a page is open, so screens the host
    sends on its own (outside any agent action) still show up.
    """

    def __init__(self, tools: GreenScreenTools, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 mcp_server: Any = None, poll_interval: float = 1.0) -> None:
        self.tools = tools
        self.host = host
        self.port = port
        self.hub = EventHub()
        settings = tools.settings
        self.hub.info = {"target": f"{settings.host}:{settings.port}", "mcp": mcp_server is not None}
        self.app = create_live_app(tools, self.hub, mcp_server=mcp_server, host=host)
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._remove_listener = tools.terminal.add_listener(self.hub.publish)
        self._poller = threading.Thread(target=self._poll, name="tn3270-live-poll", daemon=True)
        self._poller.start()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{'127.0.0.1' if self.host in ('0.0.0.0', '::') else host}:{self.port}"

    def _poll(self) -> None:
        terminal = self.tools.terminal
        while not self._stop.wait(self._poll_interval):
            if not self.hub.has_subscribers:
                continue
            try:
                if terminal.connected:
                    terminal.screen()
            except TerminalError:
                pass
            except Exception:  # noqa: BLE001 - keep polling
                log.exception("Live view poll failed")

    def _bind(self, fallback: bool) -> socket.socket:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        if os.name != "nt":
            # Allow restarts while old connections linger in TIME_WAIT (on Windows this would allow port hijacking).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError:
            if not fallback:
                sock.close()
                raise
            log.warning("Port %s is in use; the live view uses a free port instead.", self.port)
            sock.bind((self.host, 0))
        self.port = sock.getsockname()[1]
        return sock

    def _uvicorn(self) -> uvicorn.Server:
        config = uvicorn.Config(self.app, log_level="warning", timeout_graceful_shutdown=1)
        self._server = uvicorn.Server(config)
        return self._server

    def serve(self) -> None:
        """Serve in the current thread until interrupted."""
        self._uvicorn().run(sockets=[self._bind(fallback=False)])

    def start(self, *, fallback_port: bool = True) -> str:
        """Serve in a daemon thread; returns the URL. Uses a free port if the port is taken."""
        sock = self._bind(fallback=fallback_port)
        server = self._uvicorn()
        self._thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                        name="tn3270-live-http", daemon=True)
        self._thread.start()
        return self.url

    def close(self) -> None:
        self._stop.set()
        self._remove_listener()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    from ..mcp_server import create_server

    parser = argparse.ArgumentParser(
        description="Live TN3270 green screen in the browser, with an MCP endpoint (/mcp) on the same session.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT}).")
    parser.add_argument("--connect", action="store_true", help="Connect to the configured TN3270 host on start.")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    tools = GreenScreenTools()
    live = LiveView(tools, host=args.host, port=args.port, mcp_server=create_server(tools))
    try:
        if args.connect:
            print(_as_operator(tools.call, "connect").splitlines()[0], file=sys.stderr)
        print(f"Live green screen: {live.url}/\nMCP endpoint (streamable HTTP): {live.url}/mcp", file=sys.stderr)
        live.serve()
    finally:
        live.close()
        tools.close()


if __name__ == "__main__":
    main()
