$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $projectRoot

. (Join-Path $PSScriptRoot "common.ps1")
Invoke-ProjectCompose -ComposeArguments @('build', 'tg_assistant')
Invoke-ProjectCompose -ComposeArguments @('run', '--rm', 'tg_assistant', 'python', '-m', 'compadre_assistant', '--pipeline-smoke')
