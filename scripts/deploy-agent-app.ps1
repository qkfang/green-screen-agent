<#
.SYNOPSIS
    Builds and deploys the Foundry agent app (src\agent: web UI + .NET prompt agent) to Azure Container Apps.

.DESCRIPTION
    Builds src\agent\Dockerfile in Azure Container Registry and creates or updates a container app that
    uses the shared managed identity (Azure AI User on the Foundry account) to create the prompt agent in
    the Foundry project and run it. The agent calls the MCP endpoint of the green-screen-live app.
    Run deploy-azure-resources.ps1 and deploy-green-screen-agent.ps1 first.

    WARNING: the app has no authentication. Anyone with the URL can make the agent drive the TN3270 session.
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-green-screen-agent",
    [string]$Subscription,
    [ValidatePattern("^[a-z][a-z0-9-]{0,30}[a-z0-9]$")]
    [string]$AppName = "green-screen-agent-app",
    # The container app that serves the MCP endpoint (/mcp) and the live green screen.
    [string]$LiveAppName = "green-screen-live",
    [string]$ImageTag
)

. "$PSScriptRoot\azure-common.ps1"

Initialize-AzCli $Subscription | Out-Null
$shared = Get-SharedResources $ResourceGroup
$appDir = Join-Path (Split-Path -Parent $PSScriptRoot) "src\agent"
$exists = Test-Az containerapp show -n $AppName -g $ResourceGroup

$liveFqdn = Invoke-Az containerapp show -n $LiveAppName -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
$clientId = Invoke-Az identity show --ids $shared.IdentityId --query clientId -o tsv

if (-not $ImageTag) { $ImageTag = Get-Date -Format "yyyyMMddHHmmss" }
$image = "$($shared.LoginServer)/green-screen-agent-app:$ImageTag"

Write-Step "Building $image in Azure Container Registry"
$run = Invoke-Az acr build --registry $shared.Registry --image "green-screen-agent-app:$ImageTag" `
    --platform linux/amd64 --no-logs --query "{id:runId,status:status}" -o json $appDir | ConvertFrom-Json
if ($run.status -ne "Succeeded") {
    throw "Image build $($run.id) $($run.status). Logs: az acr task logs -r $($shared.Registry) --run-id $($run.id)"
}

$envVars = @(
    "AZURE_CLIENT_ID=$clientId",
    "AZURE_AI_PROJECT_ENDPOINT=$($shared.FoundryProjectEndpoint)",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME=$($shared.FoundryModelDeployment)",
    "GREEN_SCREEN_MCP_URL=https://$liveFqdn/mcp",
    "GREEN_SCREEN_LIVE_URL=https://$liveFqdn/"
)

if ($exists) {
    Write-Step "Updating container app '$AppName'"
    Invoke-Az containerapp update -n $AppName -g $ResourceGroup --image $image --set-env-vars @envVars -o none
}
else {
    Write-Step "Creating container app '$AppName'"
    Invoke-Az containerapp create -n $AppName -g $ResourceGroup --environment $shared.EnvironmentId `
        --image $image --registry-server $shared.LoginServer --registry-identity $shared.IdentityId `
        --user-assigned $shared.IdentityId --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 1 `
        --ingress external --target-port 8080 --env-vars @envVars --tags app=green-screen-agent -o none
}

$fqdn = Invoke-Az containerapp show -n $AppName -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host ""
Write-Host "Foundry agent app deployed ($image)." -ForegroundColor Green
Write-Host "  UI            : https://$fqdn/"
Write-Host "  Foundry agent : green-screen-agent in $($shared.FoundryProjectEndpoint)"
Write-Host "  MCP service   : https://$liveFqdn/mcp"
