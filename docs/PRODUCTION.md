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

## Последний проверенный снимок — 30.10.2026

| Область | Результат проверки |
| --- | --- |
| Версия и допуск | `v3.0.1`, SHA `4d58c9c09259f8f9b1578a467b20c5e6055a6bc2`; [GitHub Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v3.0.1). `release-full` завершился успешно для точного SHA, [run 41000609812](https://github.com/karateka-git/compadre-assistant/actions/runs/41000609812). |
| Каналы | `tg_assistant` и `vk_assistant` — `healthy`, restart count 0; polling обоих каналов работает. Пользователь подтвердил `/help` и меню в личных диалогах Telegram и VK. |
| Инцидент и восстановление | Предыдущий `v3.0.0` перешёл в `degraded` после таймаута Docker healthcheck в 5 секунд. Выполнено ручное восстановление вперёд на `v3.0.1`; это recovery-процедура, а не обычный `deploy-prod`. |
| Данные | Рабочая SQLite: `integrity_check=ok`, `foreign_key_check` пуст, применены `0001_initial_schema.sql`–`0004_private_chat_address_lookup.sql`. |
| Backup | Проверены и сохранены целыми backup до миграции и backup, созданный для recovery. |
| systemd | `obs-chat-bot.service` и `obs-chat-bot-backup.timer` — `active/enabled`. |

Снимок 30.10.2026 отражает проверенное состояние после ручного восстановления
и отдельно пользовательское подтверждение `/help` и меню в обоих личных
каналах. Инцидент `v3.0.0` не означает откат БД или восстановление старой
копии: результатом стало продвижение вперёд на `v3.0.1`, при этом обе проверенные
резервные копии сохранены. Технические команды и условия повторной проверки —
в [OPERATIONS.md](OPERATIONS.md); обычный допуск и `deploy-prod` описаны в
[RELEASES.md](RELEASES.md).

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
