# green-screen-agent

Let AI agents operate IBM 3270 **green-screen** (TN3270) applications directly over the
TN3270 protocol. No screenshots, OCR or emulator GUI: the agent reads the 3270 screen
buffer (text + input fields) and types into fields / presses AID keys like an operator.

The same tool set is exposed two ways:

| Front end | How | Module |
|---|---|---|
| **Microsoft Foundry agent** (Python, `azure-ai-projects` 2.x prompt agent) | function tools executed locally | `green_screen_agent.foundry_agent` |
| **GitHub Copilot CLI / VS Code** | MCP server (stdio) | `green_screen_agent.mcp_server` |

A **TN3270 host simulator** with a small CICS-style customer application is included for
testing, and the tools were also verified against a real MVS 3.8j system (TK5 in Docker).
See [docs/research.md](docs/research.md) for the options that were evaluated.

Tools: `connect`, `read_screen`, `type_text`, `type_credential`, `press_key`,
`wait_for_text`, `disconnect`.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[foundry,dev]"
cp .env.example .env          # set TN3270_USERNAME=DEMO and TN3270_PASSWORD=DEMO123 for the simulator

python -m green_screen_agent.simulator          # TN3270 simulator on 127.0.0.1:3270 (sign on DEMO / DEMO123)
pytest                                           # unit + end-to-end tests (simulator, MCP, Foundry loop)
```

Any 3270 emulator (x3270, c3270, wc3270) can connect to the simulator to see the same screens.

### GitHub Copilot CLI

`.mcp.json` in the repository root registers the `tn3270` MCP server (Copilot CLI loads it
for trusted folders). Start Copilot from the repo with the package installed:

```bash
copilot --allow-tool='tn3270'
> Connect to the green screen, sign on, and tell me the status and balance of customer 100003.
```

For `copilot -p` (non-interactive) set `GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP=true`.
In VS Code, `.vscode/mcp.json` starts the same server (agent mode) using `.env`.

### Microsoft Foundry agent

Needs a Foundry project, a model deployment and `az login` (DefaultAzureCredential):

```bash
export FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
export FOUNDRY_MODEL_NAME=gpt-4.1
green-screen-agent -v -p "Sign on and list the customers that are on HOLD"
green-screen-agent --confirm-keys        # interactive; approve every key sent to the host
```

The agent version is created on start and deleted on exit (`--keep-agent` keeps it).
The model runs in Foundry; the 3270 session and credentials stay in this process.

### Real mainframe for testing (MVS 3.8j TK5 + KICKS)

```bash
docker run -d --name mvs-kicks -p 3270:3270 -p 8038:8038 backscratcher/tk5-mvs-kicks:latest
# wait ~2 minutes for the IPL; users HERC01 / CUL8TR
TN3270_USERNAME=HERC01 TN3270_PASSWORD=CUL8TR copilot --allow-tool='tn3270'
```

Always `LOGOFF` from TSO - a dropped connection leaves the user "in use" until restart.

## Security notes

- Passwords are typed by `type_credential` from configuration, only into non-display
  fields, and are never returned to the model. Use a least-privilege service user.
- `connect` only reaches `TN3270_HOST` unless `TN3270_ALLOWED_HOSTS` allows more.
- Screen text is untrusted input (prompt injection); the instructions tell the model to
  treat it as data. Use `--confirm-keys` (Foundry) or Copilot's tool approvals for
  write operations, and TLS (`TN3270_TLS=true`, port 992) outside a lab.
