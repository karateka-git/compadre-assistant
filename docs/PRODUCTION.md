# Production VPS

Production переключён на Compadre Assistant 2026-10-13 по отдельному разрешению
пользователя, с остановкой старых каналов и чистой SQLite после проверенного backup.
Развёрнут SHA `ac2ac981ba628a881b483238a4eaf191a644de6a` из
`codex/assistant-rebirth`; checkout находится в detached HEAD и чистый.
Это выполненный переход; повторять очистку БД при обычном обновлении нельзя.
Подробная процедура и rollback: [DEPLOY_COMPADRE_ASSISTANT.md](DEPLOY_COMPADRE_ASSISTANT.md).

Существующая GitHub App 2026-10-10 переименована в **Compadre Assistant**,
ID `4363317` сохранён, новый slug — `compadre-assistant`. Локальная конфигурация
обновлена и чтение прежнего vault через действующий PEM проверено.
2026-10-13 в production выполнено отдельное исправление конфигурации:
`GITHUB_APP_SLUG` изменён с `my-knowledge-catcher` на `compadre-assistant`.
App ID, Client ID, PEM и остальные переменные окружения сохранены.

Этот документ — контекст для агента, который продолжает эксплуатацию или
развёртывание проекта. Он не содержит ключи, токены, содержимое `.env` или PEM.

Сведения ниже — контекст развёртывания. Проверка 2026-09-29 подтвердила
systemd unit, его override и backup timer, но состояние сервиса и сети может
измениться. Перед эксплуатационными действиями проверяйте их заново.
Unit-файлы systemd и backup script в репозитории отсутствуют.
Текущий snapshot после перехода приведён ниже; старые проверки сохранены как история.

## Доступ

- VPS: `user@91.188.213.142`.
- Локальный закрытый SSH-ключ на текущем рабочем компьютере:
  `C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka`.
- Публичная часть ключа: тот же путь с суффиксом `.pub`.
- Ключ не копировать в репозиторий, чат или логи. На другом компьютере создать
  или безопасно получить отдельный SSH-ключ, затем добавить его в H3LLO.
- Для чтения кода на VPS используется отдельный GitHub deploy key в
  `/home/user/.ssh/obs-chat-bot_deploy`. Он имеет доступ только на чтение к
  `karateka-git/compadre-assistant` (до 2026-10-10 — `karateka-git/obsChatBot`).

Подключение с текущего компьютера:

```powershell
ssh -i "C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka" user@91.188.213.142
```

## Состояние развёртывания

- ОС: Ubuntu 26.04.1 LTS.
- Проект на VPS: `/opt/obs-chat-bot`.
- Код: detached HEAD `ac2ac981ba628a881b483238a4eaf191a644de6a`.
- Origin: `git@github.com:karateka-git/compadre-assistant.git`.
  Fetch refspec ограничен `main`; другие ветки получать явным refspec из runbook.
- Runtime: `compadre_assistant`, Compose `tg_assistant`/`vk_assistant`,
  image `compadre-assistant:dev`, Docker 29.8.0 / Compose v5.5.1.
- Telegram: `@CompadreAssistant_bot`, ID `8870763711`; VK group ID `240322403`.
- Включены `obsidian,ask` и `telegram,vk`, `APP_DEBUG=false`.
  Постоянный Ask: `https://api.timeweb.ai/v1`, `dashscope/qwen3.5-flash`;
  источник ключа явно утверждён пользователем, значение не публикуется.
- Конфигурация: `/opt/obs-chat-bot/.env`, права `600`, не хранится в Git.
- GitHub App PEM: `/opt/obs-chat-bot/data/github-app.pem`, права `600`, не
  хранится в Git.
- Production SQLite: `/opt/obs-chat-bot/data/app.db`.
- Дополнительный исходящий путь к Telegram подготовлен: H3LLO `wgobs`
  (`10.77.77.2/30`) соединён с Timeweb `wgobs` (`10.77.77.1/30`, UDP
  `51831`); Dante слушает SOCKS5 только на `10.77.77.1:1080` и разрешает
  подключения только от `10.77.77.2`. Исходящий маршрут H3LLO по умолчанию
  не изменён. Конфигурация и закрытые ключи WireGuard хранятся только в
  `/etc/wireguard/` соответствующих серверов.
- Запуск после reboot: `obs-chat-bot.service` через systemd. Override
  `/etc/systemd/system/obs-chat-bot.service.d/channels.conf` запускает и
  останавливает `vk_assistant` и `tg_assistant`; проверен перезапуском unit.
- Ночные локальные SQLite-копии: `obs-chat-bot-backup.timer`; запуск в 03:15
  UTC, семь последних файлов в `/var/backups/obs-chat-bot`.

### Результат переключения 2026-10-13

После controlled restart оба новых контейнера healthy, restarts=0,
restart policy `unless-stopped`. Безопасная проверка журналов подтвердила
Telegram polling и VK Long Poll; traceback и TelegramConflictError отсутствовали.
GetMe через штатный proxy подтвердил новый ID; VK getLongPollServer успешен.
Проверка процессов: два новых runtime, ни одного старого. Старые контейнеры
остановлены и оставлены для rollback. PEM checksum прежний; конфигурация
изменилась только в согласованных ключах. Новая SQLite прошла integrity check.
До остановки короткий Ask API-запрос с утверждённым профилем вернул HTTP 200.

