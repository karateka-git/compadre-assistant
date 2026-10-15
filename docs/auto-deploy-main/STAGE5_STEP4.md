# Этап 5, шаг 4: отказные проверки и runbook первого выпуска

Этот документ — подготовка. Команды установки, остановки, reboot и выпуска **не
выполнялись** на production. Перед шагом 5.5 заново прочитать
[`docs/PRODUCTION.md`](../PRODUCTION.md), снять текущее состояние и получить
эксплуатационное разрешение. Текущий production SHA, image ID, состав unit и
схема из прошлой инвентаризации не заменяют повторную проверку.

## Что проверено локально

- Fixture GitHub selector отклоняет неверный Release/tag/SHA/CI, ошибку API и
  смену ветки в ходе выбора. Контроллер повторяет полное сравнение selector
  после fetch и непосредственно перед критической секцией.
- Межпроцессный lock держится вне checkout. Контроллер записывает `preparing`
  **до** смены checkout; после смерти процесса при build следующий вызов
  восстанавливает прежний SHA, сверив оба работающих канала и внешний pin.
  Неудачный возврат checkout сохраняет manifest для следующего recovery.
- Fault injection на временной SQLite и fake host покрывает сбой записи state
  после marker, отказ backup/migrate, один нездоровый канал, повтор/reboot и
  исходное состояние timer. Возврат не заменяет БД снимком автоматически.
- Docker Compose v5.5.1 локально подтвердил normal/critical merge, один image
  tag, `pull_policy: never`, critical `restart: no`, то же имя проекта,
  mounts и build context при внешнем `COMPOSE_FILE`.
- Три drop-in прошли `systemd-analyze verify` в изолированном Debian
  container с искусственными базовыми unit. Это проверка синтаксиса и merge,
  а не boot настоящего VPS.
- `--verify-pinned` выполняет контролируемый stop/start обоих каналов на старом
  pin с тем же lock, marker, timer и rollback, без нового Release и миграции.

## Порядок первого выпуска на VPS (только шаг 5.5)

1. Получить отдельно утверждённый точный SHA контроллера и версию
   опубликованного Release. Проверить чистый detached checkout, origin,
   `systemctl cat` всех трёх unit, `NeedDaemonReload=no`, active/enabled
   backup timer, оба `healthy`, фактический одинаковый `.Image`, policy
   `unless-stopped`, текущие `schema_migrations` и `PRAGMA integrity_check`.
   Сохранить SHA, image ID, состояние timer и результаты без `.env`, PEM,
   токенов и содержимого SQLite. Проверить доступность GitHub API, deploy key,
   fingerprint host key и свободное место. Не считать Docker tag доказательством
   фактического старого image ID.
2. Из **проверенного** commit установить snapshot `scripts/` и `deploy/` вне
   checkout в `/usr/local/lib/compadre-assistant-deploy` с владельцем root;
   поставить `deploy/deploy-prod` как `/usr/local/sbin/deploy-prod` (0755).
   Сверить `sha256sum` или `cmp` с проверенным snapshot. Не копировать `.env`,
   `data/`, PEM или SQLite. Если Git root не читает checkout из-за ownership,
   разрешить `safe.directory` только `/opt/obs-chat-bot`.
3. При двух работающих каналах привязать
   `compadre-assistant:release-<СТАРЫЙ_ПОЛНЫЙ_SHA>` к **фактическому старому
   image ID** командой `docker image tag <СТАРЫЙ_IMAGE_ID> <СТАРЫЙ_TAG>`;
   `docker image inspect <СТАРЫЙ_TAG> --format '{{.Id}}'` должен вернуть
   записанный ID. Установить оба Compose override в
   `/var/lib/compadre-assistant-deploy` (каталог 0700). Атомарно создать
   `runtime.env` (0600) ровно с `ASSISTANT_IMAGE=<СТАРЫЙ_TAG>` и
   `COMPOSE_FILE=/opt/obs-chat-bot/docker-compose.yml:/var/lib/compadre-assistant-deploy/pinned-compose.yml`.
4. Установить три drop-in из `deploy/` по путям шага 5.3, не заменяя базовые
   unit, `channels.conf` и backup script. Выполнить `systemctl daemon-reload`,
   `systemd-analyze verify obs-chat-bot.service obs-chat-bot-backup.timer
   obs-chat-bot-backup.service`, `systemctl cat` и проверить
   `NeedDaemonReload=no`. В root shell загрузить **только** эти две строки
   `runtime.env`, вызвать `docker compose config --format json` и сверить
   project name с прежним, одинаковый image обоих сервисов,
   `pull_policy=never`, `restart=unless-stopped`, mounts и build context.
   С добавленным `critical-compose.yml` restart обоих должен быть `no`.
