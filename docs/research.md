# Research: AI agents for TN3270 green screens

**Context:** users work in TN3270 green screens; systems integrate through MQ into
Evolve/mainframe; SSH is not available. Goal: a Python Microsoft Foundry agent that
operates the green screen, ideally with GitHub Copilot CLI driving the protocol directly.

## Options

| Option | How it works | Verdict |
|---|---|---|
| **Direct TN3270 (chosen)** | Headless 3270 emulator in Python; the agent gets the screen buffer as text plus the field map and sends keystrokes/AIDs. | Exact text, field boundaries, protected/hidden attributes and keyboard lock state; fast, cheap, deterministic, testable. Needs network reach to the TN3270 server (port 23/992). |
| Computer use (Foundry `computer-use-preview`) | Model looks at screenshots of an emulator and clicks/types. | Works with any UI but slower, costlier, limited-access model, OCR-like errors on dense screens, needs a VM/desktop. Keep as fallback only. |
| MQ → Evolve/mainframe | Existing system-to-system path. | Best for well-defined transactions (no UI). Use for production automation where a message interface exists; green-screen automation covers the gaps. |
| SSH / USS shell | Not available here; also not the user interface. | Out of scope. |

**Copilot CLI "hooking into the protocol":** Copilot CLI cannot speak TN3270 itself, but it
can call MCP servers. The `tn3270` MCP server in this repo gives Copilot (CLI, VS Code agent
mode, coding agent) the same direct-protocol tools as the Foundry agent.

## Python TN3270 libraries

| Library | Notes |
|---|---|
| **tnz** (IBM, Apache-2.0) | Pure Python, maintained, TLS, TN3270E, field API. **Used.** |
| py3270 / x3270 `s3270` | Python wrapper around the s3270 binary (extra native dependency). |
| tn3270lib | GPL, low-level. |

## Simulators / test hosts

- **Bundled simulator** (`python -m green_screen_agent.simulator`): CICS-style sign-on,
  menu, customer inquiry/update and paged list; used by the automated tests.
- **MVS 3.8j TK5 + KICKS** (`backscratcher/tk5-mvs-kicks`): real VTAM/TSO/ISPF. Verified:
  logon HERC01 (password typed into the non-display field), `***` continuation, ISPF menu,
  PF3 to READY, LOGOFF. KICKS (CICS-compatible) is included in the image.
- `mainframed767/mvsce` (MVS/CE) is an alternative image.

## Foundry integration

`azure-ai-projects` 2.x prompt agent (`PromptAgentDefinition`) with strict `FunctionTool`s;
the client runs the Responses API loop (`agent_reference`, conversations) and executes tool
calls locally. Alternatives: expose the MCP server over HTTP and attach it as a Foundry MCP
tool (needs authentication and network exposure), or package as a hosted agent.

## Risks and controls

Credentials from configuration/Key Vault only, typed into hidden fields; host allow-list;
human approval for attention keys; prompt-injection guidance; TLS; audit tool calls (`-v`).
