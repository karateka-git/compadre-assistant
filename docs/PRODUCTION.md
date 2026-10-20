# Production VPS

## Текущее состояние после выпуска v1.0.4 (проверено 2026-10-20)

После связанных PR [#31](https://github.com/karateka-git/compadre-assistant/pull/31)
в `main` и [#32](https://github.com/karateka-git/compadre-assistant/pull/32)
в `release/1.0` полный
[release-full run 39263707506](https://github.com/karateka-git/compadre-assistant/actions/runs/39263707506)
(attempt 1) проверил SHA `d6b7d9267e7754ee90957d9057e2a4eb41f789c1`.
На нём опубликован защищённый тег и immutable
[Release v1.0.4](https://github.com/karateka-git/compadre-assistant/releases/tag/v1.0.4).
Исправление учитывает реальный ответ GitHub Compare `status=ahead` без
`head_commit`: прежний контроллер отказывал до остановки ботов, когда тег
отставал от вершины релизной ветки.

На VPS из этого Git-объекта подготовлен и побайтово сверен root-owned snapshot
контроллера: 35 файлов `scripts/` и `deploy/`. Прежний snapshot сохранён в
`/usr/local/lib/compadre-assistant-deploy.previous-v102-20261019`.
Под общим `deploy.lock` новый каталог установлен без изменения runtime;
его read-only проверки подтвердили прежний pin, systemd/Compose и допуск
`v1.0.4`. Затем один `sudo deploy-prod v1.0.4` завершился `stage=success`.
Сейчас чистый detached checkout и оба контейнера работают на SHA
`d6b7d9267e7754ee90957d9057e2a4eb41f789c1` и одном image ID
`sha256:434ab2d8860bee4dfddfeb7311973813ca3f41e47ede81d509525e973f2aba3d`.
Оба канала `healthy`, restart count 0, policy `unless-stopped`; polling и
отсутствие другого Docker polling с теми же credentials подтверждены.
Пользователь подтвердил реальные ответы Telegram и VK.

Перед переключением контроллер создал проверенную копию
`/var/lib/compadre-assistant-deploy/backups/v1.0.4-d6b7d9267e7754ee90957d9057e2a4eb41f789c1.db`.
Рабочая SQLite сохранила inode `501886`, `integrity_check=ok` и схему `0001`;
копия также прошла integrity check. После выпуска вручную запущен тот же
`obs-chat-bot-backup.service`, который использует ночной timer: он завершился
успешно, новая `app-20261019T140536Z.db` прошла integrity check, сохранены
семь последних файлов. Timer `active/enabled`, следующий автоматический запуск
назначен на 2026-10-20 03:15 UTC. Результат первого автоматического запуска
подтверждён ниже.

Контролируемый reboot изменил boot ID с
`9e779e1c-c56b-4db6-9c14-2aa66f7ce576` на
`b743ff8e-b584-408d-9656-67ba28293251`. Сразу после восстановления SSH
служба ещё запускалась, затем стала `active`, а оба контейнера — `healthy`
на том же image ID с нулём рестартов. После reboot повторно подтверждены
polling, отсутствие дублей, целостность SQLite, `stage=success`, отсутствие
marker и активный backup timer. Повтор `deploy-prod v1.0.4` вернул
`already_installed` без пересоздания контейнеров.

### Первый автоматический ночной backup после выпуска

20.10.2026 timer автоматически запустил `obs-chat-bot-backup.service` в
03:15:02 UTC. Служба завершилась в 03:15:03 UTC с `Result=success` и
`ExecMainStatus=0`; журнал записал новую копию
`/var/backups/obs-chat-bot/app-20261020T031502Z.db` и `retained=7`.
Файл размером 12 664 832 байта существует, его `PRAGMA integrity_check`
вернул `ok`. В каталоге осталось семь файлов `app-*.db`. Timer остаётся
`active/enabled`, следующий запуск назначен на 21.10.2026 03:15 UTC.

После ночной копии `obs-chat-bot.service` имеет `Result=success` и
`ActiveState=active`. Контейнеры Telegram и VK работают и имеют `healthy`,
нулевой restart count, общий image ID
`sha256:434ab2d8860bee4dfddfeb7311973813ca3f41e47ede81d509525e973f2aba3d`.
Checkout чистый и указывает на SHA выпуска
`d6b7d9267e7754ee90957d9057e2a4eb41f789c1`. В `state.json` записаны
`stage=success` и `last_good.version=v1.0.4`; marker незавершённого deploy
отсутствует. Рабочая SQLite сохранила inode `501886`. Проверка была только
читающей; backup, deploy и перезапуски вручную не запускались. Живая проверка
обоих каналов, reboot и повтор команды были подтверждены ранее. Шаги 5.5 и
7.4.2 приняты 20.10.2026.

### Удаление устаревшего systemd drop-in 20.10.2026

По отдельному разрешению пользователя в окне 06:09:03–06:10:24 UTC под
общим `deploy.lock` удалён только
`/etc/systemd/system/obs-chat-bot.service.d/90-deploy-pinned.conf`, выполнены
`daemon-reload` и один контролируемый restart `obs-chat-bot.service`.
Сценарий завершился `WINDOW_OK` с кодом 0. Новый проверенный maintenance bundle
сохранён в root:root 0700
`/var/lib/compadre-assistant-deploy/maintenance-remove90-7e61b6fz`;
приватные копии pin/state имеют 0600. Срок удаления копий не согласован.

Независимая read-only проверка в 06:10:55 UTC подтвердила ровно
`channels.conf`, `zz-deploy-pinned.conf` в загруженных `DropInPaths`, отсутствие
90 и `NeedDaemonReload=no`. Effective команды, privilege flags,
EnvironmentFiles и marker guard сохранены; повторные одинаковые
EnvironmentFile/Condition из 90 и zz сравнивались по смыслу, а строгий
`Host.installed` проверял допустимость каждой записи. Проверены root-private
pin и Compose overrides, их байты и `state.json` не изменились.

Service `active/enabled`, `Result=success`, ActiveEnterTimestamp —
06:09:23 UTC. `Host.verify_both` подтвердил health, OCI revision и polling
обоих каналов; дублирующих Docker polling нет. Оба контейнера используют
прежний image ID `sha256:434ab2d8860bee4dfddfeb7311973813ca3f41e47ede81d509525e973f2aba3d`,
имеют `healthy`, restart count 0 и `unless-stopped`. Чистый detached checkout
остался на `d6b7d9267e7754ee90957d9057e2a4eb41f789c1`, state — `success`,
last_good — `v1.0.4`, marker отсутствует. Backup timer `active/enabled`,
следующий запуск — 21.10.2026 03:15 UTC; окно не пересекалось с ночной копией.
SQLite сохранила inode 501886, integrity `ok`, схему `0001_initial_schema.sql`.

Предыдущая попытка 06:07:11–06:07:18 UTC безопасно вернула 90 до restart:
сравнение ошибочно считало два одинаковых EnvironmentFiles/Conditions
отличием от одной записи после удаления 90. Автоматический возврат завершился
`ROLLBACK_OK`; независимый read-back в 06:07:45 UTC подтвердил исходную
конфигурацию и прежний ActiveEnterTimestamp. Bundle первой попытки сохранён:
`/var/lib/compadre-assistant-deploy/maintenance-remove90-j9jmxvj4`.
После исправления сравнения, четырёх локальных проверок и повторного preflight
вторая попытка отдельно принята на ревью. Историческая копия
`zz-deploy-pinned.v101-saved-d3fa4efa` не оканчивается на `.conf`, systemd её
не загружает; этот файл не изменялся и не удалялся.

Это обслуживание действующего v1.0.4, новый Release не публиковался.
Эксплуатационная очистка 7.5 выполнена; весь шаг остаётся открытым до
GitHub-приёмки обновлённого fan-out. Подробный порядок возврата — в
[runbook очистки](auto-deploy-main/STEP7_5_SYSTEMD_CLEANUP.md).

## История отказа live выпуска 2026-10-18

На VPS установлен проверенный snapshot контроллера из `v1.0.2`, но сам
`v1.0.2` **ещё не развёрнут**: checkout и оба контейнера работают на прежнем
SHA `ac2ac981ba628a881b483238a4eaf191a644de6a` и image ID
`sha256:1b7521619423d8607ec449d3dcabdac009334d8833d6f71874c670f2d55f0743`.
Один разрешённый запуск `sudo deploy-prod v1.0.2` завершился с кодом 1:
`preparing` → `failed_preflight`, ошибка `DeploySelectionError`. Проверка
происхождения выпуска сравнила SHA тега с тем же SHA в head `release/1.0`;
GitHub API вернул `status=identical` без поля `head_commit`, а selector
обязательно требует это поле. Отказ произошёл до создания marker, остановки
writers, backup и миграции. В `state.json` записаны `stage=failed_preflight`,
`failed_version=v1.0.2` и исторический `retry_after=2026-10-18T15:19:51Z`.
Повтор команды не выполнялся; это время само по себе не разрешает повтор.

Независимая проверка после отказа подтвердила прежний чистый detached SHA и
image ID обоих каналов. Telegram и VK `running/healthy`, polling подтверждён,
restart count 0, policy `unless-stopped`, дубликатов нет. Marker отсутствует,
`obs-chat-bot.service` и backup timer `active/enabled`, effective systemd и
Compose guards действуют. SQLite сохранила inode `501886`, integrity `ok` и
схему `0001_initial_schema.sql`; шесть копий остались на месте, включая
проверенную `app-20261018T072510Z.db`. Следующий backup назначен на
2026-10-19 03:15 UTC. Live выпуск `v1.0.2` не установлен.

Ранее ручной возврат старого pin завершился `state=rolled_back`. Затем
один запуск
`sudo deploy-prod --verify-pinned` завершился с кодом 0 и записал
`stage=pinned_verified`; marker отсутствует. Независимая проверка после команды
подтвердила прежние SHA и image ID у обоих каналов, действующие systemd и
Compose guards и отсутствие второго polling-процесса.
Telegram и VK `running/healthy`, polling подтверждён, restart count 0,
policy `unless-stopped`. `obs-chat-bot.service` и backup timer активны и
включены. SQLite сохранила inode `501886`, прошла integrity check; схема —
`0001_initial_schema.sql`. Новая копия
`app-20261018T072510Z.db` проверена; всего шесть копий. После отдельного
preflight выполнен один `sudo systemctl reboot` (код 0): SSH восстановился,
boot ID сменился с `c35ef418-6f8c-436a-a1fa-28f3b614353a` на
`9e779e1c-c56b-4db6-9c14-2aa66f7ce576`. Сразу после загрузки оба
контейнера временно имели health `starting`, затем стали `healthy` без смены
прежнего image ID. Повторная проверка подтвердила polling без conflict и
traceback, текущий restart count 0 у обоих, прежний pin и
`stage=pinned_verified` без marker, отсутствие дублирующего polling вне двух
ожидаемых Docker cgroup. Service и timer снова `active/enabled`, SQLite
сохранила inode `501886`, integrity `ok` и прежнюю схему; проверенная копия
осталась на месте. Следующий backup назначен на 2026-10-19 03:15 UTC.
Для продолжения исправить selector, провести два связанных PR в `release/1.0`
и `main`, проверить новый точный SHA полным `release-full`, опубликовать новый
PATCH Release и отдельно согласовать работы на VPS. Номер и тег `v1.0.2`
не переиспользовать. Причина прежнего отказа и обязательная проверка доступа
к Compose описаны в
[операционном порядке](OPERATIONS.md).
Разбор схемы выпуска с пользователем по шагу 7.4.1 отложен.

## Публикация v1.0.2 и подготовка восстановления 2026-10-18

Связанные PR #15 в `main` и #14 в `release/1.0` были слиты до тайм-аутов
GitHub API. `release-full` run `39105709125`, attempt 1, успешно проверил
точный релизный SHA `d3fa4efa0b3af4784f8538cae28c8b1ab6e1050a`.
Повторные тайм-ауты происходили при отдельной проверке перед публикацией
тега и GitHub Release; последующий read-back подтвердил, что записи не было.
Позже [неизменяемый Release v1.0.2](https://github.com/karateka-git/compadre-assistant/releases/tag/v1.0.2)
и тег были опубликованы на этот SHA. Слияние PR и публикация Release сами по
себе не запускают обновление VPS: `deploy-prod` выполняется отдельной командой.

При подготовке ручного восстановления первый архив snapshot содержал Windows
CRLF вместо байтов Git-коммита. Проверка `stage-controller-sha` остановила
операцию в отдельном staging-каталоге **до** замены действующего snapshot;
старые контроллер и drop-in остались на месте. Staging удалили после read-back.
Новый архив сформировали из точного SHA с `core.autocrlf=false` и побайтово
сверили все 34 файла с `git show`. Постоянный порядок этой проверки записан в
[OPERATIONS.md](OPERATIONS.md#архив-контроллера-с-windows-точные-байты-git).

## История отказа проверки pin 2026-10-17

`v1.0.1` опубликован из `release/1.0` на точном SHA
`19bba49f81f13f3ebb39730efa09f087758a22e5` после полного
`release-full` run `39045505808`, attempt 1, но на VPS не развёрнут.
Повторная проверка старого pin завершилась `degraded`. Read-only диагностика
установила причину: `obs-chat-bot.service` выполнял Compose как `User=user`,
а внешний каталог pin имеет root:root 0700 и `pinned-compose.yml` — 0600.
Журнал systemd трижды показал `permission denied` для этого файла: при
остановке и двух попытках запуска. Root-контроллер запускал оба старых
контейнера с подтверждёнными healthcheck и polling, но unit не стартовал;
автоматический возврат повторил тот же отказ.

На момент отказа marker `deploy-in-progress` был сохранён, state — `degraded`,
оба старых контейнера остановлены с policy `no`, service — inactive/enabled,
backup timer — inactive/disabled. Чистый detached checkout, runtime tag и
оба image ID указывают на прежний SHA
`ac2ac981ba628a881b483238a4eaf191a644de6a` и image
`sha256:1b7521619423d8607ec449d3dcabdac009334d8833d6f71874c670f2d55f0743`.
SQLite inode `501886`, integrity `ok`, схема `0001_initial_schema.sql`
совпадает с rollback manifest. Это состояние не разрешает простой restart:
нужен ручной recovery по [runbook](auto-deploy-main/STAGE5_STEP4.md#отказ-reboot-и-ручное-восстановление).
Для исправления позднее опубликован PATCH `v1.0.2`; результат ручного
восстановления приведён выше.

## Подготовка первого versioned выпуска 2026-10-17

`main`, `release/1.0` и опубликованный `v1.0.0` указывают на
`33ae77a0e81cacf0a346376c0609681d30e0c317`; `main-full` run
`39042879438`, attempt 1, включая `full-suite`, успешен. Первый live выпуск
**не состоялся**. Во время разрешённой установки из этого SHA проверенный
snapshot `scripts/` и `deploy/` помещён в
`/usr/local/lib/compadre-assistant-deploy`, wrapper — в
`/usr/local/sbin/deploy-prod`. Старый фактический image ID
`sha256:1b7521619423d8607ec449d3dcabdac009334d8833d6f71874c670f2d55f0743`
закреплён тегом по старому SHA; подготовлены внешние Compose override,
`runtime.env` и три systemd drop-in.

Проверка после `daemon-reload` обнаружила, что новый
`90-deploy-pinned.conf` загружается перед существующим `channels.conf`.
Последний снова задаёт `ExecStart` без `--no-build --pull never`, поэтому
`deploy-prod --verify-pinned` не запускался. Reboot и `deploy-prod v1.0.0`
также не выполнялись. Read-back подтвердил прежний чистый detached checkout
`ac2ac981ba628a881b483238a4eaf191a644de6a`, оба старых канала
running/healthy с нулём рестартов и `unless-stopped`, active/enabled service
и backup timer, отсутствие deploy marker и state. Тот частичный setup
не подтверждал безопасный следующий reboot. На тот момент исправление
готовилось как PATCH `v1.0.1`; порядок замены snapshot и приёмки — в
[STAGE5_STEP4.md](auto-deploy-main/STAGE5_STEP4.md). Права SQLite `0644`
и каталога `data` `0755` не менялись в этом окне.

Production переключён на Compadre Assistant 2026-10-13 по отдельному разрешению
пользователя, с остановкой старых каналов и чистой SQLite после проверенного backup.
Развёрнут SHA `ac2ac981ba628a881b483238a4eaf191a644de6a` из
`codex/assistant-rebirth`; checkout находится в detached HEAD и чистый.
Это выполненный переход; повторять очистку БД при обычном обновлении нельзя.
Порядок обычных обновлений — в [OPERATIONS.md](OPERATIONS.md). Историческую
процедуру первого переключения повторять нельзя.
Старые копии SQLite удалены 2026-10-13 по отдельному разрешению пользователя;
откат БД к состоянию до перехода недоступен.

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

Backup после запуска и restart: `app-20261013T074327Z.db` сохранён.
Service и backup timer активны, следующий запуск
на момент проверки — 2026-10-14 03:15 UTC. Rollback bundle:
`/home/user/.compadre-deploy/rollback-r5iZV59g` (каталог 700, файлы 600),
содержит прежний env/override/SHA/origin; `app-before.db` удалён по отдельному
разрешению пользователя. Остальные файлы bundle сохранены.
Прежний image сохранён как `compadre-assistant:rollback`.
Staging `secrets.env` удалён; срок удаления rollback bundle ещё не согласован.

После переключения 2026-10-13 по отдельному разрешению удалены только шесть
старых файлов из `/var/backups/obs-chat-bot`: `app-20261009T031500Z.db`,
`app-20261010T031517Z.db`, `app-20261011T031523Z.db`, `app-20261012T031500Z.db`,
`app-20261013T031505Z.db`, `app-20261013T074041Z.db`, а также
`/home/user/.compadre-deploy/rollback-r5iZV59g/app-before.db`.
Текущая `/opt/obs-chat-bot/data/app.db` и новый backup сохранены; проверены
их прежние inode и неизменность checksum backup. Все семь старых путей
отсутствуют, остальные файлы rollback bundle сохранены. После удаления оба
новых канала healthy, restarts=0, service и timer active. Backup script и
timer не менялись. **DB rollback к прежнему runtime больше недоступен;
исторический rollback-блок runbook нельзя применять как есть.** Любой возврат
требует отдельного плана восстановления данных; сохранённый image и конфигурация
не заменяют удалённые копии БД.

Пользователь принял закрытие этапа 4 и перехода Assistant Rebirth 2026-10-13
после успешного анализа, review и подтверждённой записи в
`karateka-git/my_obs_data` (результат ниже).
Рассылка и удаление старого бота не выполнялись.

### Исправление авторизации анализа Obsidian (2026-10-13)

При пользовательской проверке в 14:10 по Омску статья сохранилась, но анализ
завершился HTTP 401 от LLM (стадия `analysis`, `ArticleAnalysisError`).
У профилей `OPENAI_*` и `ASK_*` совпадали endpoint и модель, но различались
ключи. По разрешению продолжить исправление значение `OPENAI_API_KEY`
заменено на рабочее значение `ASK_API_KEY` из того же production `.env`.
Значения секретов не выводились; исходный файл сохранён с правами `600` в
`/home/user/.local/state/obs-chat-bot-hotfix/20261013T124904Z-obsidian-key/.env`
(каталог `700`). Замена атомарная; побайтовая проверка подтвердила неизменность
остального `.env`, права текущего файла — `600`.

Пересозданы только `tg_assistant`/`vk_assistant` с
`--no-build --pull never --no-deps`. Оба healthy, restarts=0; прежние image ID,
команды, mounts, restart policy и остальные переменные контейнеров сохранены.
Systemd service и backup timer active, PEM checksum/inode/права прежние.
SQLite не пересоздавалась: inode и права прежние, integrity check успешен.
Код VPS остался на `ac2ac981ba628a881b483238a4eaf191a644de6a`, checkout чистый.
Один короткий Chat Completions запрос с `OPENAI_*`, без пользовательского текста,
вернул HTTP 200. Повторная обработка статьи не запускалась.

Затем пользователь повторно отправил `https://habr.com/ru/articles/1092652/`
и подтвердил запись в vault. Read-only проверка VPS подтвердила Telegram
incoming id 2 в 13:08:56 UTC, анализ id 1 в 13:09:15 UTC моделью
`dashscope/qwen3.5-flash`, применение proposal id 1 (`add`) в 13:10:46 UTC
и статус статьи id 1 `reviewed`. Новых ошибок нет; сохранённая HTTP 401
относится к первой попытке. Повторная отправка использует сохранённый очищенный
текст; `/reanalyze <ID>` повторно скачивает страницу.

Пользовательский скриншот позже подтвердил semantic index 641/641. Причина
embedding HTTP 400 при первой синхронизации и отсутствие раннего ответа
`/github_sync` остаются в [бэклоге](BACKLOG.md); исправление LLM-ключа их
устранение не подтверждает.

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
