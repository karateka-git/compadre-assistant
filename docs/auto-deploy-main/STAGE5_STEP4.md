# Этап 5, шаг 4: отказные проверки и runbook первого выпуска

Этот документ — подготовка и журнал прерванной первой установки. Остановка,
reboot и выпуск **не выполнялись** на production. Перед продолжением шага 5.5 заново прочитать
[`docs/PRODUCTION.md`](../PRODUCTION.md), снять текущее состояние и получить
эксплуатационное разрешение. Текущий production SHA, image ID, состав unit и
схема из прошлой инвентаризации не заменяют повторную проверку.
По явному решению пользователя этот временный runbook сохраняется через merge
и используется для первого live выпуска. После выполнения всех проверок
плана, включая аварийный маршрут и эксплуатационную приёмку, проверенный
порядок и recovery переносятся в `docs/OPERATIONS.md`; временный каталог
удаляется отдельным завершающим изменением после проверки ссылок.

## Состояние после первого окна 2026-10-17

Опубликованный `v1.0.0` указывает на
`33ae77a0e81cacf0a346376c0609681d30e0c317`; `main-full` run
`39042879438`, attempt 1, включая `full-suite`, успешен. До переключения
обнаружена ошибка порядка systemd drop-in: установленный
`90-deploy-pinned.conf` загружается **перед** существующим `channels.conf`,
который перекрывает `ExecStart`. Effective команда осталась без `--no-build
--pull never`. Контролируемый `--verify-pinned` поэтому **не запускался**.

На VPS остаются root-owned snapshot контроллера именно из SHA `v1.0.0`,
`/usr/local/sbin/deploy-prod`, старый Docker tag, оба Compose override,
`runtime.env` и три drop-in с именами `90-*`. `deploy-in-progress` и
`state.json` отсутствуют. Checkout чистый detached на прежнем SHA
`ac2ac981ba628a881b483238a4eaf191a644de6a`; оба контейнера продолжили
работу на старом image ID
`sha256:1b7521619423d8607ec449d3dcabdac009334d8833d6f71874c670f2d55f0743`
с `healthy`, нулём рестартов и `unless-stopped`; service и backup timer
остались active/enabled. Этот частичный setup сам по себе не доказывает
безопасный запуск после reboot: до исправления не выполнять reboot,
`--verify-pinned` или `deploy-prod v1.0.0`.

Исправление готовится как новый PATCH `v1.0.1` после двух связанных PR,
успешного `release-full` точного SHA и отдельного решения о публикации.
Защищённый тег `v1.0.0` не изменять и не использовать для первого live
переключения. До появления проверенного контроллера, безопасной замены
snapshot и новой эксплуатационной приёмки шаг 5.5 открыт.

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

Для исправления порядка отдельный Ubuntu 24.04 container с systemd 255
загрузил synthetic base unit, точный проверенный `obs-chat-bot-pinned.conf`
и копию production `channels.conf`. При именах `90-deploy-pinned.conf`,
`channels.conf` effective `ExecStart` оказался без `--no-build --pull never`.
После добавления идентичного `zz-deploy-pinned.conf` порядок стал
`90`, `channels`, `zz`, а effective команда — с обоими флагами. D-Bus
подтвердил два одинаковых `EnvironmentFiles` и два отрицательных
`ConditionPathExists`; `systemd-analyze verify` прошёл в обоих состояниях.
Это проверка реального merge systemd в изолированном контейнере, а не
проверка boot VPS. Unit-тесты контроллера требуют поздний файл и эти
effective свойства, включая случай оставшегося старого `90`.

## Порядок первого выпуска на VPS (только шаг 5.5)

1. Получить отдельно утверждённый точный SHA нового контроллера и версию
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
4. Установить три drop-in из `deploy/` по путям ниже, не заменяя базовые
   unit, `channels.conf` и backup script. Выполнить `systemctl daemon-reload`,
   `systemd-analyze verify obs-chat-bot.service obs-chat-bot-backup.timer
   obs-chat-bot-backup.service`, `systemctl cat` и проверить
   `NeedDaemonReload=no`. Для service последним загруженным drop-in должен быть
   `zz-deploy-pinned.conf`; через `systemctl show` и D-Bus проверить effective
   `ExecStart` с `--no-build --pull never`, только ожидаемый путь `EnvironmentFile`
   к `runtime.env` и отрицательное `ConditionPathExists` для marker. Старый
   `90-deploy-pinned.conf` допустим только при побайтовом совпадении с
   проверенным шаблоном. Любой неизвестный drop-in или другое effective
   свойство блокирует запуск контроллера. В root shell загрузить **только** эти две строки
   `runtime.env`, выполнить `docker compose config --quiet` и через локальный
   парсер сверить конфигурацию без печати полного JSON:
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

