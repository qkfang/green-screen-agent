"""Asyncio TN3270 server hosting the demo application.

Run it with ``green-screen-simulator`` (or ``python -m green_screen_agent.simulator``)
and connect any 3270 emulator (x3270, c3270, wc3270, IBM tnz, ...) or the
agent tools to ``127.0.0.1:3270``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading

from .app import DEFAULT_USERS, CustomerStore, DemoApplication
from .datastream import (
    DO,
    DONT,
    IAC,
    OPT_BINARY,
    OPT_EOR,
    OPT_TERMINAL_TYPE,
    SB,
    SE,
    TTYPE_IS,
    TTYPE_SEND,
    WILL,
    WONT,
    TelnetParser,
    frame_record,
    parse_inbound,
)

log = logging.getLogger(__name__)

NEGOTIATION_TIMEOUT = 10.0


class NegotiationError(Exception):
    """The client did not complete TN3270 Telnet negotiation."""


class _Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.parser = TelnetParser()
        self.pending: list[tuple[str, object]] = []

    async def next_event(self) -> tuple[str, object]:
        while not self.pending:
            data = await self.reader.read(4096)
            if not data:
                raise ConnectionError("client closed the connection")
            self.pending.extend(self.parser.feed(data))
        return self.pending.pop(0)

    def send(self, data: bytes) -> None:
        self.writer.write(data)

    async def negotiate(self) -> str:
        """Negotiate TERMINAL-TYPE, EOR and BINARY; return the terminal type."""
        self.send(bytes([IAC, DO, OPT_TERMINAL_TYPE]))
        await self.writer.drain()
        terminal_type = None
        agreed: set[tuple[str, int]] = set()
        needed = {("will", OPT_EOR), ("do", OPT_EOR), ("will", OPT_BINARY), ("do", OPT_BINARY)}
        while terminal_type is None or not needed <= agreed:
            kind, value = await self.next_event()
            if kind == "will" and value == OPT_TERMINAL_TYPE:
                self.send(bytes([IAC, SB, OPT_TERMINAL_TYPE, TTYPE_SEND, IAC, SE]))
            elif kind == "wont" and value == OPT_TERMINAL_TYPE:
                raise NegotiationError("client refused TERMINAL-TYPE (not a 3270 emulator?)")
            elif kind == "sb" and isinstance(value, bytes) and value[:2] == bytes([OPT_TERMINAL_TYPE, TTYPE_IS]):
                terminal_type = value[2:].decode("ascii", "replace")
                self.send(
                    bytes([IAC, DO, OPT_EOR, IAC, WILL, OPT_EOR, IAC, DO, OPT_BINARY, IAC, WILL, OPT_BINARY])
                )
            elif kind in ("will", "do") and isinstance(value, int):
                if value in (OPT_EOR, OPT_BINARY):
                    agreed.add((kind, value))
                elif kind == "will":
                    self.send(bytes([IAC, DONT, value]))
                else:
                    self.send(bytes([IAC, WONT, value]))
            elif kind in ("wont", "dont") and value in (OPT_EOR, OPT_BINARY):
                raise NegotiationError("client refused EOR/BINARY (TN3270 requires both)")
            await self.writer.drain()
        return terminal_type


class SimulatorServer:
    """TN3270 server running :class:`DemoApplication` for every connection."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3270,
        *,
        users: dict[str, str] | None = None,
        store: CustomerStore | None = None,
        latency: float = 0.0,
    ) -> None:
        self.host = host
        self.port = port
        self.users = dict(users or DEFAULT_USERS)
        self.store = store or CustomerStore()
        self.latency = latency
        self._server: asyncio.Server | None = None
        self._sessions: set[asyncio.Task] = set()

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockname = self._server.sockets[0].getsockname()
        self.port = sockname[1]
        log.info("TN3270 simulator listening on %s:%s", self.host, self.port)
        return self.host, self.port

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for task in list(self._sessions):
            task.cancel()
        await asyncio.gather(*self._sessions, return_exceptions=True)
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        conn = _Connection(reader, writer)
        task = asyncio.current_task()
        if task is not None:
            self._sessions.add(task)
            task.add_done_callback(self._sessions.discard)
        try:
            terminal_type = await asyncio.wait_for(conn.negotiate(), NEGOTIATION_TIMEOUT)
            log.info("client %s connected as %s", peer, terminal_type)
            app = DemoApplication(self.store, self.users)
            conn.send(frame_record(app.start().to_record()))
            await writer.drain()
            while not app.closed:
                kind, value = await conn.next_event()
                if kind != "record" or not isinstance(value, bytes) or not value:
                    continue
                inbound = parse_inbound(value)
                if self.latency:
                    await asyncio.sleep(self.latency)
                screen = app.handle(inbound)
                conn.send(frame_record(screen.to_record()))
                await writer.drain()
        except (ConnectionError, NegotiationError, asyncio.TimeoutError) as exc:
            log.info("client %s disconnected: %s", peer, exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


class BackgroundSimulator:
    """Run a :class:`SimulatorServer` on a background thread (tests, notebooks).

    >>> with BackgroundSimulator(port=0) as (host, port):
    ...     pass  # connect a terminal to host:port
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, **kwargs) -> None:
        self.server = SimulatorServer(host, port, **kwargs)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="tn3270-simulator", daemon=True)

    def start(self) -> tuple[str, int]:
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self.server.start(), self._loop)
        return future.result(timeout=10)

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()

    def __enter__(self) -> tuple[str, int]:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="green-screen-simulator",
        description="TN3270 host simulator with a CICS-style demo application.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=3270, help="TCP port (default: 3270)")
    parser.add_argument("--userid", default="DEMO", help="sign-on userid (default: DEMO)")
    parser.add_argument("--password", default="DEMO123", help="sign-on password (default: DEMO123)")
    parser.add_argument("--latency", type=float, default=0.0, help="simulated host response time in seconds")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = SimulatorServer(args.host, args.port, users={args.userid: args.password}, latency=args.latency)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
