from __future__ import annotations

import socket

import pytest

from green_screen_agent.simulator import DEFAULT_USERS, BackgroundSimulator
from green_screen_agent.tools import GreenScreenTools, Settings

DEMO_USER = "DEMO"
DEMO_SECRET = DEFAULT_USERS[DEMO_USER]


def demo_settings(host: str, port: int, **env: str) -> Settings:
    """Settings for the simulator, built through the same path as the real configuration."""
    values = {
        "TN3270_HOST": host,
        "TN3270_PORT": str(port),
        "TN3270_USERNAME": DEMO_USER,
        "TN3270_PASSWORD": DEMO_SECRET,
        "TN3270_TIMEOUT": "10",
        "TN3270_SETTLE_SECONDS": "0.1",
    }
    values.update(env)
    return Settings.from_env(values, dotenv=False)


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def simulator():
    with BackgroundSimulator() as address:
        yield address


@pytest.fixture
def tools(simulator):
    host, port = simulator
    with GreenScreenTools(demo_settings(host, port)) as green_screen_tools:
        yield green_screen_tools
