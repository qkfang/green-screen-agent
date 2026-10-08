<#
.SYNOPSIS
    Creates the shared Azure resources for the green-screen agent demo from bicep\main.bicep.

.DESCRIPTION
    Creates the resource group, then deploys bicep\main.bicep: Log Analytics workspace, Azure
    Container Registry (Basic, admin user disabled), a user-assigned managed identity with AcrPull
    on the registry, a virtual network, and a VNet-integrated Container Apps environment (needed
    for external TCP ingress to TN3270). An existing environment without a VNet is deleted together
    with its container apps and recreated. Otherwise safe to re-run.
    Then run deploy-tk5.ps1 and deploy-green-screen-agent.ps1.
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-green-screen-agent",
    [string]$Location = "australiaeast",
    [ValidatePattern("^[a-z][a-z0-9-]{1,14}$")]
    [string]$Prefix = "gsa",
    [string]$Subscription
)

. "$PSScriptRoot\azure-common.ps1"

Initialize-AzCli $Subscription | Out-Null
$template = Join-Path (Split-Path -Parent $PSScriptRoot) "bicep\main.bicep"
$environmentName = "$Prefix-env"

Write-Step "Resource group '$ResourceGroup' in $Location"
Invoke-Az group create -n $ResourceGroup -l $Location --tags app=green-screen-agent -o none

# A VNet cannot be attached to an existing environment, so one created without it is replaced.
$existingSubnet = & { $ErrorActionPreference = "Continue"; & az containerapp env show -n $environmentName -g $ResourceGroup --query "properties.vnetConfiguration.infrastructureSubnetId || 'none'" -o tsv 2>$null }
if ($LASTEXITCODE -eq 0 -and $existingSubnet -eq "none") {
    $environmentId = Invoke-Az containerapp env show -n $environmentName -g $ResourceGroup --query id -o tsv
    $apps = @(Invoke-Az containerapp list -g $ResourceGroup --query "[?properties.managedEnvironmentId=='$environmentId'].name" -o tsv | Where-Object { $_ })
    Write-Warning "Environment '$environmentName' has no VNet and must be recreated. Deleting it and its apps: $($apps -join ', ')"
    foreach ($app in $apps) {
        Write-Step "Deleting container app '$app'"
        Invoke-Az containerapp delete -n $app -g $ResourceGroup --yes -o none
    }
    Write-Step "Deleting environment '$environmentName'"
    Invoke-Az containerapp env delete -n $environmentName -g $ResourceGroup --yes -o none
}

Write-Step "Deploying $template"
# Right after a delete, the environment name stays reserved for a while (ManagedEnvironmentScheduledForDelete).
for ($attempt = 1; ; $attempt++) {
    $result = & { $ErrorActionPreference = "Continue"; & az deployment group create -g $ResourceGroup -n $InfraDeploymentName `
            --template-file $template --parameters location=$Location prefix=$Prefix -o none 2>&1 }
    if ($LASTEXITCODE -eq 0) { break }
    $message = $result | Out-String
    if ($message -notmatch "ManagedEnvironmentScheduledForDelete" -or $attempt -ge 30) {
        Write-Host $message
        throw "'az deployment group' failed with exit code $LASTEXITCODE."
    }
    Write-Host "  Old environment '$environmentName' is still being removed; retrying in 60s (attempt $attempt)..."
    Start-Sleep -Seconds 60
}

$shared = Get-SharedResources $ResourceGroup
Write-Host ""
Write-Host "Shared resources are ready:" -ForegroundColor Green
Write-Host "  Resource group : $ResourceGroup ($Location)"
Write-Host "  Registry       : $($shared.LoginServer)"
Write-Host "  Identity       : $($shared.IdentityId)"
Write-Host "  Environment    : $($shared.EnvironmentId)"
Write-Host "Next: .\scripts\deploy-tk5.ps1, then .\scripts\deploy-green-screen-agent.ps1"
