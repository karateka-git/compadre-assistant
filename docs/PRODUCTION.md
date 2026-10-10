# Production VPS

Локальная версия после шага 4.2 использует пакет `compadre_assistant` и
Compose-сервисы `tg_assistant`/`vk_assistant`. Приведённые ниже production-пути
и имена сервисов описывают прежнее развёртывание. Перед выкладкой новой версии
нужно отдельно обновить команды и systemd override, остановить прежние сервисы
и исключить параллельный polling; обычный рецепт обновления прежней версии
не является инструкцией перехода на переименованную сборку.

Этот документ — контекст для агента, который продолжает эксплуатацию или
развёртывание проекта. Он не содержит ключи, токены, содержимое `.env` или PEM.

Сведения ниже — контекст развёртывания. Проверка 2026-09-29 подтвердила
systemd unit, его override и backup timer, но состояние сервиса и сети может
измениться. Перед эксплуатационными действиями проверяйте их заново.
Unit-файлы systemd и backup script в репозитории отсутствуют.

## Доступ

- VPS: `user@91.188.213.142`.
- Локальный закрытый SSH-ключ на текущем рабочем компьютере:
  `C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka`.
- Публичная часть ключа: тот же путь с суффиксом `.pub`.
- Ключ не копировать в репозиторий, чат или логи. На другом компьютере создать
  или безопасно получить отдельный SSH-ключ, затем добавить его в H3LLO.
- Для чтения кода на VPS используется отдельный GitHub deploy key в
  `/home/user/.ssh/obs-chat-bot_deploy`. Он имеет доступ только на чтение к
  `karateka-git/obsChatBot`.

Подключение с текущего компьютера:

```powershell
ssh -i "C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka" user@91.188.213.142
```

## Состояние развёртывания

- ОС: Ubuntu 26.04.1 LTS.
- Проект на VPS: `/opt/obs-chat-bot`.
- Код: ветка `main` репозитория `karateka-git/obsChatBot`.
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
  останавливает `vk_catcher` и `tg_catcher`; проверен перезапуском unit.
- Ночные локальные SQLite-копии: `obs-chat-bot-backup.timer`; запуск в 03:15
  UTC, семь последних файлов в `/var/backups/obs-chat-bot`.

Проверка 2026-09-29: код обновлён до `5dc80ca`, оба контейнера работают
(`healthy`, ноль автоматических перезапусков), Docker подтвердил
`restart=unless-stopped`. `obs-chat-bot.service` перезапущен и восстановил
оба канала; backup timer активен. Healthcheck прошёл, Telegram `getMe` через
прокси успешен, запуск Telegram polling и VK Long Poll подтверждён журналом.
Пользователь подтвердил ответ Telegram-бота на `/status`; обработка update
подтверждена журналом контейнера.

## Проверенный сценарий

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
docker compose logs --tail=100 vk_catcher
docker compose logs --tail=100 tg_catcher
```

После обновления кода:

```bash
cd /opt/obs-chat-bot
git pull --ff-only
docker compose up -d --build vk_catcher tg_catcher
docker compose run --rm tg_catcher python -m obs_chat_bot --healthcheck
```

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