### Замена частично установленного snapshot на текущем VPS

Показанный ниже исходный блок установки относится **только к чистой
установке**. На текущем VPS не распаковывать новый архив поверх каталога
контроллера: `SOURCE_ROOT` запускаемого Python-кода указывает на этот каталог,
а `v1.0.0` snapshot уже установлен. После опубликования и проверки
`v1.0.1` нужна отдельная разрешённая операция:

1. Повторить preflight шага 1, проверить отсутствие `deploy-in-progress` и
   активного выпуска, прежние checkout/image ID, работоспособность обоих
   каналов, backup timer и целостность SQLite. Сверить точный SHA `v1.0.1`
   с `release/1.0` и успешным `release-full` run/attempt. Доступный Git
   object получить read-only deploy key без смены checkout.
2. Подготовить root-owned каталог новой версии рядом с действующим snapshot;
   извлечь в него только `scripts/` и `deploy/` точного проверенного commit.
   Побайтово сверить каждый файл архива с `git show`, владельца root и mode
   каталога 0700. Прежний snapshot сохранить для восстановления, не
   перезаписывать его содержимое на месте.
3. Удерживая тот же `/var/lib/compadre-assistant-deploy/deploy.lock`, что
   контроллер, и убедившись в отсутствии запущенного `deploy-prod`, на одном
   filesystem переименовать прежний snapshot в сохранённый каталог, а новый
   — в `/usr/local/lib/compadre-assistant-deploy`. Сверить установленный
   wrapper с новым `deploy/deploy-prod`. При отказе до запуска контроллера
   вернуть прежний каталог и зафиксировать фактическое состояние.
4. Установить новый проверенный `obs-chat-bot-pinned.conf` как
   `zz-deploy-pinned.conf`; существующий `90-deploy-pinned.conf` не изменять и
   сверить его с тем же шаблоном. Сохранить исходные `channels.conf`, base
   unit, backup script и текущий runtime pin. После `daemon-reload` проверить
   `systemd-analyze verify`, `NeedDaemonReload=no`, порядок drop-in и
   effective `ExecStart`, `EnvironmentFiles`, `Conditions` через D-Bus.
   Контроллер нового snapshot должен принять старый image и unit до любого
   stop/start. При отказе не запускать `--verify-pinned`, reboot и deploy;
   проверить marker/state и оба работающих канала.

Команды установки для пунктов 2–4 **на чистом хосте** выполняются в root shell **только после**
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
install -m 0644 /usr/local/lib/compadre-assistant-deploy/deploy/obs-chat-bot-pinned.conf /etc/systemd/system/obs-chat-bot.service.d/zz-deploy-pinned.conf
install -m 0644 /usr/local/lib/compadre-assistant-deploy/deploy/obs-chat-bot-backup-guard.conf /etc/systemd/system/obs-chat-bot-backup.timer.d/90-deploy-guard.conf
install -m 0644 /usr/local/lib/compadre-assistant-deploy/deploy/obs-chat-bot-backup-service-guard.conf /etc/systemd/system/obs-chat-bot-backup.service.d/90-deploy-guard.conf
systemctl daemon-reload
systemd-analyze verify obs-chat-bot.service obs-chat-bot-backup.timer obs-chat-bot-backup.service
systemctl cat obs-chat-bot.service obs-chat-bot-backup.timer obs-chat-bot-backup.service
systemctl show --property=NeedDaemonReload obs-chat-bot.service obs-chat-bot-backup.timer obs-chat-bot-backup.service
systemctl show --property=DropInPaths,ExecStart,EnvironmentFiles obs-chat-bot.service
busctl --json=short get-property org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/obs_2dchat_2dbot_2eservice org.freedesktop.systemd1.Unit Conditions
set -a
. /var/lib/compadre-assistant-deploy/runtime.env
set +a
docker compose config --quiet
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
