# Этап 5, шаг 2: подготовка образа, состояния и SQLite

Компоненты этого шага являются библиотекой `scripts/deploy_prepare.py`, а не
самостоятельной командой. Исполняемый контроллер шага 3 должен владеть всей
критической секцией от остановки каналов до проверки обоих каналов или
зафиксированного отказа. Запускать отдельный сценарий, который остановит
writers и завершится без возврата, нельзя.

## Проверенный snapshot VPS

Read-only инвентаризация 2026-10-15: `obs-chat-bot.service` вызывает
`docker compose up -d vk_assistant tg_assistant` и останавливает оба сервиса;
`Restart=no`. Оба контейнера имеют фактический image ID
`sha256:1b7521619423d8607ec449d3dcabdac009334d8833d6f71874c670f2d55f0743`
и Docker restart policy `unless-stopped`. Свободно около 7.5 GB, SQLite
содержит миграцию 0001 и проходит `integrity_check`; ночной backup timer
активен. Перед первым выпуском эти факты проверить повторно. Тег Docker
из старого описания не заменяет фактический `.Image` контейнеров.

## Порядок интеграции в контроллер шага 3

1. Взять `deploy_lock` в `/var/lib/compadre-assistant-deploy`, прочитать
   atomic state и фактические контейнеры. Если предыдущая стадия не закрыта,
   сначала выполнить recovery; повтор того же неудачного SHA учитывать через
   cooldown. Убедиться, что оба контейнера имеют один старый image ID.
2. Проверить чистый production checkout. Запустить selector из доверенного
   кода контроллера и получить версию, SHA, branch, Release ID и CI run/attempt.
   Получить ровно commit и тег read-only deploy key, проверить локальный
   прямой тег и detached HEAD. Повторить selector после fetch, сравнить весь
   манифест и проверить checkout. До остановки writers повторить selector ещё
   раз и сравнить результат с сохранённым.
3. `build_pinned_image` создаёт локальный тег `compadre-assistant:release-<SHA>`,
   задаёт OCI `org.opencontainers.image.revision` и сверяет фактический image ID.
   `check_candidate_image` принимает именно этот image ID, повторно сверяет
   OCI revision и выполняет три synthetic smoke без сети, secrets и
   production SQLite. Проверить свободное место для сборки и backup. До
   остановки сохранить старые SHA/image ID, candidate SHA/image ID, CI run и
   стадию в atomic state. Старый image ID не удалять.
4. До первого контролируемого выпуска установить версионируемый drop-in
   `deploy/obs-chat-bot-pinned.conf` и скопировать `deploy/pinned-compose.yml`
   во внешний `/var/lib/compadre-assistant-deploy/pinned-compose.yml`. Создать
   внешний `runtime.env` с двумя строками:
   `ASSISTANT_IMAGE=compadre-assistant:release-<старый SHA>` и
   `COMPOSE_FILE=/opt/obs-chat-bot/docker-compose.yml:/var/lib/compadre-assistant-deploy/pinned-compose.yml`.
   Сначала привязать старый тег к **фактическому старому image ID**.
   Проверить `systemd-analyze verify`, `docker compose config` и обычный
   reboot/start на старом pin. Внешний Compose override необходим для первого
   выпуска: старый checkout ещё содержит `image: compadre-assistant:dev`.
   Drop-in меняет `ExecStart` на `--no-build --pull never`; новый
   `docker-compose.yml` также использует `ASSISTANT_IMAGE` и
   `pull_policy: never`. `runtime.env` менять только атомарной заменой,
   права 0600. Контроллер при ручном Compose вызове должен передавать те же
   `ASSISTANT_IMAGE` и `COMPOSE_FILE`, что получает systemd.
5. Перед критической секцией создать постоянный marker
   `/var/lib/compadre-assistant-deploy/deploy-in-progress`, чтобы systemd не
   поднял старые каналы после reboot. Остановить и постоянно замаскировать
   ночной backup timer на время критической секции, затем дождаться уже
   запущенной backup service. Маска переживает reboot; снять её и вновь
   включить timer при завершении или recovery. Остановить **оба** writers через
   systemd/Compose, проверить отсутствие работающих Telegram/VK контейнеров
   и второго polling с теми же credentials. Не останавливать один канал
   отдельно. Состояние и journal обновлять на каждой стадии.
6. `backup_sqlite` создаёт отдельную копию с SQLite backup API и проверяет
   `integrity_check` до миграции. Каталог копий выпуска отделён от ночной
   ротации; файл с правами 0600, без публикации содержимого. На ошибке backup
   миграцию не запускать. При отсутствии миграций backup всё равно нужен для
   зафиксированного контрольного снимка выпуска.
7. Только после backup выполнить `docker run --rm --network none --read-only`
   из candidate image с `python -m compadre_assistant --migrate
   --database-path /app/data/app.db`, примонтировав production `data`
   и **без** env-file. В этом каталоге есть PEM, поэтому доступ к образу
   допускается только после проверки точного SHA/CI; migration CLI сам PEM
   не читает и не выводит. Это существующий отдельный CLI режим; healthcheck
   миграции не запускает. Перед вызовом доказать совместимость каждого нового
   SQL с предыдущим runtime или остановить автоматический выпуск для ручного
   плана. После вызова сверить список `schema_migrations` с файлами образа и
   `integrity_check` через `validate_schema`. Ошибка между SQL-файлами
   оставляет БД на фактической стадии; автоматический restore запрещён.
8. Лишь после успешной миграции атомарно сменить `runtime.env` на candidate,
   убрать marker и запустить оба канала. Весь этот переход и rollback относятся
   к шагу 3. При любом исходе включить ночной timer и сверить его состояние.

Серверный журнал и atomic state — единственные уведомления на этом этапе по
выбору пользователя. Внешний канал отложен в `docs/BACKLOG.md`; ошибка
доставки будущего уведомления не должна останавливать recovery.

## Открытые ограничения

- Этот шаг не устанавливает drop-in, не меняет VPS и не предоставляет
  исполняемого `deploy-prod`: переход и возврат требуют контроллер шага 3.
- `docker-compose.yml` имеет fallback на `:dev` для локальной разработки.
  Production systemd должен получить `ASSISTANT_IMAGE` и `COMPOSE_FILE` через
  `runtime.env`; до этого установку контроллера считать незавершённой.
- Dockerfile закрепляет base image digest, но `pip install` скачивает
  зависимости при сборке. Фактический image ID и OCI revision сохраняются;
  одинаковый исходный SHA не означает побайтово одинаковый CI image.
- Backup на том же VPS не защищает от потери VM. Ни backup, ни старый образ
  не дают автоматического права на возврат после несовместимой миграции.
