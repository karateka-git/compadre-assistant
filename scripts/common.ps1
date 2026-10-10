# Общие команды для scripts: Compose сам разрешает .env и переменные окружения.
function Get-ConfiguredBotServices {
    $composeJson = docker compose config --format json
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read Docker Compose configuration."
    }
    # Полная конфигурация содержит secrets; используем её только в памяти.
    $composeConfig = ($composeJson -join "`n") | ConvertFrom-Json
    $channelValue = $composeConfig.services.tg_assistant.environment.ENABLED_CHANNELS
    $channels = @($channelValue.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($channels.Count -eq 0 -or (@($channels | Select-Object -Unique).Count -ne $channels.Count)) {
        throw "ENABLED_CHANNELS must select telegram and/or vk without duplicates."
    }
    foreach ($channel in $channels) {
        switch ($channel) {
            'telegram' { 'tg_assistant' }
            'vk' { 'vk_assistant' }
            default { throw "Unknown channel in ENABLED_CHANNELS: $channel" }
        }
    }
}

function Invoke-ProjectCompose {
    param([string[]]$ComposeArguments)
    docker compose @ComposeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose command failed with exit code $LASTEXITCODE."
    }
}
