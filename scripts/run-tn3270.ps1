[CmdletBinding()]
param(
    [string]$ContainerName = "mvs-kicks",
    [string]$Image = "backscratcher/tk5-mvs-kicks:latest",
    [ValidateRange(1, 65535)]
    [int]$Tn3270Port = 3270,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 8038
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install Docker Desktop and ensure 'docker' is on PATH."
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is not running. Start Docker Desktop and try again."
}

$state = & docker container inspect --format "{{.State.Status}}" $ContainerName 2> $null
if ($LASTEXITCODE -eq 0) {
    if ($state -eq "running") {
        Write-Host "TN3270 server '$ContainerName' is already running on localhost:$Tn3270Port."
        exit 0
    }

    & docker start $ContainerName
    if ($LASTEXITCODE -ne 0) {
        throw "Docker could not start existing container '$ContainerName'."
    }

    Write-Host "Started TN3270 server '$ContainerName' on localhost:$Tn3270Port."
    exit 0
}

& docker run --detach `
    --platform linux/amd64 `
    --name $ContainerName `
    --publish "${Tn3270Port}:3270" `
    --publish "${WebPort}:8038" `
    $Image

if ($LASTEXITCODE -ne 0) {
    throw "Docker could not create TN3270 server '$ContainerName' from '$Image'."
}

Write-Host "TN3270 server '$ContainerName' is starting on localhost:$Tn3270Port."
Write-Host "Wait about two minutes for the MVS IPL, then sign on with HERC01 / CUL8TR."
