<#
.SYNOPSIS
    Deploys the TK5 MVS 3.8j + KICKS mainframe (backscratcher/tk5-mvs-kicks) to Azure Container Apps.

.DESCRIPTION
    Imports the Docker Hub image into the shared registry (no Docker Hub pulls at runtime) and
    creates or updates a single-replica container app with external TCP ingress on port 3270, so
    any TN3270 emulator can connect to <fqdn>:3270. Apps in the same environment reach it at
    <AppName>:3270. Ingress is restricted to -AllowedIpRange (default: your current public IP)
    plus the environment subnet; pass -AllowedIpRange 0.0.0.0/0 to allow everyone.
    Run deploy-azure-resources.ps1 first (external TCP ingress needs its VNet-integrated environment).
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-green-screen-agent",
    [string]$Subscription,
    [ValidatePattern("^[a-z][a-z0-9-]{0,30}[a-z0-9]$")]
    [string]$AppName = "tk5-mvs-kicks",
    [string]$SourceImage = "docker.io/backscratcher/tk5-mvs-kicks:latest",
    [string]$Cpu = "2.0",
    [string]$Memory = "4Gi",
    # CIDR ranges allowed to reach TN3270 port 3270. Defaults to this machine's public IP.
    [string[]]$AllowedIpRange
)

. "$PSScriptRoot\azure-common.ps1"

Initialize-AzCli $Subscription | Out-Null
$shared = Get-SharedResources $ResourceGroup
$repository = "tk5-mvs-kicks"

if (-not $shared.SubnetPrefix) {
    throw "The Container Apps environment has no VNet, so external TCP ingress is not possible. Re-run scripts\deploy-azure-resources.ps1."
}
if (-not $AllowedIpRange) {
    $publicIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 15).Trim()
    $AllowedIpRange = @("$publicIp/32")
}
$allowRules = [ordered]@{ "environment" = $shared.SubnetPrefix }
for ($i = 0; $i -lt $AllowedIpRange.Count; $i++) {
    $range = $AllowedIpRange[$i]
    $allowRules["client-$($i + 1)"] = if ($range -match "/") { $range } else { "$range/32" }
}

Write-Step "Importing $SourceImage into $($shared.LoginServer)"
Invoke-Az acr import --name $shared.Registry --source $SourceImage --image "${repository}:latest" --force -o none
$digest = Invoke-Az acr repository show --name $shared.Registry --image "${repository}:latest" --query digest -o tsv
# Pin the digest so a re-run only restarts the mainframe when the upstream image changed.
$image = "$($shared.LoginServer)/$repository@$digest"

if (Test-Az containerapp show -n $AppName -g $ResourceGroup) {
    Write-Step "Updating container app '$AppName'"
    Invoke-Az containerapp update -n $AppName -g $ResourceGroup --image $image --cpu $Cpu --memory $Memory `
        --min-replicas 1 --max-replicas 1 -o none
    Invoke-Az containerapp ingress update -n $AppName -g $ResourceGroup --type external --transport tcp `
        --target-port 3270 --exposed-port 3270 -o none
}
else {
    Write-Step "Creating container app '$AppName'"
    Invoke-Az containerapp create -n $AppName -g $ResourceGroup --environment $shared.EnvironmentId `
        --image $image --registry-server $shared.LoginServer --registry-identity $shared.IdentityId `
        --user-assigned $shared.IdentityId --cpu $Cpu --memory $Memory --min-replicas 1 --max-replicas 1 `
        --ingress external --transport tcp --target-port 3270 --exposed-port 3270 `
        --tags app=green-screen-agent -o none
}

Write-Step "Restricting TN3270 ingress to $($allowRules.Values -join ', ')"
foreach ($rule in $allowRules.GetEnumerator()) {
    Invoke-Az containerapp ingress access-restriction set -n $AppName -g $ResourceGroup --rule-name $rule.Key `
        --ip-address $rule.Value --action Allow -o none
}
$existingRules = @(Invoke-Az containerapp ingress access-restriction list -n $AppName -g $ResourceGroup --query "[].name" -o tsv | Where-Object { $_ })
foreach ($name in $existingRules | Where-Object { -not $allowRules.Contains($_) }) {
    Invoke-Az containerapp ingress access-restriction remove -n $AppName -g $ResourceGroup --rule-name $name -o none
}

$fqdn = Invoke-Az containerapp show -n $AppName -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host ""
Write-Host "TK5 is starting in '$AppName' (image $image)." -ForegroundColor Green
Write-Host "  TN3270 (public)          : ${fqdn}:3270  (allowed: $($AllowedIpRange -join ', '))"
Write-Host "  TN3270 (in environment)  : ${AppName}:3270"
Write-Host "  The MVS IPL takes about two minutes. Users: HERC01 / CUL8TR."
Write-Host "Next: .\scripts\deploy-green-screen-agent.ps1"
