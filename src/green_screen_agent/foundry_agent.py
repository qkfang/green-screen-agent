"""Microsoft Foundry agent that operates a TN3270 green screen through function tools.

The agent is a Foundry *prompt agent* (``azure-ai-projects`` 2.x). Its tools are
plain function tools, so the model runs in Foundry while the 3270 session runs
here, next to the mainframe network: Foundry never needs a route to the host and
credentials never leave this process.

Usage::

    green-screen-agent -p "Sign on and tell me the status of customer 100003"
    green-screen-agent            # interactive chat
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable

from .prompts import AGENT_INSTRUCTIONS
from .tools import TOOL_SPECS, GreenScreenTools, Settings
from .terminal import AID_KEYS, TerminalError, normalize_key

DEFAULT_AGENT_NAME = "green-screen-agent"
DEFAULT_MAX_TOOL_ROUNDS = 40

Approver = Callable[[str, dict[str, Any]], bool]
EventSink = Callable[[str, str], None]


def function_tools() -> list[Any]:
    """The tool definitions as Foundry ``FunctionTool`` objects (strict JSON schemas)."""
    from azure.ai.projects.models import FunctionTool

    return [
        FunctionTool(name=spec["name"], description=spec["description"], parameters=spec["parameters"], strict=True)
        for spec in TOOL_SPECS
    ]


def agent_definition(model: str, instructions: str = AGENT_INSTRUCTIONS) -> Any:
    """Prompt-agent definition: model deployment + instructions + green-screen tools."""
    from azure.ai.projects.models import PromptAgentDefinition

    return PromptAgentDefinition(model=model, instructions=instructions, tools=function_tools())


def run_turn(
    openai_client: Any,
    *,
    agent_name: str,
    conversation_id: str,
    user_input: str,
    tools: GreenScreenTools,
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    approve: Approver | None = None,
    on_event: EventSink | None = None,
) -> str:
    """Send one user message to the agent and execute its tool calls until it answers.

    Tool calls from one model response run in order. If one fails, the rest of that
    batch is skipped so the model never submits a half-filled screen.
    """
    extra_body = {"agent_reference": {"name": agent_name, "type": "agent_reference"}}
    emit = on_event or (lambda kind, text: None)
    response = openai_client.responses.create(conversation=conversation_id, input=user_input, extra_body=extra_body)
    for _ in range(max_tool_rounds):
        _raise_for_failure(response)
        calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
        if not calls:
            return response.output_text
        outputs = []
        failed = False
        for call in calls:
            emit("call", f"{call.name}({call.arguments})")
            if failed:
                output = "ERROR: Skipped because an earlier tool call in the same batch failed."
            elif approve is not None and not approve(call.name, _loads(call.arguments)):
                output = "ERROR: The operator rejected this action. Stop and explain what you were about to do."
            else:
                output = tools.call(call.name, call.arguments)
            failed = failed or output.startswith("ERROR:")
            emit("output", output)
            outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
        response = openai_client.responses.create(conversation=conversation_id, input=outputs, extra_body=extra_body)
    _raise_for_failure(response)
    raise RuntimeError(f"The agent did not finish within {max_tool_rounds} tool rounds.")


def _loads(arguments: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _raise_for_failure(response: Any) -> None:
    if getattr(response, "status", None) == "failed":
        raise RuntimeError(f"The agent response failed: {getattr(response, 'error', None)}")


def confirm_attention_keys(tools: GreenScreenTools, ask: Callable[[str], str] = input) -> Approver:
    """Human-in-the-loop approver: ask before any key that sends data to the host."""

    def approve(name: str, arguments: dict[str, Any]) -> bool:
        if name != "press_key":
            return True
        try:
            key = normalize_key(str(arguments.get("key", "")))
        except TerminalError:
            return True  # invalid key: the tool call will fail without touching the host
        if key not in AID_KEYS:
            return True
        try:
            screen = tools.read_screen()
        except TerminalError:
            screen = "(not connected)"
        print(f"\n{screen}\n", file=sys.stderr)
        answer = ask(f"Agent wants to press {key.upper()} and send this screen to the host. Allow? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    return approve


def _print_event(kind: str, text: str) -> None:
    prefix = "-> tool" if kind == "call" else "<- result"
    if kind == "output" and len(text) > 2500:
        text = text[:2500] + "\n... (truncated)"
    print(f"[{prefix}] {text}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Microsoft Foundry agent that operates a TN3270 green screen.",
        epilog="Settings: FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_MODEL_NAME and TN3270_* (environment or .env).",
    )
    parser.add_argument("-p", "--prompt", help="Run one task and exit (default: interactive chat).")
    parser.add_argument("--endpoint", help="Foundry project endpoint (FOUNDRY_PROJECT_ENDPOINT).")
    parser.add_argument("--model", help="Model deployment name (FOUNDRY_MODEL_NAME).")
    parser.add_argument("--agent-name", help=f"Agent name (FOUNDRY_AGENT_NAME, default {DEFAULT_AGENT_NAME}).")
    parser.add_argument("--keep-agent", action="store_true", help="Keep the agent version in Foundry on exit.")
    parser.add_argument("--max-tool-rounds", type=int, default=DEFAULT_MAX_TOOL_ROUNDS)
    parser.add_argument("--confirm-keys", action="store_true",
                        help="Ask for approval before every key that sends data to the host.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print tool calls and screens to stderr.")
    args = parser.parse_args(argv)

    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    endpoint = args.endpoint or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    model = args.model or os.environ.get("FOUNDRY_MODEL_NAME")
    agent_name = args.agent_name or os.environ.get("FOUNDRY_AGENT_NAME") or DEFAULT_AGENT_NAME
    if not endpoint or not model:
        parser.error("Set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL_NAME (or use --endpoint/--model).")
    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError:
        parser.error("The Foundry SDK is not installed. Run: pip install -e '.[foundry]'")

    tools = GreenScreenTools(Settings.from_env())
    approve = confirm_attention_keys(tools) if args.confirm_keys else None
    on_event = _print_event if args.verbose else None
    with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=endpoint, credential=credential) as project,
        project.get_openai_client() as openai_client,
        tools,
    ):
        agent = project.agents.create_version(agent_name=agent_name, definition=agent_definition(model))
        print(f"Agent {agent.name} version {agent.version} ({model}) is ready.", file=sys.stderr)
        conversation = openai_client.conversations.create()
        try:
            prompts = [args.prompt] if args.prompt else None
            while True:
                if prompts is not None:
                    if not prompts:
                        break
                    user_input = prompts.pop()
                else:
                    try:
                        user_input = input("\nYou> ").strip()
                    except EOFError:
                        break
                    if user_input.lower() in ("exit", "quit"):
                        break
                    if not user_input:
                        continue
                answer = run_turn(
                    openai_client,
                    agent_name=agent.name,
                    conversation_id=conversation.id,
                    user_input=user_input,
                    tools=tools,
                    max_tool_rounds=args.max_tool_rounds,
                    approve=approve,
                    on_event=on_event,
                )
                print(f"\nAgent> {answer}")
        finally:
            openai_client.conversations.delete(conversation_id=conversation.id)
            if not args.keep_agent:
                project.agents.delete_version(agent_name=agent.name, agent_version=agent.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
