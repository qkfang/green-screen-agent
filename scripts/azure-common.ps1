# Shared helpers for the Azure deployment scripts. Dot-source it: . "$PSScriptRoot\azure-common.ps1"

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Az {
    # Runs az and throws on failure. Only the command group is echoed so secrets in arguments never are.
    $output = & az @args
    if ($LASTEXITCODE -ne 0) {
        throw "'az $($args[0..1] -join ' ')' failed with exit code $LASTEXITCODE."
    }
    $output
}

function Test-Az {
    $ErrorActionPreference = "Continue"  # Windows PowerShell turns redirected stderr into errors
    & az @args 2>$null | Out-Null
    $LASTEXITCODE -eq 0
}

function Initialize-AzCli([string]$Subscription) {
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        throw "Azure CLI ('az') was not found. Install it from https://aka.ms/installazurecli."
    }
    if ($Subscription) {
        Invoke-Az account set --subscription $Subscription | Out-Null
    }
    $json = & { $ErrorActionPreference = "Continue"; & az account show -o json 2>$null }
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        throw "Not signed in to Azure. Run 'az login' first."
    }
    $account = $json | ConvertFrom-Json
    Write-Host "Subscription: $($account.name) ($($account.id))"
    Invoke-Az extension add --name containerapp --upgrade --yes --only-show-errors | Out-Null
    $account
}

# Name of the resource-group deployment of bicep\main.bicep; the app scripts read its outputs.
$InfraDeploymentName = "green-screen-agent-infra"

function Get-SharedResources([string]$ResourceGroup) {
    $json = & { $ErrorActionPreference = "Continue"; & az deployment group show -g $ResourceGroup -n $InfraDeploymentName --query properties.outputs -o json 2>$null }
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        throw "Deployment '$InfraDeploymentName' not found in '$ResourceGroup'. Run scripts\deploy-azure-resources.ps1 first."
    }
    $outputs = $json | ConvertFrom-Json
    [pscustomobject]@{
        Registry      = $outputs.registryName.value
        LoginServer   = $outputs.registryLoginServer.value
        IdentityId    = $outputs.identityId.value
        EnvironmentId = $outputs.environmentId.value
        SubnetPrefix  = $outputs.infrastructureSubnetPrefix.value
    }
}
