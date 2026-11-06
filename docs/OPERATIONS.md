# Эксплуатация Compadre Assistant

Здесь описаны установка опубликованного выпуска на VPS, диагностика и
восстановление. Локальную рабочую копию подготовьте по [SETUP.md](SETUP.md)
и запускайте по [RUN.md](RUN.md). Условия допуска релиза описаны в
[RELEASES.md](RELEASES.md), а последний зафиксированный снимок сервера — в
[PRODUCTION.md](PRODUCTION.md).

| Задача | С чего начать |
| --- | --- |
| Запустить и проверить локальную копию | [RUN.md](RUN.md) |
| Подключить GitHub vault | [GITHUB_APP.md](GITHUB_APP.md) |
| Найти причину сбоя | [Диагностика embeddings](#диагностика-embeddings), [Obsidian write-back](#диагностика-obsidian-write-back), [Частые проблемы](#частые-проблемы) |
| Установить версию на VPS | [Production VPS](#production-vps) → [Подготовка и запуск версии](#подготовка-и-запуск-версии) → [Отказ выпуска и возврат](#отказ-выпуска-и-возврат) |

Локальная разработка и production используют разные правила для SQLite.
Разрешение пересоздать development-БД **не относится к VPS**.

## Production VPS

Перед работой прочитайте [контекст и правила production](PRODUCTION.md).
Описанное ниже состояние сервера требует проверки при подключении.

```mermaid
flowchart LR
    A[Проверить снимок VPS<br/>и эксплуатационное окно] --> B[Проверить точный SHA<br/>и опубликовать Release]
    B --> C[Отдельно запустить<br/>deploy-prod vX.Y.Z]
    C --> D{Результат контроллера}
    D -->|success| E[Сверить pin, SQLite,<br/>оба канала и backup]
    D -->|отказ| F[Сохранить state и marker,<br/>разобрать stage]
    F --> G[Проверить возврат<br/>или составить план recovery]
```

Публикация Release не является командой установки. При отказе дальнейшее
действие определяется сохранённым `stage`; состояние `degraded` запрещает
новую выкладку до ручного восстановления. Контроллер не заменяет живую
пользовательскую проверку ответов обоих каналов.

По снимку от 20.10.2026 production развёрнут в `/opt/obs-chat-bot` на H3llo VPS. Конфигурация
`.env`, PEM GitHub App и SQLite не хранятся в Git. VK работает напрямую,
Telegram Bot API доступен через выделенный VPN и прокси. Пользовательский
ответ на `/status` был подтверждён после включения.

Для Telegram подготовлен отдельный WireGuard-интерфейс `wgobs` между H3LLO
(`10.77.77.2`) и Timeweb (`10.77.77.1`). На Timeweb SOCKS5-прокси Dante
принимает подключения только по адресу туннеля `10.77.77.1:1080` от H3LLO.
В серверный `.env` уже добавлено
`TELEGRAM_PROXY_URL=socks5://10.77.77.1:1080`; получение данных бота и polling
проверены. Прокси применяется только к Bot API, прочие
интеграции используют обычный маршрут.

Проверка сетевого пути без токена и без изменений в БД:

```bash
sudo systemctl is-active wg-quick@wgobs
curl --socks5-hostname 10.77.77.1:1080 -I --max-time 10 https://api.telegram.org/
```

На Timeweb проверить `systemctl is-active wg-quick@wgobs danted`. Если туннель
или прокси недоступны, проверить `wg show wgobs`, состояние двух systemd-сервисов
и доступность UDP `51831` между VPS. Закрытые ключи и содержимое `.env` в
журналы и отчёты не копировать.

Проверить службу и контейнеры:

```bash
sudo systemctl is-active obs-chat-bot.service obs-chat-bot-backup.timer
sudo docker ps --filter label=com.docker.compose.project=obs-chat-bot \
  --format '{{.Names}} {{.Image}} {{.Status}}'
```

Обычный выпуск выполняется только для опубликованного GitHub Release с
успешным полным CI точного коммита: `main-full` для новой линии из проверенного
`main` либо `release-full` для нового SHA существующей релизной линии. После проверки текущего
состояния и согласования эксплуатационного окна команда
`sudo deploy-prod vX.Y.Z` сама сверяет Release, закрепляет образ, создаёт backup, останавливает
оба канала, при необходимости применяет миграции и проверяет запуск обоих.
Связь движущейся релизной ветки, точного тега и фактической версии VPS описана
в [RELEASES.md](RELEASES.md).
Повтор того же выпуска должен завершиться `already_installed` без пересоздания.
При проверке тега, который отстаёт от вершины релизной ветки, GitHub Compare
может вернуть `status=ahead` без поля `head_commit`. Точный head берётся из
отдельно прочитанного ref и передаётся в compare-запросе; допуск подтверждают
`status=ahead`, `base_commit` и `merge_base_commit` с SHA тега. Требование
`head_commit` блокировало первый выпуск до исправления в `v1.0.4`.
При `degraded` новый запуск блокируется до ручной проверки и восстановления
по [порядку ниже](#отказ-выпуска-и-возврат).
Не удалять production-БД по development-инструкции: изменения её схемы требуют
миграции с сохранением данных.

### Доступ systemd к закреплённому Compose

Внешний каталог `/var/lib/compadre-assistant-deploy` принадлежит root и имеет
права `0700`; `runtime.env` и оба Compose override внутри него — root:root
`0600`. Основной `obs-chat-bot.service` задаёт `User=user` и `Group=user`.
После удаления устаревшего `90-deploy-pinned.conf` действующий порядок
drop-in: `channels.conf`, затем `zz-deploy-pinned.conf`. Поздний файл
должен задавать **обе** итоговые команды:
`ExecStart=!/usr/bin/docker compose up -d --no-build --pull never
vk_assistant tg_assistant` и `ExecStop=!/usr/bin/docker compose stop
vk_assistant tg_assistant`. По [документации systemd](https://www.freedesktop.org/software/systemd/man/257/systemd.service.html)
префикс `!` выполняет только эти команды без ограничений `User=`/`Group=`;
остальные настройки службы сохраняются. Права приватного каталога и файлов
не ослаблять ради запуска Compose.

Перед первым start/stop после изменения unit, drop-in или прав проверить
`systemd-analyze verify`, `NeedDaemonReload=no`, порядок `DropInPaths` и
итоговые D-Bus `ExecStartEx` **и** `ExecStopEx`: точные аргументы и единственный
флаг `no-setuid`. Также сверить `User=user`, `Group=user`, рабочий каталог,
путь `EnvironmentFiles` к `runtime.env` и условие, блокирующее службу при
`deploy-in-progress`. Контроллер `deploy-prod` выполняет эти проверки перед
обычным выпуском. Если хоть одна не проходит, остановиться до перезапуска;
подмена прав `0700`/`0600` или одного только `ExecStart` не исправляет путь
остановки службы.

Если Compose запускается как `user`, root-private pin недоступен:
контейнеры могут запуститься вручную от root, но systemd не подтвердит
службу, а возврат попадёт в `degraded`. Поэтому до restart проверяют
обе итоговые команды и флаг `no-setuid`, а не только имя позднего файла.
Последний проверенный снимок этой конфигурации — в
[PRODUCTION.md](PRODUCTION.md).

### Архив контроллера с Windows: точные байты Git

При подготовке snapshot для VPS не упаковывать файлы из рабочей копии Windows:
на ней переводы строк могут стать CRLF, хотя опубликованный Git-коммит хранит
LF. 18 октября 2026 года такой архив остановился на проверке SHA256
`scripts/deploy_prod.py` **до** замены действующего snapshot. Контрольную сумму
в установочном скрипте под изменённые байты не подгонять.

Автономный архив создавать из точного проверенного commit SHA:
для обычной подготовки это может быть SHA Release, для отдельного исправления
контроллера — SHA проверенного merge. Записать источник snapshot отдельно от
SHA устанавливаемого приложения.

```powershell
$snapshotSha = "PASTE_VERIFIED_40_CHAR_SHA"
$archivePath = "C:\Temp\release-snapshot.tar"
$python = "C:\Users\compadre\Documents\obsChatBot\.venv\Scripts\python.exe"
& $python -B -m scripts.controller_snapshot build --sha $snapshotSha --archive $archivePath
# Сохранить выданные controller_sha и archive_sha256 во внешний протокол.
& $python -B -m scripts.controller_snapshot unpack --sha $snapshotSha --archive $archivePath --digest PASTE_SHA256 --directory C:\Temp\controller-staging-UNIQUE
```

Архив только `scripts/` + `deploy/` недостаточен для этапов 5–6: imports
используют `runtime_schema.py` и `migration_runner.py` приложения. Сборщик
добавляет минимальные Git-зависимости и manifest, отвергает CR и links;
`unpack` проверяет digest, SHA, все файлы, синтаксис и imports через отдельный
`python -I -B` вне release checkout. Это локальная проверка, а не допуск CI.

Перед передачей сверить SHA256 всего архива и **каждого обычного файла** в нём
с `git show <SHA-релиза>:<путь-файла>`; отклонить неожиданные symlink/hardlink.
Для передаваемых shell/Python-скриптов проверить отсутствие `CR` и синтаксис.
На VPS повторно сверить SHA256 архива и распакованных файлов в отдельном
root-owned staging-каталоге. Если хоть один файл не совпал, остановиться до
атомарной замены snapshot и проверить источник архива. Незавершённый staging
удалять только после проверки его точного пути и неизменности действующих
snapshot, drop-in, marker и состояния ботов.

### Обновить отдельно установленный controller snapshot

`/usr/local/sbin/deploy-prod` запускает Python из
`/usr/local/lib/compadre-assistant-deploy`, а не из checkout выпуска.
Поэтому исправление контроллера для новой миграции надо установить в этот
отдельный snapshot **до** повтора `deploy-prod`. SHA snapshot и SHA
устанавливаемого Release могут различаться; оба записывают отдельно.

1. После PR и проверки точного merge SHA в полном CI выбрать проверенный
   commit контроллера. Из него создать автономный архив сборщиком
   [выше](#архив-контроллера-с-windows-точные-байты-git). Сверить каждый
   файл с `git show <SHA>:<путь>`, отсутствие лишних типов записей и SHA256
   архива; записать SHA commit, digest и точный CI run/attempt. После merge
   пересобрать архив для точного прошедшего полный CI merge SHA.
2. Получить отдельное согласование на перенос архива и изменение VPS. Перед
   записью заново проверить [production](PRODUCTION.md): `git status`,
   `state.json`/marker, работу двух каналов и backup timer. Не запускать
   установку snapshot при активном deploy или `degraded`; сначала разбирать
   такое состояние. После отказа `failed_preflight` старые каналы должны
   работать, marker должен отсутствовать. На всё время установки удерживать
   существующий inode `/var/lib/compadre-assistant-deploy/deploy.lock` через
   `flock -n`; файл не удалять и не переименовывать. Под lock повторно сверить
   stage/marker, cooldown и отсутствие работающего backup service.
3. На VPS проверить digest перенесённого архива, безопасно распаковать его в
   новый root-owned staging вне checkout и сверить хеш каждого файла с
   manifest проверенного commit. Проверить Python syntax/imports, права,
   wrapper `deploy-prod` и отсутствие symlink/hardlink, путей вне staging и
   CR в скриптах. Подготовить staging через `controller_snapshot unpack`
   с внешними `--sha`/`--digest` и `--require-root`; проверить root:root и отсутствие чужой записи.
   Не выводить `.env`, SQLite, ключи и полные логи.
4. Сохранить действующий snapshot как отдельную неизменяемую копию и
   переключить каталог `/usr/local/lib/compadre-assistant-deploy` на
   проверенный staging через переименование в том же filesystem. Сохранить
   также прежние wrapper, `controller.sha`, digests и точные пути копий.
   Два rename не являются одной атомарной транзакцией: имя установки временно
   отсутствует; в это окно никто не запускает controller. Если
   переключение не завершилось, вернуть старый каталог до любого deploy.
   Сверить установленный каталог через `controller_snapshot verify` с архивом,
   внешними SHA/digest, `--directory` и `--require-root`; проверить изолированный import и
   установить проверенный wrapper как root:root 0755. Его `-B` запрещает
   создание pycache внутри snapshot. Атомарно записать SHA в `controller.sha`
   (root:root 0600, fsync файла и каталога); новый controller сверяет его с
   manifest и собственными байтами. До конца проверки lock не отпускать.
   **Не менять** release checkout, image pin, БД, marker или state ради
   установки контроллера.
5. После свежего read-only осмотра, отдельного допуска к установке и окна
   backup проверить точный Release/SHA/CI выбранной версии, текущий `retry_after` и
   отсутствие `degraded`. Только затем рассматривать повтор
   `sudo deploy-prod vX.Y.Z`; не стирать cooldown/state для ускорения.
   Записать оба SHA (controller и Release) и результат новой команды.

Этот порядок готовит установку, но не разрешает её автоматически. После
успешной миграции или отказа проверить фактические stage, schema, backup,
image ID и оба канала по [порядку возврата](#отказ-выпуска-и-возврат).

При отказе установки под тем же lock вернуть прежние каталог, wrapper и
`controller.sha`, сверить сохранённые digests/imports, затем pin, state,
marker и timer с исходным осмотром. Если новый controller уже записал v2
state/selector, возврат к legacy controller запрещён: он не понимает границу
приёма. Нужен отдельный совместимый recovery; старая БД не является откатом
snapshot. Незавершённый staging сохранять до разбора.

### Подготовить backup и штатный запуск для SQLite v2

Локальная подготовка шага 6.5 описана в
[протоколе snapshot](ci-release-review/STAGE6_CONTROLLER_SNAPSHOT.md).
`scripts.deploy_backup` выбирает terminal-поколение через selector, сверяет
runtime pin, копирует SQLite API и проверяет integrity/FK. Он удерживает
тот же `deploy.lock` до конца backup и отвергает marker даже при прямом CLI.
Для будущей установки подготовлен `deploy/obs-chat-bot-backup-selector.conf`:
он заменяет только `ExecStart` backup service; timer и marker guards остаются.
В проверяемом gate 7.3 целевое имя этого шаблона —
`/etc/systemd/system/obs-chat-bot-backup.service.d/zz-deploy-selector.conf`,
после `90-deploy-guard.conf`. До первого production v2 deploy в 7.7 отдельно
установить оба selector drop-in (основной — `zz-deploy-pinned.conf`), guards
и snapshot, затем проверить effective units. Gate не устанавливает их сам.
Локальная подготовка и оставшиеся блокеры записаны в
[шаге 7.3](ci-release-review/STAGE7_3_PREFLIGHT.md).
После отдельного допуска нужны `daemon-reload`, `systemd-analyze verify`,
проверка effective ExecStart, timer и копии выбранной БД с новой записью.

**Production rollout v2 остаётся закрыт.** Новые drop-ins ещё не установлены.
В кандидате безусловный отказ заменён проверкой snapshot, точных файлов и
effective units/guards, отсутствия известных сторонних writers; перед marker
повторяется exact-pair CI/image admission. Ручного CLI/env обхода нет. Локально
реализованы `finalization_intent` → `runtime_adopted` → `success`/`rolled_back`,
guarded boot и readonly schema admission/context. Будущий
`deploy/obs-chat-bot-selector.conf` заменяет прямой `compose up` helper-командой:
при занятом lock она только принимает уже работающую точную пару; при свободном
запускает существующие durable ID без create/up. Docker restart остаётся `no`.
Нужны Linux-проверка oneshot/PartOf/reboot, прав и backup. Администратор VPS
сохраняет доступ к Docker и `data`; во время согласованного окна выпуска он не
запускает ручной SQLite CLI, `docker run/compose run` и другой writer или polling
с теми же credentials. Это эксплуатационное правило для доверенного оператора,
а не техническая защита от него. Проверка writable Docker mounts перед
снимком/recovery обнаруживает известные контейнеры, но не блокирует создание
writer после осмотра. Два прежних stopped контейнера с writable mount
удалены в [шаге 6.6](ci-release-review/STAGE6_INTEGRATION.md#зафиксировать-разрешённую-очистку-vps);
перед rollout заново проверить полный inventory. Установка
snapshot сама по себе gate не снимает; следующие проверки и read-only факты
VPS — в рабочем протоколе. Marker/timer вручную не менять. `verify-pinned`
для v2 только проверяет durable boot и работающую пару; legacy stop/start
на selector-поколении не применяется.

Проверить ежедневные SQLite-копии и их журнал:

```bash
sudo systemctl list-timers obs-chat-bot-backup.timer
sudo journalctl -u obs-chat-bot-backup.service --since today
sudo ls -l /var/backups/obs-chat-bot
```

Timer запускает copy в 03:15 UTC, хранит семь последних файлов и выполняет
backup через SQLite API. Копии лежат на том же диске VPS; защита от потери
всей VM требует отдельного backup/snapshot у провайдера или внешнего хранилища.

### Подготовка и запуск версии

На локальной машине из чистого checkout точного кандидата выполнить
read-only проверку, просмотреть будущие release notes и только затем
опубликовать. Новый `release/X.Y` предварительно создаётся из проверенного
`main` через `scripts.create_release_branch`: `--check-only`, затем
локальное создание без флага и, после разрешения записи, `--publish-remote`.
Каждый вызов принимает имя ветки и полный SHA. Для PATCH использовать
действующую линию.

| Этап | Условие перехода |
| --- | --- |
| Подготовить кандидата | Чистый checkout точного SHA; полный CI и ветка соответствуют [правилам допуска](RELEASES.md#2-получить-полный-ci-для-точного-коммита). |
| Проверить публикацию | `--check` успешен, вывод `--dry-run` и release notes просмотрены. |
| Опубликовать | После `--publish` тег, SHA и GitHub Release совпадают. VPS ещё не менялся. |
| Установить | Перед командой заново проверены VPS и окно backup; после команды проверены `stage`, pin, SQLite, оба канала и timer. |

```powershell
$env:GITHUB_TOKEN = gh auth token
python -m scripts.publish_release v1.0.5 <полный-SHA> --change patch --notes-file <путь-к-notes> --check
python -m scripts.publish_release v1.0.5 <полный-SHA> --change patch --notes-file <путь-к-notes> --dry-run
python -m scripts.publish_release v1.0.5 <полный-SHA> --change patch --notes-file <путь-к-notes> --publish
```

Пример номера не предписывает следующий фактический выпуск. Перед записью
сверить изменения линии, указанный SHA и результаты полного CI; публикация
не запускает VPS. На сервере после свежего read-only осмотра
[PRODUCTION.md](PRODUCTION.md), отдельного эксплуатационного допуска и
проверки окна backup вызвать `sudo deploy-prod vX.Y.Z`. Команда синхронна:
по окончании проверить stage/state, отсутствие marker, точный SHA/image ID
обоих каналов, health и polling, службу, backup timer и SQLite integrity.
Для реальных ответов Telegram/VK требуется пользовательская проверка.
Результат доступен в серверном журнале; внешних уведомлений сейчас нет.

### Закрепить базу полного CI после установки

Только после успешного `deploy-prod` и независимой проверки `last_good`,
рабочей БД и обоих каналов обновить базу следующего полного CI. Публикация
Release без установки на VPS не меняет базу: например, при опубликованной
v3.2.0 и установленной v3.1.0 внешний CI pin всё ещё должен указывать на
reviewed policy с v3.1.0. `main-full` и `release-full` читают именно её
`previous_sha`, `release_tag` и `release_id`, проверяют immutable Release и
прямой tag. `full-suite` повторно проверяет ту же policy и точную пару.

После установки нового выпуска сверить на VPS установленный SHA и Release ID
без вывода данных БД. В отдельном PR обновить только baseline поля
`scripts/repository_compatibility_policy.json` до этого SHA/tag/ID; проверить
матрицу старого repository API, `pr-fast` и merge commit. Затем установить
repository variable на **полный SHA reviewed merge commit**, проверить её
значение и запустить ручной полный `main-full` для текущего SHA `main`.
Для существующей release-линии проверить `release-full` точного нового SHA.
Новый CI pin не заменяет отдельный production `compatibility-policy.sha`:
тот остаётся закреплён за evidence пары, с которой текущий выпуск был
установлен. До подтверждённого нового CI нельзя считать зелёный run другой
пары допуском; при несовпадении pin и `last_good` predeploy закрывается.

```powershell
gh variable set COMPATIBILITY_POLICY_SHA --repo karateka-git/compadre-assistant --body <полный-SHA-reviewed-merge>
gh variable list --repo karateka-git/compadre-assistant --json name,value
gh workflow run main-full.yml --repo karateka-git/compadre-assistant --ref main
```

Перед каждым Git-действием проверить чистоту worktree; для push получить
согласие пользователя и после push перечитать `AGENTS.md`. При неуспешной
проверке policy/Release или любого обязательного job оставить выпуск закрытым
и разбирать конкретный run/attempt, не подменяя baseline последним
опубликованным Release.

### Проверить живое переключение двух каналов

После технически успешной установки проверить живые ответы отдельно: статус
контроллера и `healthy` контейнеров не доказывает работу команд. Использовать
согласованные тестовые Telegram/VK identities одного `AppUser A` и отдельный
`AppUser B`; не публиковать внешние ID, тексты вопросов и ответов. Если
каналы A ещё не связаны, выполнить [привязку](RUN.md#13-посмотреть-доступные-команды)
и проверить профиль назначения до переключений.

1. До личных команд снять для обоих сервисов container ID, image ID, PID и
   `StartedAt` read-only командой ниже. Указать версию, SHA и время снимка.
2. У A сравнить `/capabilities` в Telegram и VK, у B записать исходный выбор.
   Включить Obsidian у A в Telegram, проверить выбор в VK и вход `/obsidian`.
   Выключить Obsidian у A в VK: следующий текст исходного Telegram-чата
   должен получить подсказку, а `/status` — сохранить прежний режим.
   Выбор B должен остаться прежним.
3. Повторно включить Obsidian у A через VK; проверить прежний режим и заранее
   согласованные сохранённые данные без записи в vault. Для Ask принять
   длительный вопрос у A, затем до результата отключить Ask во втором канале:
   начатый ответ приходит в исходный чат, следующий вход получает отказ.
   Если ответ пришёл до disable, порядок не доказан и сценарий повторяют
   в согласованное окно.
4. Снять те же Docker поля после переключений и сравнить с первым снимком.
   Изменение личного выбора не должно перезапустить ни один канал. Записать
   отдельно наблюдаемые технические признаки и пользовательское подтверждение
   сценариев; без подтверждения человека живую приёмку не объявлять.

```bash
cd /opt/obs-chat-bot
for service in tg_assistant vk_assistant; do
  cid=$(docker compose ps -q "$service")
  docker inspect --format '{{.Name}} {{.Id}} {{.Image}} {{.State.Pid}} {{.State.StartedAt}}' "$cid"
done
```

Не отключать действующий vault ради живой проверки Device Flow. Если Ask не
настроен или нет безопасного тестового профиля, отметить непроверенную часть,
не подменяя её локальным fake-тестом.

### Отказ выпуска и возврат

Состояния `state.json` в `/var/lib/compadre-assistant-deploy` описывают
ход одной команды; marker `deploy-in-progress` удерживает systemd от
самостоятельного запуска во время критической секции. Не удалять marker,
lock, state или backup для «повтора». Сначала сверить состояние, оба
контейнера, pin, checkout, службу, timer и SQLite integrity/schema без
вывода секретов и содержимого БД.

| Stage | Действие |
| --- | --- |
| `failed_preflight` | Проверка остановилась до переключения. Убедиться, что старые каналы работают и marker отсутствует; устранить причину. `failed_version`/`retry_after` временно закрывают повтор той же версии. |
| `preparing`, `ready` без marker | После прерывания один повтор той же команды восстанавливает прежний checkout и **завершается отказом**. Проверить прежние SHA, pin и оба канала перед новой попыткой. |
| `ready`, `critical`, `writers_stopped`, `backup_verified`, `migration_started`, `schema_verified`, `candidate_pinned`, `candidate_started`, `rollback_started` с marker | До попытки новой миграции автоматический возврат допустим только при прежней схеме и целой SQLite. После попытки новой миграции контроллер переводит выпуск в `degraded` даже без новой строки `schema_migrations`: частичное изменение нельзя исключить. Повтор команды служит разбору recovery, а не новой установке. |
| `rolled_back` | Старый runtime подтверждён контроллером; проверить его независимо. Новая выкладка — отдельное решение. |
| `pinned_verified` | Проверка закреплённого прежнего образа завершена; это не установка новой версии. |
| `success` | Сверить `last_good` с фактическим checkout и двумя контейнерами. Повтор уже установленной версии даёт `already_installed` без пересоздания. |
| `degraded` либо несовместимая/неизвестная схема | Оставить marker, остановить оба writer и timer; новая команда заблокирована. Нужен отдельный план ручного восстановления. |

Возврат **не** восстанавливает SQLite из копии автоматически. Если миграции
изменили схему, старый код, отвергающий неизвестную миграцию, запускать нельзя;
решение о восстановлении backup должно учитывать записи после копии и
возможные изменения GitHub vault. При `degraded` под отдельным разрешением
сохранить lock/marker и проверить прежний SHA/image и схему. Возврат на старый
runtime допустим только после доказательства **точного прежнего manifest**,
целостности БД и структуры относительно проверенной backup-копии; при новой
или неопределённой схеме нужен отдельный план
продолжения на совместимом коде либо восстановления проверенной backup-копии
с оценкой записей после неё. При доказанном прежнем manifest установить
проверенный systemd/Compose snapshot, поднять оба старых канала с прежним
pin, сверить health/polling и отсутствие дублей. Лишь после этого вернуть
restart policy, снять marker, запустить unit, проверить оба канала снова,
вернуть timer к сохранённому состоянию и атомарно записать `rolled_back`.
Если любой признак не доказан, оставить `degraded` и оба канала остановленными.
Не считать один лишь `active` unit доказательством успешного возврата.

Узкий forward recovery доступен только после отказа финализации уже принятого
candidate с выбранной новой SQLite, сохранённым `boot.outcome=success` и
`migration_attempted=false`. После отдельной установки проверенного controller
snapshot и selector units с безопасным Python import администратор повторно
сверяет state/marker, точные ID/image/OCI, integrity/FK/fingerprint выбранной
БД, Release/CI evidence и отсутствие чужих writers. Затем в согласованном
окне выполняет root-only helper установленного snapshot:

```sh
sudo env -i HOME=/root PATH=/usr/bin:/bin PYTHONPATH=/usr/local/lib/compadre-assistant-deploy \
  /usr/bin/python3 -P -B -m scripts.deploy_forward_recovery
```

Helper удерживает существующий deploy lock, сохраняет durable forward intent
до запуска и использует только те же два container ID на выбранной новой БД.
После health/polling он проводит штатную финализацию unit, marker и timer;
process-crash повтор допускается только для того же intent. При отказе
он восстанавливает `degraded` и marker, останавливает известные writers и
отдельно сообщает, если остановка либо guards не подтверждены. Команда не выполняет
новую миграцию, не создаёт контейнеры и не подменяет обычный выпуск. Её
выполнение требует отдельного решения по фактическому состоянию VPS.

### Аварийная версия

Аварийный путь требует решения владельца, причины, подтверждённого PR merge
и внешней записи утверждённого текста Release. Из чистого checkout
выполнить команду ниже сначала с `--check`, затем с `--dry-run`, затем
с отдельно разрешённым `--publish`:

```powershell
python -m scripts.publish_emergency_release vX.Y.Z <полный-SHA> --change patch --reason "причина" --notes-file <путь-к-notes> --pr N --check
```

Для PATCH указывать
PR выпускаемой линии и связанный main PR, если он создавался; PR других
линий не включать. Проверить tag/Release после записи. На VPS после нового
осмотра запустить `sudo deploy-prod --emergency vX.Y.Z --reason "причина"`.
Обычный режим аварийный Release отвергает. Аварийная команда пропускает
только допуск по успешному полному CI; backup, migration, pin, health,
marker и rollback действуют как обычно. Настоящий аварийный выпуск на VPS
пока не испытан.

В подготовленном [controller шага 5.5](ci-release-review/STAGE5_PREDEPLOY.md)
emergency не обходит `repository-compatibility`: нужны успешные steps этого
job и доверенное evidence точного run/attempt, совпадающие с установленной
базой. Ошибка API, pin/evidence или предварительной SQLite-копии даёт отказ
до marker/остановки. Перед будущей установкой snapshot надо отдельно
подготовить внешние `compatibility-policy.sha` и `github-read-token` по
указанному контракту; на VPS эти изменения ещё не применялись.

### Разобрать отказ predeploy по SAFE-коду

Подготовленный controller записывает статический `SAFE_*` в stderr и поле
`error` состояния `failed_preflight`; приватный `metrics-*.json` сохраняет
его в `predeploy_reason`. Поле `reason` метрик по-прежнему обозначает тип
исключения. Код не включает token, HTTP body, SQL или строки БД. При ошибке
возврата checkout сохранённый `predeploy_reason` объясняет первичный отказ,
а сообщение контроллера требует отдельно разобрать recovery. На VPS этот
новый контракт ещё не установлен.

| Код | Проверка и дальнейшее действие |
| --- | --- |
| `SAFE_SOURCE_SCHEMA`, `SAFE_CANDIDATE_MANIFEST`, `SAFE_SCHEMA_PREFIX`, `SAFE_CANDIDATE_IMAGE` | Проверить исходный журнал/integrity, manifest SQL кандидата, точный префикс применённых миграций и revision собранного образа до evidence. |
| `SAFE_CONFIG_READ`, `SAFE_CONFIG_PERMISSIONS`, `SAFE_CONFIG_EMPTY` | Проверить наличие и root:root/0600 внешнего pin/token, непустое значение; не выводить token. |
| `SAFE_POLICY_PIN`, `SAFE_POLICY_CONTENT` | Проверить полный SHA внешнего reviewed pin и полноту policy. |
| `SAFE_POLICY_BLOB_MISSING`, `SAFE_POLICY_BLOB_DIGEST` | Получить заново точные Git blobs; проверить доступ API и закреплённый commit. |
| `SAFE_CI_PROVENANCE`, `SAFE_CI_ATTEMPT`, `SAFE_CI_JOB_MISSING`, `SAFE_CI_JOB_FAILED` | Проверить точный run/attempt, repo/ref/SHA и все обязательные steps; исправить CI и rerun. |
| `SAFE_INSTALLED_PREVIOUS_SHA`, `SAFE_BASELINE_RELEASE` | Сопоставить installed SHA с reviewed baseline и immutable Release/tag; новый baseline требует review policy. |
| `SAFE_INSTALLED_LAST_GOOD`, `SAFE_INSTALLED_RUNTIME`, `SAFE_INSTALLED_IMAGE`, `SAFE_INSTALLED_REVISION` | Проверить last_good, systemd/pin, оба контейнера и OCI revision; не продолжать установку при drift. |
| `SAFE_WORKFLOW_BYTES` | Сверить три workflow с внешним policy commit; изменение требует нового reviewed pin. |
| `SAFE_ARTIFACT_MISSING`, `SAFE_ARTIFACT_METADATA`, `SAFE_ARTIFACT_DIGEST` | Проверить уникальность artifact, run/head SHA, срок хранения и SHA256 ZIP из GitHub metadata; при истечении нужен новый CI run/attempt. |
| `SAFE_ARTIFACT_REDIRECT`, `SAFE_ARTIFACT_SIZE`, `SAFE_ARTIFACT_EXPANDED_SIZE`, `SAFE_ARTIFACT_MEMBER`, `SAFE_ARTIFACT_FORMAT` | Проверить HTTPS download, лимит 64 MiB, структуру ZIP и JSON; повреждённый artifact не принимать. |
| `SAFE_EVIDENCE_IDENTITY`, `SAFE_CANDIDATE_SHA` | Сверить точную пару SHA, policy, run/attempt, результаты и внутренние хеши. |
| `SAFE_TEST_MANIFEST`, `SAFE_REPORT_CONTRACT` | В artifact проверить полноту 173 IDs, результаты, sources/imports и fingerprints; агрегат `passed=true` недостаточен. |
| `SAFE_EVIDENCE_READ`, `SAFE_EVIDENCE_RECHECK`, `SAFE_EVIDENCE_CHANGED` | Проверить доступ GitHub и доступность metadata; при повторной проверке evidence/pin должны остаться теми же. |
| `SAFE_COPY_BACKUP`, `SAFE_COPY_SOURCE_SCHEMA` | Проверить online backup и исходный журнал/integrity на приватной копии. |
| `SAFE_COPY_MIGRATION`, `SAFE_COPY_CANDIDATE_SCHEMA` | Проверить candidate CLI, timeout и точный журнал/integrity мигрированной копии. |
| `SAFE_COPY_FOREIGN_KEYS`, `SAFE_COPY_FINGERPRINT` | Разобрать FK либо несовпадение схемы копии с CI, не изменяя live-БД. |
| `SAFE_COPY_TABLE_LOSS`, `SAFE_COPY_DATA_LOSS` | Разобрать потерю таблицы/общих колонок либо изменение мультимножества исходных строк. |
| `SAFE_PREDEPLOY_IO` | Прочая ошибка внешней операции predeploy (например, временный каталог); проверить приватное хранилище и окружение. |

Если tests действительно запускались, в CI artifact
`repository-compatibility-<run>-<attempt>` файлы `behavior.json`/`candidate.json`
содержат failed/error test IDs, а соответствующие `.log` — traceback unittest.
Upload выполняется с `always()`, когда файлы уже созданы. При отказе pin,
checkout, API, установки dependencies или импорта до tests имя упавшего test
может отсутствовать; при отказе до создания output может отсутствовать и
artifact. Predeploy не исполняет эти tests повторно и не публикует traceback
candidate migration на production-копии: он сообщает только безопасный код.
После исправления причины повторно проверить допуск точной пары; не создавать
marker и не останавливать каналы вручную для обхода отказа.

## Локальный запуск и проверка

Порядок подготовки среды, пересоздания только development-БД, запуска Docker
и автономных проверок находится в [RUN.md](RUN.md). Перед живой проверкой
остановите второй polling-экземпляр с теми же credentials. Подключение
GitHub App и vault описано в [GITHUB_APP.md](GITHUB_APP.md).

## Диагностика

Начните с типа ошибки и сохранённого `stage`, затем читайте только безопасные
поля журналов и БД. Полные секреты и пользовательские тексты не выводите.

### Диагностика SQLite

При одновременном старте `tg_assistant` и `vk_assistant` недостающие миграции
применяются одним процессом под `BEGIN IMMEDIATE`; второй процесс ждёт SQLite
lock, повторно проверяет `schema_migrations` и не выполняет тот же SQL второй
раз. Сообщения вида
`table ... already exists` на чистой базе означают регрессию этой блокировки.

Core-миграция `0002_user_capability_preferences.sql` добавляет
`user_capability_preferences` после уже применённой `0001_initial_schema.sql`.
При установке новой версии контроллер сначала проверяет, что применённые
миграции образуют точный префикс candidate, останавливает оба writer, создаёт
и проверяет backup с прежним manifest, затем применяет недостающие миграции по
[порядку выпуска](#подготовка-и-запуск-версии).
`0001` не редактируется. Пустая новая таблица означает личные defaults и для
ранее зарегистрированных пользователей: Ask выбран, Obsidian выключен; статьи,
vault, identities и `active_chat_modes` сохраняются. Личное или серверное
отключение не удаляет эти данные. При подозрении на сбой проверьте записи
`schema_migrations` и целостность SQLite через контроллер выпуска; не удаляйте
рабочую БД и не применяйте development-порядок пересоздания.

### Debug-режим

Для расширенных логов в консоли сервера добавить в `.env`:

```dotenv
APP_DEBUG=true
```

В debug-режиме приложение пишет безопасные события обработки: тип входящего
сообщения, результат flow, `app_user_id`, `incoming_message_id`, `article_id`,
`analysis_id`, данные внешней identity при регистрации/привязке. Секреты,
полный текст статей, LLM prompt и API keys не логируются.

После проверки вернуть:

```dotenv
APP_DEBUG=false
```

### Диагностика embeddings

Embedding-события используют тот же стандартный Python `logging`, что и весь
проект, и попадают в stdout/stderr контейнеров. Docker собирает этот поток;
отдельной telemetry-БД или специального embedding logger нет.

Посмотреть события обоих каналов:

```powershell
docker compose logs tg_assistant vk_assistant | Select-String "event=embedding_"
```

Только окончательные сбои и переходы на FTS fallback:

```powershell
docker compose logs tg_assistant vk_assistant |
  Select-String "event=embedding_operation_failed|event=embedding_fallback"
```

`operation_id` связывает начало, SDK batches, итог операции и последующий
fallback. Поля `app_user_id`, `vault_id` и `article_id` позволяют найти
конкретный workflow. Для массовой индексации итоговое событие содержит число
batches/texts, usage, latency, errors и оценочную стоимость.

OpenAI SDK сохраняет штатный `max_retries=2`: один логический SDK-вызов может
сделать до трёх HTTP-попыток. Adapter видит и логирует их общую длительность и
окончательный результат, но не выдумывает недоступные ему детали каждой
внутренней попытки. `response.usage` и provider request ID выводятся только при
наличии. API keys, тексты chunks, compact query и vector values не логируются.

Каждый успешный embedding batch сохраняется в SQLite до следующего запроса.
После сбоя или перезапуска `/vault_sync` продолжает только недостающую работу;
частичные vectors не доступны semantic search до публикации полного marker.
Истечение или смена владельца sync lease останавливает запросы и запись,
сохраняя прежние checkpoints для следующей синхронизации.

В `event=embedding_request_failed` различаются `status=invalid_response`
(например, malformed `200 OK`), `status=http_error`, `status=transport_error`
и `status=request_error` для прочих ошибок вызова SDK. Для malformed-ответов
сохраняются `http_status`, `request_id`, `batch_index`, `batch_size`,
`expected_vectors`, `actual_vectors`, `response_type`, `data_present`,
`data_type`, `item_types`, `index_present_count`, `embedding_present_count`,
`index_types`, `embedding_types`, `value_types`. При отсутствии массива `data`
фактическое число vectors равно `unknown`, а не вымышленному нулю.
В логах есть только количества и типы полей, без значений vectors и полного JSON.

Если бот отвечает ошибкой, посмотреть последние диагностические записи:

```powershell
@'
import sqlite3

con = sqlite3.connect("data/app.db")
con.row_factory = sqlite3.Row
for row in con.execute("""
    SELECT id, article_id, incoming_message_id, stage, error_type, error_message, created_at
    FROM processing_errors
    ORDER BY id DESC
    LIMIT 10
"""):
    print(dict(row))
con.close()
'@ | python -
```

Основные таблицы для диагностики (не полный перечень схемы):

- `articles` — сохранённые статьи и их статус;
- `incoming_messages` — сообщения внешних каналов со ссылками;
- `analysis_results` — сохранённые LLM-сводки;
- `processing_errors` — диагностика ошибок загрузки, извлечения и анализа.
- `obsidian_notes` — полные Markdown-заметки активных vault;
- `obsidian_note_chunks` — структурные chunks текущей локальной копии;
- `obsidian_note_chunks_fts` — автоматически обновляемая FTS5-проекция chunks;
- `obsidian_chunk_index_states` — marker согласованного поколения parser/policy.
- `obsidian_note_chunk_index_states` — source blob SHA и signature chunks каждой
  заметки; по ним stale global marker восстанавливается без полного rebuild.
- `obsidian_proposals` — pending и завершённая история решений `add`/`update`/
  `skip`, исходные SHA и commit успешного write-back.

### Диагностика Obsidian write-back

При ответе `да` в логах искать `Obsidian proposal applied` или
`Obsidian proposal commit failed`. Логи содержат proposal/user/article IDs,
действие, target path и SHA, но не installation token и не полный Markdown.
Ошибки write-back также сохраняются в `processing_errors` со stage
`obsidian_write`.

После временного сбоя предложение остаётся `pending`, статья —
`needs_obsidian_review`, а `cleaned_text` и локальные vault SHA не меняются.
Повторить `да`: сервис заново читает remote state. Если commit был создан, но
его HTTP-ответ потерялся, совпадение remote Markdown завершает локальную
транзакцию без второго commit. Если HEAD/blob изменены другим автором, proposal
получает статус `conflict`; нужно повторно прислать исходную ссылку.

Sync и write-back используют одну таблицу lease. Ответ о занятом vault означает,
что операция идёт в другом связанном Telegram/VK-канале; pending proposal не
теряется, `да` можно повторить после завершения операции.

### Повторная отправка ответов каналов

При временном сбое VK Long Poll повторяет получение сервера и updates с
задержкой до 30 секунд. Запись `VK long poll transient failure` содержит
этап, номер попытки и безопасный тип сетевой причины. Постоянная ошибка
доступа завершает процесс; при циклическом restart проверить настройки VK.
Telegram polling тоже повторяет временные сетевые ошибки с задержкой до
30 секунд. Неверный token или конфликт второго polling-экземпляра требуют
исправить конфигурацию или остановить дублирующий процесс.

Telegram и VK выполняют до трёх попыток отправки только при временных ошибках.
Предупреждение `message send retry` содержит канал, номер неудачной попытки,
задержку и тип ошибки. Запись `message send failed` означает, что ошибка постоянная
либо все разрешённые попытки исчерпаны. Текст ответа и credentials в retry-лог не
попадают.

VK использует неизменный `random_id` одной retry-серии. Telegram соблюдает
`TelegramRetryAfter`; из-за отсутствия idempotency key у Telegram редкий дубль
возможен, если API принял сообщение, но HTTP-ответ потерялся.

## Работа с GitHub vault

Подключение, синхронизация и отключение vault описаны ниже. Требования к App,
PEM и `.knowledge-catcher.yml` приведены в [GITHUB_APP.md](GITHUB_APP.md).

### Подключение vault

Регистрация пользователя, Device Flow, выбор repository, права App и
подготовка файлов правил описаны в [GITHUB_APP.md](GITHUB_APP.md).
Здесь приведены действия при эксплуатации уже подключённого vault.

### Синхронизация GitHub vault

Первая синхронизация выполняется сразу после подключения или подтверждённой
замены vault. Ручная проверка запускается в любом связанном канале:

```text
/vault_sync
```

Текущее подключение, количество локальных Markdown-заметок, instruction-файлов
и timestamps:

```text
/vault_status
```

Одновременно один vault синхронизирует только один процесс; второй Telegram/VK
запрос получает сообщение о выполняющейся синхронизации. При ошибке GitHub
сохранённый source SHA не продвигается, lease освобождается, а последняя локальная
копия остаётся доступной. Installation token, содержимое Markdown и HTTP-ответы
GitHub не выводятся в логи.

Первая синхронизация и обновление с 50 или более изменёнными файлами используют
один ZIP snapshot конкретного commit SHA. Небольшое обновление скачивает только
изменённые blobs. В operational log поле `mode=archive` или `mode=blobs`
показывает выбранный путь вместе с количеством файлов и длительностью. GET имеет
read timeout 30 секунд и до трёх попыток; временный HTTP-ответ с `Retry-After`
задаёт задержку следующей попытки. ZIP ограничен 100 MiB и не распаковывается на
диск.

В корне vault обязателен `.knowledge-catcher.yml` версии `1` с непустым списком
`instructions`. Все пути относятся к корню vault и должны вести к существующим
UTF-8 файлам. Конфигурация и каждый instruction-файл проверяются до применения
нового snapshot. При ошибке source SHA не продвигается, последний полный набор
правил сохраняется, а ручной ответ содержит безопасную причину и готовый пример.
Такая ошибка не маскируется fallback и блокирует article pipeline до
исправления repository. Instruction-файлы хранятся отдельно и не считаются
Obsidian-заметками.

Перед каждой статьёй общий incoming flow проверяет возраст `last_checked_at`.
Внутри шестичасового окна GitHub-запросов нет. Если прошло шесть часов или
больше, первая следующая статья выполняет инкрементальную проверку под тем же
SQLite lease. Ошибка автоматической проверки не удаляет локальные заметки и не
останавливает article pipeline: статья обрабатывается с последней локальной
копией, а пользователь получает предупреждение. Занятый другим связанным каналом
lease обрабатывается так же. Ошибка GitHub остаётся в operational log только как
тип исключения без token, URL HTTP-ответа и содержимого Markdown.

Отключение активного vault:

```text
/vault_disconnect
```

Команда не удаляет данные сразу: в SQLite создаётся подтверждение с TTL 10
минут. `да` удаляет строку vault, а SQLite каскадно очищает локальные заметки,
tags, wikilinks и lease. `нет` удаляет только подтверждение. GitHub account,
installation IDs, пользователь, identities, статьи и анализы сохраняются.
Сам GitHub repository команда не изменяет.

### Частые проблемы

- `TelegramConflictError`: уже запущен другой экземпляр бота с тем же token.
  Закрыть старое окно и запустить заново.
- `401 Unauthorized access` на stage `analysis`: неверный AI-agent API key или
  ключ вставлен в `.env` вместе с `Bearer`.
- Ошибка загрузки страницы: сайт недоступен, вернул неуспешный HTTP-статус или
  блокирует загрузку.
- Ошибка извлечения текста: HTML загрузился, но extractor не нашёл содержательный
  текст статьи.
