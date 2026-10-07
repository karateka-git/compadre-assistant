$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

. (Join-Path $PSScriptRoot "common.ps1")
$services = @(Get-ConfiguredBotServices)
Invoke-ProjectCompose -ComposeArguments @('build', $services[0])
foreach ($mode in @('--healthcheck', '--sqlite-smoke', '--pipeline-smoke', '--analysis-smoke')) {
    Invoke-ProjectCompose -ComposeArguments @('run', '--rm', $services[0], 'python', '-m', 'obs_chat_bot', $mode)
}
