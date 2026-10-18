# Эксплуатация Compadre Assistant

Порядок запуска и диагностики локальных Telegram/VK и production VPS.
Подготовка новой рабочей копии описана в [SETUP.md](SETUP.md).

## Production VPS

Перед работой прочитайте [контекст и правила production](PRODUCTION.md).
Описанное ниже состояние сервера требует проверки при подключении.

Текущий production развёрнут в `/opt/obs-chat-bot` на H3llo VPS. Конфигурация
`.env`, PEM GitHub App и SQLite не хранятся в Git. VK работает напрямую,
Telegram Bot API доступен через выделенный VPN и прокси. Пользовательский
ответ на `/status` подтверждён после включения.

Для Telegram подготовлен отдельный WireGuard-интерфейс `wgobs` между H3LLO
(`10.77.77.2`) и Timeweb (`10.77.77.1`). На Timeweb SOCKS5-прокси Dante
принимает подключения только по адресу туннеля `10.77.77.1:1080` от H3LLO.
В серверный `.env` уже добавлено
`TELEGRAM_PROXY_URL=socks5://10.77.77.1:1080`; получение данных бота и polling
проверены. Прокси применяется только к Bot API, прочие
интеграции используют обычный маршрут.

Проверка сетевого пути без токена и без изменений в БД:

```bash
sudo systemctl is-active wg-quick@wgobs
curl --socks5-hostname 10.77.77.1:1080 -I --max-time 10 https://api.telegram.org/
```

На Timeweb проверить `systemctl is-active wg-quick@wgobs danted`. Если туннель
или прокси недоступны, проверить `wg show wgobs`, состояние двух systemd-сервисов
и доступность UDP `51831` между VPS. Закрытые ключи и содержимое `.env` в
журналы и отчёты не копировать.

Проверить службу и контейнеры:

```bash
sudo systemctl is-active obs-chat-bot.service obs-chat-bot-backup.timer
sudo docker ps --filter label=com.docker.compose.project=obs-chat-bot \
  --format '{{.Names}} {{.Image}} {{.Status}}'
```

