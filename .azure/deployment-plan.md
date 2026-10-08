# Azure Deployment Plan

> **Status:** Deployed

Generated: 2026-10-08

---

## 1. Project Overview

**Goal:** Run the TK5 MVS 3.8j + KICKS mainframe (`backscratcher/tk5-mvs-kicks:latest`) and the
green-screen agent live server (browser green screen + MCP streamable HTTP at `/mcp`) on Azure
Container Apps, deployed with PowerShell + Azure CLI scripts in `scripts/`.

**Path:** Add Components (MODIFY - existing Python project, no existing Azure infra)

---

## 2. Requirements

| Attribute | Value |
|-----------|-------|
| Classification | Development / demo |
| Scale | Small - single replica each (in-memory terminal session; single mainframe) |
| Budget | Cost-optimized (Consumption Container Apps, Basic ACR) |
| **Subscription** | sub-MCAP951655-1 (`873a4995-e21b-47e2-953e-f2e88e2fa4f9`) - confirmed by user |
| **Location** | australiaeast - confirmed by user |
| Resource group | `rg-green-screen-agent` (new) |

---

## 3. Components Detected

| Component | Type | Technology | Path |
|-----------|------|------------|------|
| TK5 MVS + KICKS | TN3270 host (TCP 3270) | Prebuilt Docker image, linux/amd64 | `backscratcher/tk5-mvs-kicks:latest` |
| green-screen live | Web UI + MCP HTTP (port 3271) | Python 3.12, Starlette/uvicorn, mcp 2.x, tnz | `src/green_screen_agent/live` |

The Foundry agent (`green-screen-agent` CLI) is a client that runs tools in-process; it is not a
server, so it is not deployed. Copilot CLI / VS Code connect to the deployed `/mcp` endpoint.

---

## 4. Recipe Selection

**Selected:** Bicep (shared infrastructure in `bicep/main.bicep`) + AZCLI PowerShell scripts

**Rationale:** User requested the Azure resources as Bicep in a `bicep/` folder, and three
scripts in `scripts/`: (1) deploy the Bicep (resource group + shared resources), (2) TK5
container app, (3) green-screen agent container app. The app scripts read the Bicep deployment
outputs (`green-screen-agent-infra`) and use idempotent `az containerapp` commands, because
images must be imported/built in ACR before the apps are created.

---

## 5. Architecture

**Stack:** Containers (Azure Container Apps)

### Service Mapping

| Component | Azure Service | SKU / Config |
|-----------|---------------|--------------|
| TK5 MVS + KICKS | Container App `tk5-mvs-kicks` | 2 vCPU / 4 GiB, min=max=1, **external TCP ingress** 3270 (exposed 3270), IP allowlist |
| green-screen live | Container App `green-screen-live` | 0.5 vCPU / 1 GiB, min=max=1, **external HTTPS ingress** -> 3271 (open to internet - user choice) |

### Supporting Services

| Service | Purpose |
|---------|---------|
| Log Analytics | Container Apps environment logs |
| Container Apps environment (Consumption, VNet-integrated) | Shared environment on subnet `gsa-vnet/container-apps` (10.42.0.0/23); the agent reaches `tk5-mvs-kicks:3270` internally |
| Azure Container Registry (Basic, admin disabled) | Stores the built agent image and an imported copy of the TK5 image (avoids Docker Hub rate limits) |
| User-assigned managed identity + AcrPull | Image pulls without registry passwords |
| Container App secret | `TN3270_PASSWORD` (never in plain env vars) |

### Networking / security notes

- External TCP ingress needs a custom VNet, so the environment is VNet-integrated (a VNet
  cannot be added later; `deploy-azure-resources.ps1` recreates an environment without one).
  TN3270 port 3270 is public at `<fqdn>:3270` but restricted to the deployer's public IP
  (`-AllowedIpRange`) plus the environment subnet.
- The live app listens on `0.0.0.0` (MCP DNS-rebinding guard is localhost-only) with
  `FORWARDED_ALLOW_IPS=*` so uvicorn sees the `https` scheme from the ACA proxy and the
  page's Origin check passes.
- **Risk accepted by user:** the live UI and `/mcp` have no authentication and are public.
  Anyone with the URL can drive the TK5 session. Recommended follow-up: IP restrictions or
  Container Apps authentication.

---

## 6. Provisioning Limit Checklist

| Resource Type | Number to Deploy | Total After Deployment | Limit/Quota | Notes |
|---------------|------------------|------------------------|-------------|-------|
| Microsoft.App/managedEnvironments (australiaeast) | 1 | 1 | 15 per region (default) | Existing: 0 (Resource Graph) |
| Container App vCPU (Consumption) | 2.5 | 2.5 | 100 cores / environment (default) | Within limits |
| Microsoft.ContainerRegistry/registries | 1 | n/a | Well within default | Basic SKU |
| Microsoft.OperationalInsights/workspaces | 1 | n/a | Well within default | |

