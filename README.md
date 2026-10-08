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

These are terminal primitives, not application actions. Copilot is not given simulator
objects, customer operations, menu metadata or a side-channel API. The simulator exposes
only its TN3270 TCP listener: it encodes each screen as a 3270 data stream and changes
application state only after parsing an inbound TN3270 record. Consequently, the agent
must discover options from the actual received screen buffer, decide what to do, type into
3270 fields, send an AID key, and verify the next received screen. The same path is used
against the simulator and a real mainframe.

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

The Docker image is `linux/amd64` only. On Windows PowerShell, use the launcher below;
it explicitly enables Docker's x86-64 emulation when the host is ARM64:

```powershell
.\scripts\run-tn3270.ps1
# Wait ~2 minutes for the IPL; users HERC01 / CUL8TR
$env:TN3270_USERNAME="HERC01"
$env:TN3270_PASSWORD="CUL8TR"
copilot --allow-tool='tn3270'
```

The equivalent Docker command is:

```powershell
docker run -d --platform linux/amd64 --name mvs-kicks -p 3270:3270 -p 8038:8038 backscratcher/tk5-mvs-kicks:latest
```

On macOS or Linux:

```bash
docker run -d --platform linux/amd64 --name mvs-kicks -p 3270:3270 -p 8038:8038 backscratcher/tk5-mvs-kicks:latest
TN3270_USERNAME=HERC01 TN3270_PASSWORD=CUL8TR copilot --allow-tool='tn3270'
```

Always `LOGOFF` from TSO - a dropped connection leaves the user "in use" until restart.

Port 3270 opens the TK5 VTAM `Logon ===>` screen, not CICS. KICKS (the CICS-compatible
transaction monitor) runs inside a TSO session, so start it from there:

1. At `Logon ===>` sign on as `HERC01` / `CUL8TR`. This lands in the ISPF menu.
2. Exit ISPF (`X` or PF3) to the TSO `READY` prompt.
3. Enter `EXEC KICKSSYS.V1R5M0.CLIST(KICKS)`. The KICKS logo (KSGM) appears.
4. Press CLEAR until the screen is blank, type a transaction id at the top left and press
   Enter: `BTC0` (Nevada Dept. of Labor demo), `MENU` (Murach customer sample) or `KSGM`.
5. To leave, clear the screen and enter `KSSF` (back to `READY`), then `LOGOFF`.

## Security notes

- Passwords are typed by `type_credential` from configuration, only into non-display
  fields, and are never returned to the model. Use a least-privilege service user.
- `connect` only reaches `TN3270_HOST` unless `TN3270_ALLOWED_HOSTS` allows more.
- Screen text is untrusted input (prompt injection); the instructions tell the model to
  treat it as data. Use `--confirm-keys` (Foundry) or Copilot's tool approvals for
  write operations, and TLS (`TN3270_TLS=true`, port 992) outside a lab.
