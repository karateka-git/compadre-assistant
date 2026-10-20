# Production VPS

Здесь находятся доступ, ограничения и **последний документированный снимок** VPS.
Он проверен 20.10.2026 и не доказывает состояние сервера сегодня. Перед любой
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

## Последний проверенный снимок — 20.10.2026

| Область | Результат проверки |
| --- | --- |
| Версия | `v1.0.4`, SHA `d6b7d9267e7754ee90957d9057e2a4eb41f789c1`, чистый detached HEAD; [GitHub Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v1.0.4). |
| Каналы | `tg_assistant` и `vk_assistant` на одном закреплённом image, оба `healthy`, restart count 0; реальные ответы подтверждены пользователем. |
| Контроллер | `stage=success`, `last_good.version=v1.0.4`, marker отсутствует; повторная установка вернула `already_installed`. |
| Данные | SQLite `integrity_check=ok`, схема `0001_initial_schema.sql`; рабочая БД сохранена при выпуске и перезагрузке. |
| Backup | Первая автоматическая ночная копия 20.10 прошла integrity check; timer `active/enabled`, хранится семь последних копий. Копии лежат на том же диске VPS. |
| systemd | `obs-chat-bot.service` активна; после удаления устаревшего drop-in загружены `channels.conf` и `zz-deploy-pinned.conf`. |

Это итог проверок выпуска, reboot и последующего обслуживания 19–20 октября.
Отдельная проверка контроллера и каналов после удаления старого drop-in не
создавала нового Release. Подробные заметки о прежних попытках доступны в
истории Git; действующие выводы для установки и возврата закреплены в
[OPERATIONS.md](OPERATIONS.md).

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