**Status:** ✅ All resources within limits. Providers Microsoft.App, Microsoft.ContainerRegistry,
Microsoft.OperationalInsights are registered.

---

## 7. Execution Checklist

### Phase 1: Planning
- [x] Analyze workspace
- [x] Gather requirements
- [x] Confirm subscription and location with user
- [x] Prepare resource inventory and check limits
- [x] Scan codebase
- [x] Select recipe (AZCLI)
- [x] Plan architecture
- [x] **User approved this plan**

### Phase 2: Execution
- [x] `Dockerfile` + `.dockerignore` for the green-screen live server
- [x] `bicep/main.bicep` (+ `main.bicepparam`) - Log Analytics, ACR, identity + AcrPull, Container Apps environment
- [x] `scripts/deploy-azure-resources.ps1` - create RG, deploy `bicep/main.bicep`
- [x] `scripts/deploy-tk5.ps1` - import TK5 image to ACR, create/update `tk5-mvs-kicks`
- [x] `scripts/deploy-green-screen-agent.ps1` - `az acr build`, create/update `green-screen-live`
- [x] `scripts/azure-common.ps1` - shared helpers (az wrapper, reads Bicep deployment outputs)
- [x] README Azure section
- [x] Security hardening review (no ACR admin user, managed-identity pulls, password as secret, non-root container, TN3270 port IP-restricted)
- [x] Local functional check: live server on 0.0.0.0 with proxy headers - UI 200, HTTPS-origin POST accepted, MCP initialize OK (local docker build blocked by this machine's PyPI TLS; image builds in ACR)
- [x] **⛔ Update plan status to "Ready for Validation"**

### Phase 3: Validation
- [x] Invoke azure-validate skill
- [x] All validation checks pass
  - [x] 1. Core Validation (CLI, auth, build, validate, what-if) - `bicep/main.bicep`, RG scope
  - [x] 2. Linting (`az bicep lint`)
  - [x] 3. Docker Build (image is built remotely by `az acr build`; local Docker blocked by PyPI TLS)
  - [x] 4. Azure Policy Validation
  - [x] 5. Role verification

### Validation Proof

| Check | Result |
|-------|--------|
| validate-deployment.ps1 (group scope, rg-green-screen-agent) | OVERALL: PASS - CLI ✅ auth ✅ build ✅ validate ✅ what-if ✅ (Create 6, Modify 0, Delete 0) |
| `az bicep lint` | No warnings/errors |
| Docker | Local build blocked by this machine's TLS to PyPI (network, not Dockerfile); app runtime verified locally with venv (UI 200, HTTPS-origin POST OK, MCP initialize OK); image built in ACR during deploy |
| Azure Policy | No deny policies affect the template (ARM validate passed); assignments are audit/DINE for VMs, SQL, activity logs |
| Roles | Signed-in user is Owner on the subscription (can create AcrPull role assignment) |
| Build verification | `pytest`: 71 passed, 1 skipped (Foundry SDK not installed) |
| Static RBAC | `AcrPull` for `gsa-identity` scoped to the registry; both apps pull with that identity |

Validated: 2026-10-08

### Phase 4: Deployment
- [x] Run the three scripts (azure-deploy)
- [x] Verify live UI loads and `connect` reaches TK5 internally (GET / 200, `connect` -> TK5 logo screen from `tk5-mvs-kicks:3270`, MCP initialize 200)
- [x] Live RBAC: `gsa-identity` has only AcrPull on `gsaacrjge77rrntjyto`

Endpoints: https://green-screen-live.bravewater-5b1dc3d4.australiaeast.azurecontainerapps.io/ (UI),
https://green-screen-live.bravewater-5b1dc3d4.australiaeast.azurecontainerapps.io/mcp (MCP)

---

## 8. Files to Generate

| File | Purpose | Status |
|------|---------|--------|
| `.azure/deployment-plan.md` | This plan | ✅ |
| `bicep/main.bicep`, `bicep/main.bicepparam` | Shared Azure resources | ✅ |
| `Dockerfile` | Container image for `green-screen-live` | ✅ |
| `.dockerignore` | Keep build context small, exclude `.env`/venv | ✅ |
| `scripts/azure-common.ps1` | Shared script helpers | ✅ |
| `scripts/deploy-azure-resources.ps1` | RG + Bicep deployment | ✅ |
| `scripts/deploy-tk5.ps1` | TK5 MVS + KICKS container app | ✅ |
| `scripts/deploy-green-screen-agent.ps1` | Green-screen live app + MCP container app | ✅ |
| `README.md` | Document the Azure deployment | ✅ |

---

## 9. Next Steps

1. User approves plan
2. Generate artifacts
3. azure-validate
4. azure-deploy (run scripts)
