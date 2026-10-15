# Этап 5, шаг 3: локальный контроллер переключения

Реализованы `scripts/deploy_prod.py`, `deploy/deploy-prod`, pinned/critical Compose
overrides и systemd guards. На VPS ничего не установлено и каналы не
переключались. Команда `sudo deploy-prod vX.Y.Z` появится только после
отдельной установки и проверки на старом образе в шагах 5.4–5.5.

## Граница автоматического выпуска

Контроллер сейчас допускает только выпуск без новых SQL-файлов относительно
production `schema_migrations`. Он всё равно создаёт SQLite API backup,
запускает отдельный `--migrate` из candidate image и проверяет точный список
миграций и `integrity_check`. Для `0002+` отсутствует машинно проверяемое
доказательство совместимости новой схемы с прежним runtime. Такой выпуск
отклоняется **до остановки** обоих каналов. Автоматический выпуск совместимых
`0002+` остаётся незакрытым требованием этапа 5; до его реализации нужен
отдельный ручной план, включая проверку возврата старого кода на новой схеме.

## Установка перед первым использованием

Установка требует отдельного эксплуатационного разрешения и повторной
read-only инвентаризации `docs/PRODUCTION.md`. Не выполнять следующие
действия сейчас. Сохранить копию проверенного controller snapshot вне Git
checkout в `/usr/local/lib/compadre-assistant-deploy`: каталог `scripts/`
целиком (selector и его зависимости) и `deploy/` целиком. Установить
`deploy/deploy-prod` в `/usr/local/sbin/deploy-prod` с владельцем root и правами
0755. Wrapper очищает окружение и запускает только эту внешнюю копию, чтобы
`git checkout --detach` не подменял исполняемый контроллер посреди выпуска.
Проверить, что root запускает Git в `/opt/obs-chat-bot` без ошибки ownership;
если понадобится `safe.directory`, добавить **только** этот путь, не `*`.

Сначала подтвердить текущие SHA, фактический `.Image` обоих работающих
контейнеров, `healthy`, `RestartPolicy.Name=unless-stopped`, чистый detached
checkout, действующие unit/override и `backup.timer`. Сохранить old SHA и
image ID без публикации переменных окружения контейнеров. Пометить **сам
фактический старый ID** локальным тегом
`compadre-assistant:release-<старый полный SHA>`; проверить `docker image
inspect` по тегу и ID. Скопировать `deploy/pinned-compose.yml` и
`deploy/critical-compose.yml` в `/var/lib/compadre-assistant-deploy` (каталог
0700), затем атомарно создать `runtime.env` с правами 0600 и ровно двумя
строками:

```text
ASSISTANT_IMAGE=compadre-assistant:release-<старый полный SHA>
COMPOSE_FILE=/opt/obs-chat-bot/docker-compose.yml:/var/lib/compadre-assistant-deploy/pinned-compose.yml
```

Установить `deploy/obs-chat-bot-pinned.conf` как
`/etc/systemd/system/obs-chat-bot.service.d/90-deploy-pinned.conf`,
`deploy/obs-chat-bot-backup-guard.conf` как
`/etc/systemd/system/obs-chat-bot-backup.timer.d/90-deploy-guard.conf`,
`deploy/obs-chat-bot-backup-service-guard.conf` как
`/etc/systemd/system/obs-chat-bot-backup.service.d/90-deploy-guard.conf`.
Существующие unit, `channels.conf` и backup script не заменять. После
`systemctl daemon-reload` проверить `systemd-analyze verify`,
`NeedDaemonReload=no` для всех трёх unit и effective Compose config:
обоим сервисам нужен один старый release tag, `pull_policy: never` и
`restart: unless-stopped`; с добавленным critical override — `restart: no`.
Проверить `systemctl cat` для всех трёх unit. Установка файлов сама по себе
не останавливает каналы.

Перед первым реальным выпуском выполнить отдельный **контролируемый** тест
остановки и возврата обоих каналов на старом pin, включая `systemctl start`,
проверку image ID, health, polling, restart count, timer и reboot. Этот тест
должен иметь тот же recovery путь, что и контроллер; не выполнять отдельную
последовательность `systemctl stop`/`docker compose up` без него. Шаг 5.4
должен реализовать и проверить такой сценарий до разрешения первого deploy.
Ключ `/home/user/.ssh/obs-chat-bot_deploy` используется только для fetch;
root Git получает `UserKnownHostsFile=/home/user/.ssh/known_hosts`,
`StrictHostKeyChecking=yes`, `BatchMode=yes`. Fingerprint GitHub host key
проверить доверенным путём перед установкой. Команда не передаёт `.env`, PEM
или SQLite в GitHub.

## Переход и возврат

Один процесс держит lock и state вне checkout. Selector сравнивается до
fetch, после fetch и непосредственно перед критическим участком. Fetch
получает конкретный SHA и прямой tag существующим read-only deploy key;
checkout остаётся чистым detached. Сборка закрепляет OCI revision и
фактический image ID. Synthetic smoke запускаются без сети, secrets и
production SQLite. Монотонный SemVer обязателен; повтор уже установленной
версии проверяет текущее состояние без пересоздания.

До остановки сохраняются old/candidate SHA, image ID, tag, CI run/attempt,
список миграций и исходный enabled/active статус backup timer. Marker
блокирует systemd service, backup timer и прямой запуск backup service.
Timer отключается persistent `disable --now`; при выходе восстанавливается
его **исходное** состояние. `mask` не применяется: действующий unit лежит в
`/etc/systemd/system`, и маскирование там могло бы затронуть сам unit.
Консервативно меняется Docker restart policy старых контейнеров на `no` до
остановки; critical Compose override создаёт candidate и rollback контейнеры
с `restart: no`. Ручной Docker stop при `unless-stopped` обычно переживает
daemon restart, но временная policy закрывает промежутки переключения и
случай неполного старта. После проверки обоих каналов policy возвращается,
marker снимается, запускается штатный systemd unit, и оба канала проверяются
повторно. Только затем восстанавливается timer и пишется `success`.

Backup лежит отдельно от ночной ротации. После backup выполняется отдельный
`docker run --network none --read-only ... --migrate --database-path
/app/data/app.db`, без env-file. После SQL или пользовательских записей backup
не восстанавливается автоматически. Пока схема не изменилась, при отказе
candidate контроллер останавливает оба канала, закрепляет старый ID и
проверяет оба старых канала. Если схема отличается от сохранённого списка,
результат — `degraded`, marker остаётся, оба канала останавливаются по
возможности и требуется ручной план. Recovery после reboot делает только
один возврат, не начинает автоматически следующий выпуск.

Текущее отсутствие другого polling проверяется по credentials в других
запущенных Docker-контейнерах без вывода токенов. Внешние host-процессы и
удалённые экземпляры таким способом нельзя доказательно исключить; признаки
конфликта проверяются в кратких журналах обоих каналов и эксплуатационном
осмотре. В state и серверном журнале остаются стадии, CI, backup ID и тип
ошибки; внешние уведомления отложены по плану.

## Проверено локально

`python -m unittest tests.test_deploy_prod tests.test_deploy_prepare` проверяет
успех двух каналов, повтор без пересоздания, возврат после отказа одного
канала, no-op миграцию отдельно от образа, запрет `0002+` до stop, частичный
reboot, degraded при изменённой схеме, исходное состояние таймера и поиск
дублирующего Docker polling на fake host/SQLite. Реальный systemd/Docker boot
и живые каналы остаются проверкой шагов 5.4–5.5.