Backup перед переходом: `app-20261013T074041Z.db`; после запуска и restart:
`app-20261013T074327Z.db`. Service и backup timer активны, следующий запуск
на момент проверки — 2026-10-14 03:15 UTC. Rollback bundle:
`/home/user/.compadre-deploy/rollback-r5iZV59g` (каталог 700, файлы 600),
содержит прежний env/override/SHA/origin и `app-before.db` с проверенной integrity.
Прежний image сохранён как `compadre-assistant:rollback`.
Staging `secrets.env` удалён; срок удаления rollback bundle ещё не согласован.

Открыта пользовательская приёмка в обоих каналах: регистрация, привязка,
независимые режимы, Ask с подтверждением и без истории, Device Flow,
подключение/синхронизация тестового vault, подтверждение записи и отмена.
Пользователь выбрал для приёмки vault `karateka-git/my_obs_data`;
Device Flow и подтверждение записи выполняет самостоятельно. Рассылка и удаление старого
бота не выполнялись. Этап 4 целиком не закрыт.

### Исторические проверки до переключения

Проверка 2026-09-29: код обновлён до `5dc80ca`, оба контейнера работают
(`healthy`, ноль автоматических перезапусков), Docker подтвердил
`restart=unless-stopped`. `obs-chat-bot.service` перезапущен и восстановил
оба канала; backup timer активен. Healthcheck прошёл, Telegram `getMe` через
прокси успешен, запуск Telegram polling и VK Long Poll подтверждён журналом.
Пользователь подтвердил ответ Telegram-бота на `/status`; обработка update
подтверждена журналом контейнера.

## Проверенный сценарий

2026-10-13 выполнен точечный hotfix GitHub App slug. Перед изменением
подтверждены чистая рабочая копия VPS, активные systemd service/backup timer
и `healthy` у обоих контейнеров. Исходный `.env` сохранён с правами `600` в
`/home/user/.local/state/obs-chat-bot-hotfix/20261013T070547Z/.env`.
Файл конфигурации заменён атомарно; побайтовая проверка подтвердила, что
изменена только строка `GITHUB_APP_SLUG`. Пересозданы только `tg_catcher` и
`vk_catcher` через Compose с `--no-build --pull never --no-deps`.
После перезапуска оба контейнера `healthy`, имеют новый slug и ноль
автоматических перезапусков; остальные env, image ID, команды, mounts и
restart policy совпадают с состоянием до hotfix. Systemd service и backup
timer активны. Код остался на `77686d5221c39543885f35fe90b70b7e6bfae723`,
рабочая копия чистая, контрольная сумма PEM прежняя; SQLite не пересоздавалась
(проверен прежний inode файла). Новая сборка не развёрнута.

Проверка 2026-10-10 после переименования GitHub-репозитория выполнена только
чтением: рабочая копия VPS чистая; deploy key успешно читает `HEAD` как по
старому, так и по новому SSH-адресу. GitHub сохранил read-only deploy key
`H3llo VPS` (ID `163139906`). `obs-chat-bot.service` и backup timer активны,
оба прежних контейнера `tg_catcher`/`vk_catcher` имеют статус `healthy`.
Unit, override и backup script проверены: они используют локальные пути
`/opt/obs-chat-bot`, `/usr/local/lib/obs-chat-bot` и `/var/backups/obs-chat-bot`,
а не URL репозитория. Само переименование GitHub не требует изменения этих
путей. Переход на новые Compose-сервисы остаётся отдельным действием при
развёртывании. VPS, его конфигурация, процессы и SQLite не изменялись.

VK работает в production: регистрация, подключение vault
`karateka-git/my_obs_data`, синхронизация, embeddings, обработка статьи и
подтверждённый GitHub write-back проверены.

Прямое HTTPS-соединение с `api.telegram.org` с H3LLO недоступно. Telegram
использует выделенный WireGuard и SOCKS5; `TELEGRAM_PROXY_URL` хранится в
серверном `.env` с правами `600`. Получение данных бота и polling с VPS
работают. Ответ на реальную команду `/status` проверен пользователем;
результат отмечен в [истории](HISTORY.md#доступ-telegram-bot-api-с-российских-vps).

## Базовые команды

```bash
ssh user@91.188.213.142
cd /opt/obs-chat-bot
sudo systemctl status obs-chat-bot.service
docker compose ps
docker compose ps -q | xargs -r docker inspect --format '{{.Name}} {{.State.Status}} restarts={{.RestartCount}}'
```

Для healthcheck текущей сборки:

```bash
cd /opt/obs-chat-bot
docker compose run --rm -T --no-deps tg_assistant python -m compadre_assistant --healthcheck --channels telegram,vk </dev/null
```

Обычное обновление выполнять на конкретный опубликованный SHA с планом
сохранения данных; `git pull` на текущем detached HEAD не использовать.
Разрешение очистки SQLite 2026-10-13 не распространяется на будущие обновления.
Не публиковать полные logs/inspect; выводить только безопасные поля и итоги.

После следующих обновлений проверить оба сервиса, журналы и
`RestartPolicy.Name=unless-stopped` через `docker inspect`; не очищать
production SQLite. Для функциональной проверки отправить сообщения обоим
ботам и проверить ответы.

Подробная эксплуатационная инструкция — в [OPERATIONS.md](OPERATIONS.md).

## Правила для агента

- Перед изменениями проверить `git status`, статус systemd и Docker Compose.
- Не выводить и не передавать `.env`, PEM, SSH private key, API tokens или
  содержимое production SQLite.
- Не удалять production SQLite и не применять development-правила очистки БД.
- Перед `git push` требуется явное согласие пользователя.
- Перед изменениями на VPS описать пользователю затрагиваемое действие и
  проверить результат после выполнения.