5. Запустить `sudo deploy-prod --verify-pinned` в контролируемое окно. Проверить
   `stage=pinned_verified`, одинаковый прежний image ID и `healthy` обоих,
   признаки polling в коротких журналах без полного вывода env/logs,
   restart count 0, active service и прежнее состояние backup timer. При
   отказе команда пытается вернуть **оба** канала; `degraded` означает
   остановку и ручное восстановление по разделу ниже.
6. Только после успешного старого pin выполнить отдельно согласованный reboot.
   После загрузки снова сверить detached SHA, image ID обоих каналов, health,
   polling, policy, active service, timer и отсутствие marker. Проверить
   отсутствие второго Telegram/VK polling по Docker и эксплуатационному
   осмотру внешних процессов. Этот опыт нельзя заменить local test.
7. Непосредственно перед выпуском заново проверить опубликованный Release и
   точный CI run/attempt: `sudo deploy-prod vX.Y.Z`. Команда синхронна;
   не запускать второй экземпляр. При успехе проверить `state.json`:
   `stage=success`, `last_good` с SHA, image ID, backup ID и CI attempt;
   сверить checkout, оба контейнера, SQLite integrity/schema, active service
   и исходное состояние timer. Повтор той же версии должен вывести
   `already_installed` без пересоздания. Владелец проверяет реальные ответы
   Telegram и VK до признания выпуска принятым.

Команды установки для пунктов 2–4 выполняются в root shell **только после**
предыдущей инвентаризации и подстановки проверенного `CTRL_SHA`. Архивировать
нужно commit с той же утверждённой версией контроллера, которая прошла review;
его scripts/deploy не должны меняться в server checkout при последующем
`git checkout --detach` candidate:

```bash
cd /opt/obs-chat-bot
CTRL_SHA=<ПРОВЕРЕННЫЙ_ПОЛНЫЙ_SHA_КОНТРОЛЛЕРА>
git cat-file -e "$CTRL_SHA^{commit}"
install -d -m 0700 /usr/local/lib/compadre-assistant-deploy
git archive "$CTRL_SHA" scripts deploy | tar -x -C /usr/local/lib/compadre-assistant-deploy
chown -R root:root /usr/local/lib/compadre-assistant-deploy
install -o root -g root -m 0755 /usr/local/lib/compadre-assistant-deploy/deploy/deploy-prod /usr/local/sbin/deploy-prod

OLD_SHA=$(git rev-parse HEAD)
TG_ID=$(docker compose ps -q tg_assistant)
VK_ID=$(docker compose ps -q vk_assistant)
OLD_IMAGE_ID=$(docker inspect "$TG_ID" --format '{{.Image}}')
test "$OLD_IMAGE_ID" = "$(docker inspect "$VK_ID" --format '{{.Image}}')"
OLD_TAG="compadre-assistant:release-$OLD_SHA"
docker image tag "$OLD_IMAGE_ID" "$OLD_TAG"
test "$(docker image inspect "$OLD_TAG" --format '{{.Id}}')" = "$OLD_IMAGE_ID"

install -d -m 0700 /var/lib/compadre-assistant-deploy
install -m 0600 /usr/local/lib/compadre-assistant-deploy/deploy/pinned-compose.yml /var/lib/compadre-assistant-deploy/pinned-compose.yml
install -m 0600 /usr/local/lib/compadre-assistant-deploy/deploy/critical-compose.yml /var/lib/compadre-assistant-deploy/critical-compose.yml
umask 077
TMP_RUNTIME=$(mktemp /var/lib/compadre-assistant-deploy/.runtime-XXXXXX)
printf 'ASSISTANT_IMAGE=%s\nCOMPOSE_FILE=/opt/obs-chat-bot/docker-compose.yml:/var/lib/compadre-assistant-deploy/pinned-compose.yml\n' "$OLD_TAG" >"$TMP_RUNTIME"
mv "$TMP_RUNTIME" /var/lib/compadre-assistant-deploy/runtime.env

install -d /etc/systemd/system/obs-chat-bot.service.d /etc/systemd/system/obs-chat-bot-backup.timer.d /etc/systemd/system/obs-chat-bot-backup.service.d
install -m 0644 /usr/local/lib/compadre-assistant-deploy/deploy/obs-chat-bot-pinned.conf /etc/systemd/system/obs-chat-bot.service.d/90-deploy-pinned.conf
install -m 0644 /usr/local/lib/compadre-assistant-deploy/deploy/obs-chat-bot-backup-guard.conf /etc/systemd/system/obs-chat-bot-backup.timer.d/90-deploy-guard.conf
install -m 0644 /usr/local/lib/compadre-assistant-deploy/deploy/obs-chat-bot-backup-service-guard.conf /etc/systemd/system/obs-chat-bot-backup.service.d/90-deploy-guard.conf
systemctl daemon-reload
systemd-analyze verify obs-chat-bot.service obs-chat-bot-backup.timer obs-chat-bot-backup.service
systemctl cat obs-chat-bot.service obs-chat-bot-backup.timer obs-chat-bot-backup.service
systemctl show --property=NeedDaemonReload obs-chat-bot.service obs-chat-bot-backup.timer obs-chat-bot-backup.service
set -a
. /var/lib/compadre-assistant-deploy/runtime.env
set +a
docker compose config --format json
```

