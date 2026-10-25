# Передача polling ботов для шага 3.3

Этот runbook описывает временное переключение существующих Telegram- и
VK-ботов с production VPS на локальную сборку для безопасной клиентской
проверки и обязательный возврат. Отдельные тестовые боты не создаются.
Состояние VPS в [PRODUCTION.md](../PRODUCTION.md) — датированный снимок;
перед окном проверить актуальное состояние по read-only процедуре из
[OPERATIONS.md](../OPERATIONS.md#production-vps). Не выполнять production
выкладку, миграции, изменения unit/Compose, controller state или production-БД.

## Предварительные условия

- Окно и порядок переключения согласованы с владельцем. Оба канала могут быть
  недоступны на время preflight, проб и возврата.
- В основной локальной папке `C:\Users\compadre\Documents\obsChatBot`
  уже есть `.env` с учётными данными существующих ботов. Docker читает этот файл
  напрямую через `--env-file`; не копировать его в worktree, не открывать и не
  выводить содержимое. **Не извлекать секреты с VPS, не копировать их с VPS и
  не записывать в команды, файлы, скриншоты или отчёт.** Если локальный файл
  недоступен, остановить подготовку.
- Локальный образ `compadre-assistant:contextual-help` уже собран в Docker.
  Локальная development-БД будет храниться только в новом пустом каталоге
  worktree `data/stage3-probe`; production SQLite не подключать и не копировать.
- Не использовать UI-автоматизацию или компьютерное управление.

## 1. Read-only preflight VPS

Подключиться по процедуре доступа из `docs/PRODUCTION.md` и выполнять только
чтение. Не открывать `.env`, PEM, deploy key, runtime env, SQLite или полные
логи. Проверить и записать только безопасные статусы:

```bash
cd /opt/obs-chat-bot
sudo systemctl is-active obs-chat-bot.service obs-chat-bot-backup.timer
docker compose ps
docker compose ps -q | xargs -r docker inspect --format '{{.Name}} {{.Image}} {{.State.Status}} restarts={{.RestartCount}}'
sudo test ! -e /var/lib/compadre-assistant-deploy/deploy-in-progress
```

Продолжать только если unit активна, backup timer в ожидаемом состоянии,
`tg_assistant` и `vk_assistant` существуют и `healthy`, controller не занят,
marker отсутствует и нет текущей выкладки/восстановления. Снимок production до
окна и его дату сохранить без секретов, ID пользователей и содержимого SQLite.
Если любой факт неизвестен или расходится с ожиданием — остановиться до
переключения и выяснить состояние по процедуре эксплуатации.

`obs-chat-bot.service` управляет polling обоих каналов совместной Compose
командой. Останавливать и запускать оба канала вместе только через эту unit;
не останавливать production-контейнеры Docker-командами. Backup timer оставить
в исходном состоянии.

## 2. Подготовить локальный образ, отдельную БД и проверить healthcheck

В PowerShell из корня worktree задать путь к уже существующему локальному
`.env`. Только проверить факт его наличия, не читать файл:

```powershell
$localEnvPath = 'C:\Users\compadre\Documents\obsChatBot\.env'
if (-not (Test-Path -LiteralPath $localEnvPath -PathType Leaf)) { throw 'Локальный .env не найден; остановитесь.' }
```

Создать отдельный пустой каталог для БД. Если каталог уже существует и содержит
файлы, не удалять их и не запускать пробу с этим каталогом:

```powershell
$dataRoot = Join-Path (Get-Location).Path 'data'
if (-not (Test-Path -LiteralPath $dataRoot)) { New-Item -ItemType Directory -Path $dataRoot | Out-Null }
$probeDataPath = Join-Path $dataRoot 'stage3-probe'
if (Test-Path -LiteralPath $probeDataPath) {
    if (Get-ChildItem -LiteralPath $probeDataPath -Force) { throw 'Каталог stage3-probe не пуст; остановитесь.' }
} else {
    New-Item -ItemType Directory -Path $probeDataPath | Out-Null
}
$probeDataPath = (Resolve-Path -LiteralPath $probeDataPath).Path
```

До остановки production проверить уже собранный образ на изолированной БД.
Переданные через `-e` значения задают development-режим, путь отдельной SQLite
и пустой список capabilities; Docker не включает restart policy:

```powershell
docker run --rm --env-file $localEnvPath -e APP_ENV=development -e DATABASE_PATH=/app/data/app.db -e ENABLED_CAPABILITIES= --mount "type=bind,source=$probeDataPath,target=/app/data" compadre-assistant:contextual-help python -m compadre_assistant --healthcheck --channels telegram,vk
```

Продолжать только при успешном healthcheck. Он проверяет локальный образ и
отдельный development-каталог до переключения VPS; если образ, конфигурация,
каталог или healthcheck не соответствуют ожиданию, остановиться, не затрагивая
production.

## 3. Остановить оба production polling

После успешного preflight и локального healthcheck остановить именно unit:

```bash
sudo systemctl stop obs-chat-bot.service
```

Затем read-only подтвердить `inactive` и что оба Compose-сервиса остановлены:

```bash
sudo systemctl is-active obs-chat-bot.service
docker compose ps
```

Ожидается неактивная unit и остановленные `tg_assistant`/`vk_assistant`. Если
оба polling не остановлены, не запускать локальные боты; перейти к
[порядку отказа и возврата](#если-проба-или-возврат-не-удались). Не запускать
локальные процессы параллельно с production polling: это может вызвать конфликт
polling Telegram и двойную обработку VK.

## 4. Запустить оба локальных канала

В PowerShell из корня того же worktree последовательно запустить обе команды.
Флаг `-d` оставляет контейнеры работать отдельно от терминала, а `--rm`
удаляет контейнер после остановки. У `docker run` по умолчанию restart policy
`no`; команды не задают `--restart`. Оба контейнера
читают `.env` напрямую из основной локальной папки и используют общий
изолированный каталог БД:

```powershell
docker run -d --rm --name stage3-probe-tg --env-file $localEnvPath -e APP_ENV=development -e DATABASE_PATH=/app/data/app.db -e ENABLED_CAPABILITIES= --mount "type=bind,source=$probeDataPath,target=/app/data" compadre-assistant:contextual-help python -m compadre_assistant --telegram-bot
```

```powershell
docker run -d --rm --name stage3-probe-vk --env-file $localEnvPath -e APP_ENV=development -e DATABASE_PATH=/app/data/app.db -e ENABLED_CAPABILITIES= --mount "type=bind,source=$probeDataPath,target=/app/data" compadre-assistant:contextual-help python -m compadre_assistant --vk-bot
```

Запускать только после остановки **обоих** production polling. Read-only
проверить состояние и отсутствие restart policy, не выводя env:

```powershell
docker inspect --format '{{.Name}} {{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}' stage3-probe-tg stage3-probe-vk
```

Ожидаются оба запущенных контейнера и пустая политика перезапуска. Убедиться
по безопасным стартовым признакам, что polling каждого канала стартовал; не
печатать и не сохранять полные логи. Если локальная сборка пытается обращаться
к vault, LLM или другой интеграции при старте либо credentials не относятся к
ожидаемым существующим ботам, немедленно остановить оба контейнера.

## 5. Ограниченные клиентские пробы

Проверять только личные диалоги существующих ботов и только искусственные
сообщения без URL, приватных текстов, реальных данных, запросов на запись,
публикацию, синхронизацию или иное внешнее действие. Перед отправкой
подтвердить безопасную семантику соответствующего пути по коду; если это не
доказано — ограничиться пассивным наблюдением и не отправлять.

- Проверять Telegram command menu и подсказки `/` в личном диалоге. Если
  синтетическая API-проба требует временно изменить список команд, сначала
  получить ответ для `chat` scope этого диалога и зафиксировать список,
  видимый в клиенте. Не менять global/default scope. После временного
  искусственного набора восстановить исходное состояние этого chat scope:
  вернуть сохранённый список либо удалить временный override, если scoped
  список отсутствовал. Сверить API и прежний вид меню. Если исходный вид был пустым, а удаление
  override показывает унаследованные команды, восстановить пустой список в chat
  scope и перепроверить. Если исходный вид не восстановлен, остановить пробы и
  продолжать восстановление до возврата unit.
- Telegram inline-кнопки/callback и VK keyboard/event проверять отдельным
  временным probe-кодом только с искусственным payload и без вызова application
  use case. Текущие адаптеры этих функций статически не содержат; probe не
  доказывает поддержку feature приложением.
- Обычные ответы приложения проверять только на заранее подтверждённых
  read-only командах или искусственных текстах, которые не запускают Ask/LLM,
  link processing, запись в БД/vault или интеграции. Не нажимать существующие
  кнопки, если они могут запустить действие.
- Записывать платформу/версию клиента, время, нейтральную метку диалога, статус
  и видимый результат. Не сохранять IDs, имена, токены или тексты переписки.

Не использовать групповые чаты, не трогать данные других пользователей, не
менять приложение или global/default command scopes. Временный chat scope
использовать только по процедуре выше и восстановить до завершения окна. При
неожиданном ответе или побочном эффекте
немедленно остановить оба локальных контейнера и вернуть production.

## 6. Остановить локальные каналы и вернуть VPS

Остановить оба локальных контейнера одной командой из PowerShell:

```powershell
docker stop stage3-probe-tg stage3-probe-vk
```

`docker stop` дожидается остановки; из-за `--rm` контейнеры должны удалиться.
Проверить, что контейнеры больше не существуют, прежде чем запускать production:

```powershell
docker ps --filter name=stage3-probe-tg --format '{{.Names}} {{.Status}}'
docker ps --filter name=stage3-probe-vk --format '{{.Names}} {{.Status}}'
```

Оба результата должны быть пустыми. Затем восстановить production polling обоих
каналов совместно:

```bash
sudo systemctl start obs-chat-bot.service
```

Проверить unit и оба контейнера read-only командами из preflight. Ожидаются
активная unit, оба сервиса `healthy`, polling без restart loop и отсутствие
дубликатов. Сверить image ID, container ID и безопасные состояния с production-
снимком до переключения. Healthcheck не заменяет подтверждения в обоих личных
диалогах; пользователь подтверждает, что Telegram и VK снова отвечают.

## Если проба или возврат не удались

1. Остановить оба локальных контейнера одной командой:
   `docker stop stage3-probe-tg stage3-probe-vk`. Если один уже вышел и удалён
   из-за `--rm`, проверить состояние оставшегося и остановить его по имени.
2. Подтвердить пустые результаты двух команд `docker ps` из пункта 6. Не
   продолжать пробы и не запускать дополнительные локальные экземпляры.
3. Read-only проверить, что production unit неактивна и оба production-
   контейнера остановлены. Затем один раз запустить
   `sudo systemctl start obs-chat-bot.service`.
4. Проверить unit, оба контейнера, health, polling и restart count. При ошибке
   читать только необходимые очищенные статусы/логи и действовать по
   [OPERATIONS.md: отказ и возврат](../OPERATIONS.md#отказ-выпуска-и-возврат).
   Не изменять controller marker/state, SQLite, Compose pin или restart policy;
   этот runbook не разрешает rollback выпуска или восстановление БД.
5. Если здоровый polling обоих production-ботов не восстановлен, не запускать
   локальные боты повторно. Оставить дубли остановленными и немедленно передать
   владельцу безопасный статус проблемы для ручного восстановления.

Записать результат окна, факт возврата и неподтверждённые пробы без токенов,
ID пользователей и содержимого БД. Этот runbook документирует переключение
polling; сам по себе он не закрывает шаг 3.3 или этап 3.