Обычный выпуск выполняется только для опубликованного GitHub Release с полным
`release-full` для точного коммита релизной ветки. После проверки текущего
состояния и согласования эксплуатационного окна команда
`sudo deploy-prod vX.Y.Z` сама сверяет Release, закрепляет образ, создаёт backup, останавливает
оба канала, при необходимости применяет миграции и проверяет запуск обоих.
Повтор того же выпуска должен завершиться `already_installed` без пересоздания.
При `degraded` новый запуск блокируется до ручной проверки и восстановления
по [runbook](auto-deploy-main/STAGE5_STEP4.md#отказ-reboot-и-ручное-восстановление).
Не удалять production-БД по development-инструкции: изменения её схемы требуют
миграции с сохранением данных.

### Доступ systemd к закреплённому Compose

Внешний каталог `/var/lib/compadre-assistant-deploy` принадлежит root и имеет
права `0700`; `runtime.env` и оба Compose override внутри него — root:root
`0600`. Основной `obs-chat-bot.service` задаёт `User=user` и `Group=user`.
Поздний drop-in `zz-deploy-pinned.conf` должен перекрывать более ранние
`90-deploy-pinned.conf` и `channels.conf` и задавать **обе** команды:
`ExecStart=!/usr/bin/docker compose up -d --no-build --pull never
vk_assistant tg_assistant` и `ExecStop=!/usr/bin/docker compose stop
vk_assistant tg_assistant`. По [документации systemd](https://www.freedesktop.org/software/systemd/man/257/systemd.service.html)
префикс `!` выполняет только эти команды без ограничений `User=`/`Group=`;
остальные настройки службы сохраняются. Права приватного каталога и файлов
не ослаблять ради запуска Compose.

Перед первым start/stop после изменения unit, drop-in или прав проверить
`systemd-analyze verify`, `NeedDaemonReload=no`, порядок `DropInPaths` и
итоговые D-Bus `ExecStartEx` **и** `ExecStopEx`: точные аргументы и единственный
флаг `no-setuid`. Также сверить `User=user`, `Group=user`, рабочий каталог,
путь `EnvironmentFiles` к `runtime.env` и условие, блокирующее службу при
`deploy-in-progress`. Контроллер `deploy-prod` выполняет эти проверки перед
обычным выпуском. Если хоть одна не проходит, остановиться до перезапуска;
подмена прав `0700`/`0600` или одного только `ExecStart` не исправляет путь
остановки службы.

17 октября 2026 года `deploy-prod --verify-pinned` выявил именно этот дефект:
root-контроллер смог поднять прежние контейнеры и подтвердить health/polling,
но systemd запустил Compose как `user` и получил `permission denied` при
чтении `pinned-compose.yml`. Возврат повторил тот же отказ; контроллер оставил
`degraded`, marker, остановленные каналы и выключенный backup timer. В
`v1.0.2` исправлены обе команды и добавлена проверка их фактических свойств.
После ручного восстановления 18 октября старый образ снова работает;
текущий статус и точный SHA указаны в [PRODUCTION.md](PRODUCTION.md).

### Архив контроллера с Windows: точные байты Git

При подготовке snapshot для VPS не упаковывать файлы из рабочей копии Windows:
на ней переводы строк могут стать CRLF, хотя опубликованный Git-коммит хранит
LF. 18 октября 2026 года такой архив остановился на проверке SHA256
`scripts/deploy_prod.py` **до** замены действующего snapshot. Контрольную сумму
в установочном скрипте под изменённые байты не подгонять.

Архив `scripts/` и `deploy/` создавать из точного SHA проверенного Release:

```powershell
$releaseSha = "PASTE_VERIFIED_40_CHAR_SHA"
$archivePath = "C:\Temp\release-snapshot.tar"
git -c core.autocrlf=false archive --format=tar --output=$archivePath $releaseSha scripts deploy
```

Перед передачей сверить SHA256 всего архива и **каждого обычного файла** в нём
с `git show <SHA-релиза>:<путь-файла>`; отклонить неожиданные symlink/hardlink.
Для передаваемых shell/Python-скриптов проверить отсутствие `CR` и синтаксис.
На VPS повторно сверить SHA256 архива и распакованных файлов в отдельном
root-owned staging-каталоге. Если хоть один файл не совпал, остановиться до
атомарной замены snapshot и проверить источник архива. Незавершённый staging
удалять только после проверки его точного пути и неизменности действующих
snapshot, drop-in, marker и состояния ботов.

Проверить ежедневные SQLite-копии и их журнал:

```bash
sudo systemctl list-timers obs-chat-bot-backup.timer
sudo journalctl -u obs-chat-bot-backup.service --since today
sudo ls -l /var/backups/obs-chat-bot
```

Timer запускает copy в 03:15 UTC, хранит семь последних файлов и выполняет
backup через SQLite API. Копии лежат на том же диске VPS; защита от потери
всей VM требует отдельного backup/snapshot у провайдера или внешнего хранилища.

## Перед запуском

1. Убедиться, что Docker Desktop запущен.
2. Проверить `.env`:
   - `TELEGRAM_BOT_TOKEN` задан;
   - `VK_BOT_TOKEN` и `VK_GROUP_ID` заданы, если запускается VK adapter;
   - `OPENAI_BASE_URL=https://api.timeweb.ai/v1`;
   - `OPENAI_MODEL=dashscope/qwen3.5-flash`;
   - `EMBEDDING_API_KEY` содержит общий ключ Timeweb AI Gateway без `Bearer`:
     Compose использует его также как `OPENAI_API_KEY` обоих контейнеров;
     для запуска Python напрямую задать `OPENAI_API_KEY` тем же значением;
   - если включены embeddings, одновременно заданы `EMBEDDING_BASE_URL`,
     `EMBEDDING_API_KEY`, `EMBEDDING_DOCUMENT_MODEL` и
     `EMBEDDING_QUERY_MODEL`; текущие значения — `https://api.timeweb.ai/v1`
     и `openai/text-embedding-3-large` для обеих моделей;
   - если включён GitHub connector, одновременно заданы `GITHUB_APP_ID`,
     `GITHUB_CLIENT_ID`, `GITHUB_APP_SLUG`, `GITHUB_PRIVATE_KEY_PATH`, а PEM
     доступен только процессу приложения;
   - `OBSIDIAN_CREATED_NOTE_TAG` содержит имя тега без `#`; при отсутствии
     переменной используется `knowledge-catcher`, значение `off` отключает
     marker;
   - `APP_DEBUG=true` включать только для диагностики, когда нужны подробные
     безопасные логи incoming-flow.
3. Убедиться, что нет второго polling-экземпляра Telegram-бота:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'compadre_assistant|--telegram-bot' } |
  Select-Object ProcessId,Name,CommandLine
```

Если старое окно Telegram-бота открыто, закрыть его перед новым запуском.

## Development-БД перед новым запуском

Этот порядок временный: после этапа разработки разрешение на пересоздание
без подтверждения прекращается. Далее схема обновляется миграциями с сохранением
данных; удаление БД требует отдельного явного разрешения пользователя.

При изменении схемы пересоздать локальную БД по
[порядку в RUN.md](RUN.md#пересоздание-базы-после-изменения-схемы): остановить
использующие её процессы, удалить только файлы SQLite и запустить приложение.
Пользователь разрешил это для стадии разработки без дополнительного
подтверждения и переноса данных. Подключение vault и индексы можно создать
заново. Обычные изменения данных не требуют очистки; автоматического удаления
или определения несовместимости в скриптах запуска нет.

## Проверки перед работой

```powershell
.\scripts\open-check-all.cmd
```

Эта команда пересобирает Docker-образ и проверяет:

- healthcheck;
- SQLite migrations и repository;
- article pipeline без интернета;
- analysis pipeline без реального LLM.

`check-all` не запускает весь `unittest`-набор. Кроме того, PowerShell-скрипт
сейчас не проверяет `$LASTEXITCODE` после каждого вызова Docker: ошибка раннего
шага не гарантирует остановку последующих. Проверяйте результат каждого шага.
Ограничения healthcheck описаны в [RUN.md](RUN.md#6-выполнить-healthcheck).

Для быстрой проверки без отдельного окна:

```powershell
python -m unittest discover -s tests
python -m compadre_assistant --pipeline-smoke
python -m compadre_assistant --analysis-smoke
```

## Запуск бота

```powershell
.\scripts\open-dev-start.cmd
```

Окно PowerShell должно остаться открытым. Скрипт запускает Docker Compose через
`docker compose up --build`, поднимает Telegram polling и VK long polling и держит контейнер
активным до остановки через `Ctrl+C`. В логах ожидается старт polling без
`TelegramConflictError`.

## VK adapter

Если в `.env` заданы `VK_BOT_TOKEN` и `VK_GROUP_ID`, штатный
`docker compose up --build` запускает VK adapter вместе с Telegram adapter.
Отдельная команда ниже нужна только для изолированной диагностики VK:

```powershell
docker compose run --rm --entrypoint python vk_assistant -m compadre_assistant --vk-bot
```

VK использует тот же flow регистрации, привязки каналов, сохранения статей и
LLM-анализа, что и Telegram.

При временном сбое VK Long Poll adapter повторяет получение сервера, ожидание
событий или обновление сервера с задержкой до 30 секунд. В журнале записи
`VK long poll transient failure` показывают этап, номер попытки, задержку и
безопасные типы вложенной сетевой причины с числовым `errno`, если он есть.
Текст исключения, URL Long Poll и ключ в такую запись не включаются. Если
сеть восстановилась, adapter продолжает работу без ручного перезапуска;
постоянная ошибка доступа VK завершает процесс, после чего Docker применяет
restart policy. При повторяющихся постоянных ошибках проверяйте настройки
доступа VK, а не считайте циклический restart успешным восстановлением.

Telegram polling также повторяет временные сетевые ошибки. Внутренний цикл
aiogram восстанавливает получение updates, а ошибки при запуске polling
повторяются adapter с задержкой до 30 секунд. Неверный токен и конфликт с
другим polling-экземпляром завершают процесс; по ним нужно исправить
конфигурацию или остановить второй экземпляр.

## Проверка пользовательского сценария

1. Отправить боту HTTP/HTTPS-ссылку на статью.
2. Ожидаемый ответ:
   - сохранённый анализ;
   - предложение `add`, `update` или `skip`;
   - для изменения — target path и preview полного Markdown;
   - просьба ответить `да` или `нет`; до ответа GitHub не изменён.
3. Отправить произвольный текст. Бот должен напомнить о pending-предложении, а
   не начать новую обработку.
4. Ответить `нет`: предложение закрывается, vault не меняется, статья остаётся
   доступной для нового review. Для проверки write-back повторить ссылку и
   ответить `да`.
5. После `да` для `add`/`update` проверить один новый commit в default branch и
   локальный статус статьи `reviewed`; для `skip` commit отсутствует.
   Для `add` итоговая заметка должна содержать служебный тег только в YAML
   frontmatter. Для `update` пользовательская заметка без marker должна остаться
   непомеченной, а исходные frontmatter tags уже помеченной заметки — сохраниться.
   Настройка не маркирует существующие заметки массово.
6. Повторно отправить ту же ссылку. Бот должен показать сохранённый анализ и
   результат применения без загрузки страницы, LLM и нового proposal.
7. Выполнить `/reanalyze <ID статьи>`: бот повторно загружает источник и
   открывает новый review. При недоступном источнике прежний результат остаётся.

## Диагностика SQLite

При одновременном старте `tg_assistant` и `vk_assistant` начальная схема применяется
одним процессом под `BEGIN IMMEDIATE`; второй процесс ждёт SQLite lock, повторно
проверяет `schema_migrations` и не выполняет тот же SQL второй раз. Сообщения вида
`table ... already exists` на чистой базе означают регрессию этой блокировки.

## Debug-режим

Для расширенных логов в консоли сервера добавить в `.env`:

```dotenv
APP_DEBUG=true
```

В debug-режиме приложение пишет безопасные события обработки: тип входящего
сообщения, результат flow, `app_user_id`, `incoming_message_id`, `article_id`,
`analysis_id`, данные внешней identity при регистрации/привязке. Секреты,
полный текст статей, LLM prompt и API keys не логируются.

После проверки вернуть:

```dotenv
APP_DEBUG=false
```

## Диагностика embeddings

Embedding-события используют тот же стандартный Python `logging`, что и весь
проект, и попадают в stdout/stderr контейнеров. Docker собирает этот поток;
отдельной telemetry-БД или специального embedding logger нет.

Посмотреть события обоих каналов:

```powershell
docker compose logs tg_assistant vk_assistant | Select-String "event=embedding_"
```

Только окончательные сбои и переходы на FTS fallback:

```powershell
docker compose logs tg_assistant vk_assistant |
  Select-String "event=embedding_operation_failed|event=embedding_fallback"
```

`operation_id` связывает начало, SDK batches, итог операции и последующий
fallback. Поля `app_user_id`, `vault_id` и `article_id` позволяют найти
конкретный workflow. Для массовой индексации итоговое событие содержит число
batches/texts, usage, latency, errors и оценочную стоимость.

OpenAI SDK сохраняет штатный `max_retries=2`: один логический SDK-вызов может
сделать до трёх HTTP-попыток. Adapter видит и логирует их общую длительность и
окончательный результат, но не выдумывает недоступные ему детали каждой
внутренней попытки. `response.usage` и provider request ID выводятся только при
наличии. API keys, тексты chunks, compact query и vector values не логируются.

Каждый успешный embedding batch сохраняется в SQLite до следующего запроса.
После сбоя или перезапуска `/github_sync` продолжает только недостающую работу;
частичные vectors не доступны semantic search до публикации полного marker.
Истечение или смена владельца sync lease останавливает запросы и запись,
сохраняя прежние checkpoints для следующей синхронизации.

В `event=embedding_request_failed` различаются `status=invalid_response`
(например, malformed `200 OK`), `status=http_error`, `status=transport_error`
и `status=request_error` для прочих ошибок вызова SDK. Для malformed-ответов
сохраняются `http_status`, `request_id`, `batch_index`, `batch_size`,
`expected_vectors`, `actual_vectors`, `response_type`, `data_present`,
`data_type`, `item_types`, `index_present_count`, `embedding_present_count`,
`index_types`, `embedding_types`, `value_types`. При отсутствии массива `data`
фактическое число vectors равно `unknown`, а не вымышленному нулю.
В логах есть только количества и типы полей, без значений vectors и полного JSON.

Если бот отвечает ошибкой, посмотреть последние диагностические записи:

```powershell
@'
import sqlite3

con = sqlite3.connect("data/app.db")
con.row_factory = sqlite3.Row
for row in con.execute("""
    SELECT id, article_id, incoming_message_id, stage, error_type, error_message, created_at
    FROM processing_errors
    ORDER BY id DESC
    LIMIT 10
"""):
    print(dict(row))
con.close()
'@ | python -
```

Основные таблицы для диагностики (не полный перечень схемы):

- `articles` — сохранённые статьи и их статус;
- `incoming_messages` — сообщения внешних каналов со ссылками;
- `analysis_results` — сохранённые LLM-сводки;
- `processing_errors` — диагностика ошибок загрузки, извлечения и анализа.
- `obsidian_notes` — полные Markdown-заметки активных vault;
- `obsidian_note_chunks` — структурные chunks текущей локальной копии;
- `obsidian_note_chunks_fts` — автоматически обновляемая FTS5-проекция chunks;
- `obsidian_chunk_index_states` — marker согласованного поколения parser/policy.
- `obsidian_note_chunk_index_states` — source blob SHA и signature chunks каждой
  заметки; по ним stale global marker восстанавливается без полного rebuild.
- `obsidian_proposals` — pending и завершённая история решений `add`/`update`/
  `skip`, исходные SHA и commit успешного write-back.

## Диагностика Obsidian write-back

При ответе `да` в логах искать `Obsidian proposal applied` или
`Obsidian proposal commit failed`. Логи содержат proposal/user/article IDs,
действие, target path и SHA, но не installation token и не полный Markdown.
Ошибки write-back также сохраняются в `processing_errors` со stage
`obsidian_write`.

После временного сбоя предложение остаётся `pending`, статья —
`needs_obsidian_review`, а `cleaned_text` и локальные vault SHA не меняются.
Повторить `да`: сервис заново читает remote state. Если commit был создан, но
его HTTP-ответ потерялся, совпадение remote Markdown завершает локальную
транзакцию без второго commit. Если HEAD/blob изменены другим автором, proposal
получает статус `conflict`; нужно повторно прислать исходную ссылку.

Sync и write-back используют одну таблицу lease. Ответ о занятом vault означает,
что операция идёт в другом связанном Telegram/VK-канале; pending proposal не
теряется, `да` можно повторить после завершения операции.

## Повторная отправка ответов каналов

Telegram и VK выполняют до трёх попыток отправки только при временных ошибках.
Предупреждение `message send retry` содержит канал, номер неудачной попытки,
задержку и тип ошибки. Запись `message send failed` означает, что ошибка постоянная
либо все разрешённые попытки исчерпаны. Текст ответа и credentials в retry-лог не
попадают.

VK использует неизменный `random_id` одной retry-серии. Telegram соблюдает
`TelegramRetryAfter`; из-за отсутствия idempotency key у Telegram редкий дубль
возможен, если API принял сообщение, но HTTP-ответ потерялся.

## Регистрация и выбор GitHub vault

Команда `/register` создаёт пользователя, запрашивает общее имя профиля и затем
просит прислать обычный HTTPS URL `github.com/owner/repository`. Имя можно
изменить через `/name <имя>`; внутренний `app_user_id` остаётся только в БД и
диагностических логах. Необязательный `vault-path` передаётся после URL, задаётся
относительно корня repository с `/`; пустой путь означает корень.
Проверка выполняется
краткоживущим installation token с `Contents: write`, ограниченным конкретным
repository. Token не сохраняется и не логируется. Публичный repository,
доступный App только для чтения, не может быть выбран как vault.

Если бот сообщает, что repository недоступен, проверить:

- GitHub App установлено именно на этот repository;
- изменение permission `Contents: read and write` одобрено для существующей
  installation;
- repository не находится в архивном или отключённом состоянии;
- URL не ведёт на branch, файл, Issues или другую вложенную страницу.

Если GitHub ещё не авторизован, Device Flow продолжается в background thread.
Vault manager открывает отдельное короткое SQLite-соединение для фонового
завершения и не использует закрытое request-соединение исходного сообщения.
При отсутствии installation бот даёт ссылку настройки App; после выбора
repository его URL нужно прислать повторно.

Замена активного vault хранится как подтверждение с TTL 10 минут и применяется
только после ответа `да`. Ответ `нет` сохраняет прежний vault. Без активного
подтверждения ответы `да` и `нет` не изменяют ни vault, ни связь каналов.

## Синхронизация GitHub vault

Первая синхронизация выполняется сразу после подключения или подтверждённой
замены vault. Ручная проверка запускается в любом связанном канале:

```text
/github_sync
```

Текущее подключение, количество локальных Markdown-заметок, instruction-файлов
и timestamps:

```text
/github_status
```

Одновременно один vault синхронизирует только один процесс; второй Telegram/VK
запрос получает сообщение о выполняющейся синхронизации. При ошибке GitHub
сохранённый source SHA не продвигается, lease освобождается, а последняя локальная
копия остаётся доступной. Installation token, содержимое Markdown и HTTP-ответы
GitHub не выводятся в логи.

Первая синхронизация и обновление с 50 или более изменёнными файлами используют
один ZIP snapshot конкретного commit SHA. Небольшое обновление скачивает только
изменённые blobs. В operational log поле `mode=archive` или `mode=blobs`
показывает выбранный путь вместе с количеством файлов и длительностью. GET имеет
read timeout 30 секунд и до трёх попыток; временный HTTP-ответ с `Retry-After`
задаёт задержку следующей попытки. ZIP ограничен 100 MiB и не распаковывается на
диск.

В корне vault обязателен `.knowledge-catcher.yml` версии `1` с непустым списком
`instructions`. Все пути относятся к корню vault и должны вести к существующим
UTF-8 файлам. Конфигурация и каждый instruction-файл проверяются до применения
нового snapshot. При ошибке source SHA не продвигается, последний полный набор
правил сохраняется, а ручной ответ содержит безопасную причину и готовый пример.
Такая ошибка не маскируется fallback из 9.10 и блокирует article pipeline до
исправления repository. Instruction-файлы хранятся отдельно и не считаются
Obsidian-заметками.

Перед каждой статьёй общий incoming flow проверяет возраст `last_checked_at`.
Внутри шестичасового окна GitHub-запросов нет. Если прошло шесть часов или
больше, первая следующая статья выполняет инкрементальную проверку под тем же
SQLite lease. Ошибка автоматической проверки не удаляет локальные заметки и не
останавливает article pipeline: статья обрабатывается с последней локальной
копией, а пользователь получает предупреждение. Занятый другим связанным каналом
lease обрабатывается так же. Ошибка GitHub остаётся в operational log только как
тип исключения без token, URL HTTP-ответа и содержимого Markdown.

Отключение активного vault:

```text
/github_disconnect
```

Команда не удаляет данные сразу: в SQLite создаётся подтверждение с TTL 10
минут. `да` удаляет строку vault, а SQLite каскадно очищает локальные заметки,
tags, wikilinks и lease. `нет` удаляет только подтверждение. GitHub account,
installation IDs, пользователь, identities, статьи и анализы сохраняются.
Сам GitHub repository команда не изменяет.

## Частые проблемы

- `TelegramConflictError`: уже запущен другой экземпляр бота с тем же token.
  Закрыть старое окно и запустить заново.
- `401 Unauthorized access` на stage `analysis`: неверный AI-agent API key или
  ключ вставлен в `.env` вместе с `Bearer`.
- Ошибка загрузки страницы: сайт недоступен, вернул неуспешный HTTP-статус или
  блокирует загрузку.
- Ошибка извлечения текста: HTML загрузился, но extractor не нашёл содержательный
  текст статьи.
