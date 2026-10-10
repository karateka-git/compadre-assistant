$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

. (Join-Path $PSScriptRoot "common.ps1")
$services = @(Get-ConfiguredBotServices)
Invoke-ProjectCompose -ComposeArguments @('build', $services[0])
Invoke-ProjectCompose -ComposeArguments @('run', '--rm', $services[0], 'python', '-m', 'compadre_assistant', '--healthcheck')
