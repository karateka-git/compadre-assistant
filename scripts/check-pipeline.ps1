$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

. (Join-Path $PSScriptRoot "common.ps1")
Invoke-ProjectCompose -ComposeArguments @('build', 'tg_catcher')
Invoke-ProjectCompose -ComposeArguments @('run', '--rm', 'tg_catcher', 'python', '-m', 'obs_chat_bot', '--pipeline-smoke')
