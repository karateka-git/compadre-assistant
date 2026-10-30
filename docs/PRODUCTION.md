# Production VPS

Здесь находятся доступ, ограничения и **последний документированный снимок** VPS.
Он зафиксирован 30.10.2026 и не доказывает состояние сервера сегодня. Перед
любой работой повторно проверьте факты на VPS. Установка, диагностика, backup и
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

## Последний проверенный снимок — 30.10.2026, 04:33:27 UTC

| Область | Результат проверки |
| --- | --- |
| Версия и допуск | `v3.0.1`, SHA `4d58c9c09259f8f9b1578a467b20c5e6055a6bc2`; [GitHub Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v3.0.1). `release-full` завершился успешно для точного SHA, [run 41000609812](https://github.com/karateka-git/compadre-assistant/actions/runs/41000609812). |
| Установка и контроллер | `stage=success`, marker отсутствует, checkout — чистый detached HEAD `4d58c9c09259f8f9b1578a467b20c5e6055a6bc2`. Установленный controller snapshot `38bc8411e17d717e954e554f890d599eddd96b96` не менялся; 35 файлов совпали с candidate. |
| Каналы и образы | `tg_assistant` и `vk_assistant` — `healthy`, restart count 0, policy `unless-stopped`, image ID `sha256:5c2467f184e1dcb51dee7ade66932701cf89408d5e0e8ff39e22c5b7e31f75a3`; polling обоих каналов работает. |
| Инцидент и восстановление | `v3.0.0` перешёл в `degraded` из-за таймаута Docker healthcheck в 5 секунд. Выполнено ручное восстановление вперёд на `v3.0.1`; это recovery-процедура, а не обычный `deploy-prod`. Холодный Telegram healthcheck завершился с exit code 0 за 7.407 секунды, что подтверждает, что лимита 5 секунд недостаточно для этого сценария запуска. |
| Рабочая БД | `integrity_check=ok`, `foreign_key_check` пуст; применены `0001_initial_schema.sql`–`0004_private_chat_address_lookup.sql`. |
| Backup до миграции | `/var/lib/compadre-assistant-deploy/backups/v3.0.0-523ce4c595e7fe87771a10752944358a19c3bed8.db`; mode `0600`, `integrity_check=ok`, `foreign_key_check` пуст, схема содержит `0001`–`0002`. |
| Backup recovery | `/var/lib/compadre-assistant-deploy/backups/recovery-v301-20261030T043039155240Z.db`; mode `0600`, `integrity_check=ok`, `foreign_key_check` пуст, схема содержит `0001`–`0004`. Обе копии сохранены. |
| systemd | `obs-chat-bot.service` — `active/enabled`; `obs-chat-bot-backup.timer` — `active/enabled`. |
| Пользовательская приёмка | Отдельно от серверных наблюдений пользователь подтвердил `/help` и меню в личных диалогах Telegram и VK. Содержимое ответов и временные метки сообщений не записывались. |

Серверная часть снимка наблюдалась 30.10.2026 в 04:33:27 UTC после ручного
восстановления. Инцидент `v3.0.0` завершился продвижением вперёд на `v3.0.1`,
без возврата к старой схеме; backup до миграции и recovery backup сохранены.
Пользовательская приёмка выше приведена отдельно и не расширяет серверное
наблюдение. Технические команды и условия повторной проверки — в
[OPERATIONS.md](OPERATIONS.md); обычный допуск и `deploy-prod` описаны в
[RELEASES.md](RELEASES.md).

## Исторический снимок — 21.10.2026

На тот момент работал `v2.0.0` (`0d21ef0172ecf8024bd6c1583a8046fda798aec3`),
SQLite содержала миграции `0001`–`0002`, а backup перед `0002` сохранял схему
`0001` и проходил `integrity_check`. Controller snapshot был собран из SHA
`38bc8411e17d717e954e554f890d599eddd96b96`; полный CI релизной линии прошёл
[run 39601345018](https://github.com/karateka-git/compadre-assistant/actions/runs/39601345018).
Пользователь подтвердил личный выбор, связанные каналы, второй профиль,
долгий Ask и позднее сохранение режима после повторного включения Obsidian.
Тексты и временные метки ответов независимо не наблюдались; доступность
прежней статьи также не проверялась. Этот исторический результат не описывает
текущее состояние VPS.

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
