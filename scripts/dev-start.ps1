param(
    [string]$Mode = ""
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

Write-Host "Project: $projectRoot"
if ($Mode -eq "debug") {
    $env:APP_DEBUG = "true"
    Write-Host "Debug mode: APP_DEBUG=true"
}
elseif ($Mode) {
    throw "Unknown dev-start mode: $Mode"
}

Write-Host "Starting Docker Desktop..."
docker desktop start

Write-Host "Waiting for Docker Engine..."
for ($attempt = 1; $attempt -le 30; $attempt++) {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Docker Engine is ready."
        break
    }

    if ($attempt -eq 30) {
        throw "Docker Engine did not become ready in time."
    }

    Start-Sleep -Seconds 2
}

. (Join-Path $PSScriptRoot "common.ps1")
$services = @(Get-ConfiguredBotServices)
$inactiveServices = @(@('tg_catcher', 'vk_catcher') | Where-Object { $_ -notin $services })
if ($inactiveServices.Count -gt 0) {
    Invoke-ProjectCompose -ComposeArguments (@('stop') + $inactiveServices)
}
Invoke-ProjectCompose -ComposeArguments (@('up', '--build', '--remove-orphans', '--menu=false') + $services)