После проверок и отдельного согласования окна:

```bash
deploy-prod --verify-pinned
# После подтверждения старого pin и отдельного согласования reboot:
systemctl reboot
# После повторной инвентаризации и проверки Release:
deploy-prod vX.Y.Z
```

Эти команды не следует выполнять одним paste-блоком: после каждого этапа
сверяются фактические результаты и условия следующего шага.

## Отказ, reboot и ручное восстановление

`state.json` и `deploy-in-progress` находятся в
`/var/lib/compadre-assistant-deploy`. Не удалять marker, lock, backup или
state ради повторной попытки. Сначала читать stage, фактические контейнеры,
runtime pin, checkout, timer и SQLite integrity/schema. Не публиковать полный
`docker inspect`, `.env`, PEM, логи или содержимое БД.

| Наблюдение | Действие |
| --- | --- |
| `failed_preflight`; marker нет, старые каналы работают | Исправить причину. `failed_version` и `retry_after` закрывают немедленный повтор той же версии; новый запуск после времени допуска всё равно повторяет selector и все проверки. |
| `preparing`/`ready` без marker после прерывания | Один повтор `sudo deploy-prod <та же версия>` выполняет только recovery checkout и завершается отказом. Проверить возвращённый старый SHA и каналы перед новым запуском. |
| Marker есть, stage между `ready` и `rollback_started`, схема совпадает с `schema_before` | Один повтор команды выполняет автоматический возврат старого pin и завершается отказом. Проверить `rolled_back`, оба канала, service и timer; новый выпуск запускать отдельно. |
| `degraded`, неверная схема или SQLite integrity | Оставить marker, остановить оба writer и timer. Не запускать старый код на неизвестной схеме и не подменять БД backup автоматически. Составить отдельный план совместимости/восстановления с оценкой записей после backup и возможных GitHub write-back. |
| `success`, но фактическое состояние расходится | Не объявлять выпуск успешным повторно. Сохранить журнальные stage и факты; вернуть оба канала только после доказательства совместимости текущей схемы со старым кодом. |

Если автоматический возврат не подтвердился, оператор сначала удерживает
`deploy-in-progress`, отключает backup timer и проверяет, что **оба** writers
остановлены. Затем проверяет точный `old_image_id` и `old_tag` из state,
`schema_before`, целостность текущей БД и совместимость со старым кодом.
Только при подтверждённой прежней схеме можно вручную вернуть старый detached
SHA, атомарно записать прежний tag в `runtime.env`, запустить оба контейнера
с pinned+critical Compose override и проверить оба image ID/health/polling.
После этого восстановить policy `unless-stopped`, снять marker, запустить
штатный service, повторить проверки и вернуть timer к сохранённым
`enabled`/`active`. Зафиксировать фактический результат и привести
`state.json` к проверенному `rolled_back` атомарно; до этого новая команда
заблокирована. Если хотя бы одна проверка невозможна, сохранить `degraded`
и не считать ручной возврат завершённым.

## Не закрыто этим шагом

- `0002+` всё ещё fail closed до остановки каналов: автоматическая проверка
  совместимости новой схемы с прежним runtime не реализована.
- После снятия marker и до записи `success` при внезапном reboot systemd может
  поднять candidate; state останется незавершённым. Сначала выполнить recovery,
  не считать boot доказательством успешного выпуска.
- Реальный порядок systemd и reboot, гонка ночного backup на VPS, живые
  Telegram/VK, внешние дубликаты polling и сохранность пользовательских данных
  могут быть подтверждены только на шаге 5.5.
- Dockerfile закрепляет base digest, но зависимости при server build
  скачиваются заново; SHA кода не гарантирует идентичный с CI image ID.
