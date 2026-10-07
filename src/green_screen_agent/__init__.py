"""Let AI agents operate IBM 3270 green-screen applications over TN3270."""

from .terminal import InputField, Screen, TerminalError, Tn3270Terminal
from .tools import TOOL_SPECS, GreenScreenTools, Settings, ToolError

__version__ = "0.1.0"

__all__ = [
    "GreenScreenTools",
    "InputField",
    "Screen",
    "Settings",
    "TOOL_SPECS",
    "TerminalError",
    "Tn3270Terminal",
    "ToolError",
    "__version__",
]
