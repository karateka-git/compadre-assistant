# Production VPS

Здесь находятся доступ, ограничения и **последний документированный снимок** VPS.
Он проверен 21.10.2026 и не доказывает состояние сервера сегодня. Перед любой
работой повторно проверьте факты на VPS. Установка, диагностика, backup и
восстановление описаны в [OPERATIONS.md](OPERATIONS.md), допуск версии — в
[RELEASES.md](RELEASES.md).

## Доступ

- VPS: `user@91.188.213.142`; приложение находится в `/opt/obs-chat-bot`.
- Закрытый SSH-ключ на текущем рабочем компьютере:
  `C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka`.
  Публичная часть ключа — тот же путь с суффиксом `.pub`.
- Ключ не копировать в репозиторий, чат или логи. Для другого компьютера
  создать или безопасно получить отдельный ключ и добавить его в H3LLO.
- Для чтения кода на VPS используется отдельный GitHub deploy key
  `/home/user/.ssh/obs-chat-bot_deploy` с доступом только на чтение к
  `karateka-git/compadre-assistant`.

```powershell
ssh -i "C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka" user@91.188.213.142
```

## Последний проверенный снимок — 21.10.2026

| Область | Результат проверки |
| --- | --- |
| Версия | `v2.0.0`, SHA `0d21ef0172ecf8024bd6c1583a8046fda798aec3`, чистый detached HEAD; [GitHub Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v2.0.0). |
| Каналы | `tg_assistant` и `vk_assistant` на image ID `sha256:db9ef5fd808474349ee4f182de7febe1df9d647635d948574be0d3fcf86e938e`, оба `healthy`, restart count 0, policy `unless-stopped`. В 13:36 UTC container ID, PID и `StartedAt` обоих совпали со снимком до личных переключений. Пользователь подтвердил живые сценарии Telegram/VK, второго профиля и долгого Ask; тексты и timestamps ответов независимо не наблюдались. |
| Контроллер | Отдельный snapshot из проверенного SHA `38bc8411e17d717e954e554f890d599eddd96b96` (`release/2.0`); архив `scripts/` и `deploy/` побайтно сверен с Git, SHA-256 `dcec561a81f6de071946a8d095788a4e87d741fe80bc040a8db939a9f6c0ffc8`. Прежний snapshot сохранён в `/usr/local/lib/compadre-assistant-deploy.previous-v1.0.4-38bc841`. После `sudo deploy-prod v2.0.0`: `stage=success`, `last_good.version=v2.0.0`, marker отсутствует. |
| Данные | Рабочая SQLite: `integrity_check=ok`, `foreign_key_check` пуст, миграции `0001_initial_schema.sql` и `0002_user_capability_preferences.sql`. |
| Backup | Проверенная копия перед `0002`: `/var/lib/compadre-assistant-deploy/backups/v2.0.0-0d21ef0172ecf8024bd6c1583a8046fda798aec3.db`, права `0600`, `integrity_check=ok`, только `0001`; ночной timer `active/enabled`. Копии лежат на том же диске VPS. |
| systemd | `obs-chat-bot.service` активна; после установки release и обновления контроллера marker отсутствует. |

Проверка выполнена по Git, controller state, Docker Compose, systemd и
SQLite на VPS после установки 21.10.2026. До установки старый контроллер
остановил попытку `v2.0.0` на `failed_preflight` из-за новой `0002`, не меняя
БД и каналы. Исправление прошло [полный CI релизной линии](https://github.com/karateka-git/compadre-assistant/actions/runs/39601345018), после чего контроллер обновлён отдельно и повторная установка завершилась успешно. Порядок установки и возврата — в [OPERATIONS.md](OPERATIONS.md).

Read-only повторная проверка 21.10.2026 в 13:36 UTC сохранила `stage=success`,
чистый SHA checkout, обе миграции, `integrity_check=ok`, отсутствие ошибок
foreign keys и marker, активные unit и backup timer. Пользователь сообщил
«Проверил всё перечисленное» о живой проверке личного выбора, связанных
каналов, второго профиля и долгого Ask. Это пользовательское свидетельство;
наблюдаемые на VPS факты о процессах приведены выше. Тексты сообщений и
временные метки порядка Ask/disable не получены, поэтому они не входят в
независимую техническую проверку.

## Базовые команды

```bash
cd /opt/obs-chat-bot
sudo systemctl status obs-chat-bot.service
sudo systemctl is-active obs-chat-bot-backup.timer
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
Проверить оба канала, polling, журналы, `RestartPolicy.Name=unless-stopped`,
SQLite integrity и backup timer; для реальных ответов требуется
пользовательская проверка. Полные logs и inspect не публиковать.

## Ограничения для работы с VPS

- Перед изменением проверить `git status`, systemd, Docker Compose и
  состояние контроллера. Согласовать эксплуатационное действие с пользователем
  и проверить результат после выполнения.
- Не выводить и не передавать `.env`, PEM, SSH private key, API tokens или
  содержимое production SQLite.
- Не удалять production SQLite и не применять к ней правила очистки
  development-БД. Изменение схемы требует новой миграции с сохранением данных.
- Перед `git push` требуется явное согласие пользователя.
