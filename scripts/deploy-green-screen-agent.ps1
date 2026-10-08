<#
.SYNOPSIS
    Builds and deploys the green-screen live server (web UI + MCP endpoint) to Azure Container Apps.

.DESCRIPTION
    Builds the repository Dockerfile in Azure Container Registry (no local Docker needed) and creates
    or updates a single-replica container app with public HTTPS ingress. The app serves the browser
    green screen at / and MCP streamable HTTP at /mcp, both on one TN3270 session to the TK5 app.
    The TN3270 password is stored as a Container Apps secret.
    Run deploy-azure-resources.ps1 and deploy-tk5.ps1 first.

    WARNING: the live UI and /mcp have no authentication. Anyone with the URL can drive the session.
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-green-screen-agent",
    [string]$Subscription,
    [ValidatePattern("^[a-z][a-z0-9-]{0,30}[a-z0-9]$")]
    [string]$AppName = "green-screen-live",
    # TN3270 target: the TK5 container app inside the same environment.
    [string]$Tn3270Host = "tk5-mvs-kicks",
    [ValidateRange(1, 65535)]
    [int]$Tn3270Port = 3270,
    [string]$Tn3270Username = $(if ($env:TN3270_USERNAME) { $env:TN3270_USERNAME } else { "HERC01" }),
    # Defaults to $env:TN3270_PASSWORD; prompted on first deployment if neither is set.
    [SecureString]$Tn3270Password,
    [string]$ImageTag
)

. "$PSScriptRoot\azure-common.ps1"

Initialize-AzCli $Subscription | Out-Null
$shared = Get-SharedResources $ResourceGroup
$repoRoot = Split-Path -Parent $PSScriptRoot
$exists = Test-Az containerapp show -n $AppName -g $ResourceGroup

$password = if ($Tn3270Password) { [Net.NetworkCredential]::new("", $Tn3270Password).Password } else { $env:TN3270_PASSWORD }
if (-not $password -and -not $exists) {
    $secure = Read-Host "TN3270 password for $Tn3270Username" -AsSecureString
    $password = [Net.NetworkCredential]::new("", $secure).Password
    if (-not $password) { throw "A TN3270 password is required for the first deployment." }
}

if (-not $ImageTag) {
    $commit = & { $ErrorActionPreference = "Continue"; & git -C $repoRoot rev-parse --short HEAD 2>$null }
    $ImageTag = (@($commit, (Get-Date -Format "yyyyMMddHHmmss")) | Where-Object { $_ }) -join "-"
}
$image = "$($shared.LoginServer)/green-screen-agent:$ImageTag"

Write-Step "Building $image in Azure Container Registry"
# --no-logs still waits for the run; streaming logs crashes az on Windows consoles with non-cp1252 output.
$run = Invoke-Az acr build --registry $shared.Registry --image "green-screen-agent:$ImageTag" --image "green-screen-agent:latest" `
    --platform linux/amd64 --no-logs --query "{id:runId,status:status}" -o json $repoRoot | ConvertFrom-Json
if ($run.status -ne "Succeeded") {
    throw "Image build $($run.id) $($run.status). Logs: az acr task logs -r $($shared.Registry) --run-id $($run.id)"
}

$envVars = @(
    "TN3270_HOST=$Tn3270Host",
    "TN3270_PORT=$Tn3270Port",
    "TN3270_USERNAME=$Tn3270Username",
    "TN3270_PASSWORD=secretref:tn3270-password"
)

if ($exists) {
    if ($password) {
        Write-Step "Updating TN3270 password secret"
        Invoke-Az containerapp secret set -n $AppName -g $ResourceGroup --secrets "tn3270-password=$password" -o none
    }
    Write-Step "Updating container app '$AppName'"
    Invoke-Az containerapp update -n $AppName -g $ResourceGroup --image $image --set-env-vars @envVars `
        --min-replicas 1 --max-replicas 1 -o none
}
else {
    Write-Step "Creating container app '$AppName'"
    # One replica: the terminal session lives in process memory and is shared by the UI and /mcp.
    Invoke-Az containerapp create -n $AppName -g $ResourceGroup --environment $shared.EnvironmentId `
        --image $image --registry-server $shared.LoginServer --registry-identity $shared.IdentityId `
        --user-assigned $shared.IdentityId --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 1 `
        --ingress external --target-port 3271 --secrets "tn3270-password=$password" --env-vars @envVars `
        --tags app=green-screen-agent -o none
}

$fqdn = Invoke-Az containerapp show -n $AppName -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host ""
Write-Host "Green-screen live server deployed ($image)." -ForegroundColor Green
Write-Host "  Live green screen : https://$fqdn/"
Write-Host "  MCP endpoint      : https://$fqdn/mcp"
Write-Host "  TN3270 target     : ${Tn3270Host}:$Tn3270Port as $Tn3270Username"
Write-Host "Copilot CLI .mcp.json: `"tn3270`": {`"type`": `"http`", `"url`": `"https://$fqdn/mcp`", `"tools`": [`"*`"]}"
