# Шаг 6.5: автономный controller snapshot и интеграционные ограничения

Запись от 06.11.2026 фиксирует локальную подготовку snapshot и backup для
SQLite v2. Порядок отдельной установки и возврата находится в
[OPERATIONS.md](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot),
общий статус — в [плане](PLAN.md#шаги-этапа-6).
**Шаг 6.5 выполнен частично: production rollout остаётся закрыт.**
Первый локальный проход выполнен без VPS. Затем по отдельному поручению
проведена только безопасная read-only инвентаризация 06.11.2026; факты ниже.
Ни controller, ни units, ни runtime/БД на VPS не менялись. Установленный
snapshot целиком не переаттестован; прежний источник —
[PRODUCTION.md](../PRODUCTION.md). Локально дополнены boot/finalization и
доставка schema admission; Linux-приёмка и порядок окна без ручных writers
ещё не проверены.

## Подготовить автономный архив

`scripts/controller_snapshot.py` собирает новый tar из `git ls-tree`/`cat-file`
точного commit SHA, независимо от CRLF и untracked файлов рабочей папки.
В архив входят Git blobs из `scripts/`, `deploy/`, шесть минимальных файлов
package `compadre_assistant` и сгенерированный `controller-manifest.json`.
Manifest содержит controller SHA и digest/размер/режим каждого файла.
SHA Release приложения не используется как замена controller SHA.

Исправлен пробел прежнего runbook: архив только `scripts/` + `deploy/` больше
не автономен. `predeploy_compatibility` импортирует `runtime_schema`, который
импортирует `migration_runner`. Wrapper запускается из `/` и не должен брать
эти модули из произвольного release checkout. Новая проверка imports использует
отдельный `python -I -B` из staging, без текущей рабочей папки в import path.

Сборщик отвергает symlink, submodule, CR и неполные зависимости. Проверка
архива требует внешний SHA256 из протокола сборки, отвергает повторные имена,
traversal, hardlink/symlink, неожиданные пути/режимы/размеры и изменённые байты.
`unpack` пишет исключительно в новый staging. `verify --directory` проверяет
установленные байты, отсутствие лишних файлов/pycache и links; на Linux
`--require-root` дополнительно требует root:root. Python syntax и изолированные
imports проверяются до переключения. Это не проверка полного CI или systemd.

`deploy-prod` использует `-B`. Перед новым переходом и проверкой pin controller
сверяет приватный `controller.sha` с manifest собственного `SOURCE_ROOT` и его
файлами; сохраняет controller SHA в `state.json` независимо от candidate SHA.
При несовпадении новый переход не доходит до marker/остановки каналов.

## Сохранить существующие контракты

| Контракт | Локальный результат и граница |
| --- | --- |
| Legacy `state.json` | Поля, `last_good`, `retry_after` и неизвестные дополнительные поля не переписываются при чтении. Установка snapshot не мигрирует state в v2. Повтор и legacy recovery покрыты существующими tests. |
| Marker | Имя и смысл `deploy-in-progress` сохранены; backup CLI сам отвергает marker, не полагаясь только на systemd condition. Установка при marker запрещена. |
| Lock | Используется существующий `deploy.lock` без удаления inode. Backup удерживает тот же неблокирующий lock на весь online backup. Установка/возврат snapshot должны удерживать этот inode внешним `flock -n`. Linux межпроцессный flock не проверен на Windows. |
| Compose/systemd | Legacy pinned, critical и marker-guard шаблоны не менялись. Подготовлены selector oneshot drop-in, durable finalization и guarded boot. Они остаются за gate и не установлены на VPS; Linux-приёмка не выполнена. |
| Backup timer | Имя timer и исходные enabled/active сохраняются. Подготовлен отдельный drop-in `obs-chat-bot-backup-selector.conf` для backup service; он не устанавливается автоматически. Реальный effective unit и timer не проверены. |
| Возврат snapshot | До запуска deploy вернуть сохранённые каталог, wrapper, `controller.sha` и проверить digests. После появления v2 state/selector legacy controller не допускается. Нельзя восстанавливать старую БД под видом возврата controller. |

Новые файлы `controller.sha` и manifest надо установить вместе с новым
snapshot под lock. Отсутствующий `controller.sha` не выводится из Release
checkout. Архив локального commit необходимо пересобрать после merge из
точного прошедшего полный CI SHA; до этого он является материалом стенда.

## Проверить backup выбранной SQLite

`scripts/deploy_backup.py` работает под общим deploy lock. Legacy использует
`data/app.db` только при полном отсутствии признаков v2. При v2 выбранный путь
определяется по state/selector и идентичностям поколений; отсутствующий или
повреждённый selector не даёт fallback к `app.db`. Проверяется terminal stage,
отсутствие marker/recovery-blocked, runtime pin и manifest. SQLite API backup
проверяет integrity, журнал и FK, не удаляет старые копии/поколения.

Tests на реальной временной SQLite подтверждают: backup нового поколения
содержит новую строку, которой нет в legacy `app.db`; исходная legacy БД и
state сохраняются. Отказы marker, неизвестной stage, selector, missing selector,
pin и занятого lock не создают backup. Windows-тест подменяет только Linux
lock; реальный SQLite backup, selector и проверки схемы исполняются.

## Исключить известные writers и сохранить gate

Перед окончательным снимком и recovery контроллер дополнительно перечисляет
все Docker containers, включая остановленные. Любой сторонний writable mount
`data`, файла внутри него или родителя (`checkout`, `/opt`, `/`) закрывает
действие; собственные точные ID каналов проверяются прежними проверками.
Read-only и отдельные writable mounts допустимы. Inspect/env и пути не
публикуются. Fake transport tests покрывают работающий/остановленный writer,
ancestor/descendant bind, readonly/separate bind и неизвестный inventory.

Это обнаружение, а не fencing: оно не исключает host Python/SQLite CLI,
параллельный новый `docker run/compose run`, другой daemon/mount alias или
другой хост с credentials. `require_switch_rollout` остаётся безусловным,
без env/CLI bypass. Новый finalizer вызывается после `candidate_started` либо
`recovery_verified`, но сам также требует rollout gate. Marker/timer вручную
не меняются.

## Продолжить до включения v2

1. Провести отдельную проверку локально реализованных finalization/boot и
   readonly admission delivery. Подтвердить lifecycle на Linux с реальными
   units и обоими образами; unit tests не заменяют эту приёмку.
2. Сохранить администратору VPS доступ к Docker и `data`. На время выпуска
   согласовать окно обслуживания: администратор не запускает ручные CLI,
   контейнеры или другие процессы, пишущие в production SQLite. Проверить
   известные writers, разобрать старые stopped containers и описать порядок
   maintenance/reboot. Контроллер не может технически запретить доверенному
   администратору обойти lock; это явная граница эксплуатационной гарантии.
3. Выполнить интеграционную матрицу 6.6 и Linux/Docker/systemd/backup стенд
   этапа 7: успех, возврат до/после admission, restart/reboot, lock/timer,
   запись после переключения, backup выбранной БД и отсутствие двойного polling.
4. Получить полный CI точного merge SHA; собрать и проверить его архив.
   Отдельно согласовать перенос и установку VPS по PRODUCTION/OPERATIONS.
   Зафиксировать фактически установленный controller SHA/digest отдельно
   от Release SHA/image ID, старые snapshot/wrapper/SHA и результат возврата.
5. Снять gate отдельным проверенным изменением только после выполнения этих
   условий. Локальный архив, зелёные unit tests и исторический снимок VPS
   не являются доказательством установленного controller или Linux-приёмки.

### Применить полученный lifecycle-контракт

Базовые units не хранятся в repository. Их безопасные effective свойства
получены отдельным read-only осмотром и позволили продолжить локальную 6.5.
Environment, содержимое `.env`/ключей/SQLite, полные ExecStart/logs/inspect
не выводились. На сервере выполнялись только systemctl show безопасных
свойств и чтение metadata; диагностический Python исполнялся со stdin с `-B`,
без создания скриптов на VPS. Это инвентаризация, не запуск project tests.

Контроллер удерживает deploy lock во время `systemctl start`. Новый helper
решает это без создания второго writer: если lock занят, разрешён **только
read-only adoption уже работающих точных ID** по durable boot intent. Если
хоть один контейнер остановлен, adoption отказывает. При свободном lock helper
может запустить только существующие сохранённые ID, без compose create/up;
проверяет selector, image/OCI, env/mounts, admission и оба health/polling.
Перед start старого образа на новой схеме повторяется проверка evidence.
`restart=no` сохраняется, а будущий drop-in связывает unit с Docker через
`PartOf=docker.service`; реальный restart/reboot этого порядка ещё не испытан.

Finalizer сохраняет `finalization_intent` и точный boot contract до durable
удаления marker, принимает oneshot unit, повторно проверяет пару, сохраняет
`runtime_adopted`, возвращает исходные enabled/active timer, затем пишет
`success`/`rolled_back`. Повтор незавершённой финализации использует тот же
intent. Ошибки возвращаются controller для recovery/закрытого отказа. Новый
v2-путь `verify-pinned` проверяет работающий durable boot без legacy stop/start.

При recovery после admission создаются приватные JSON admission/context с
exact old/candidate SHA/image ID, controller SHA, artifact digest, SQL digest
старого checkout и fingerprint новой схемы. Compose override монтирует только
эти файлы readonly и задаёт два schema env. Проверяются их identity, журнал,
байты/права и фактические mounts. Для совпадающего manifest допуск не нужен.
Он не заменяет перепроверку GitHub evidence. Файлы создаются под остановленными
writers до create/start; прежние поколения и WAL не восстанавливаются.

Это локально реализованный контракт 6.5, а не доказанная Linux-приёмка или
завершение матрицы 6.6. На этой точке работа останавливается по просьбе
пользователя; следующий шаг 6.6 в этом проходе не выполнялся.

## Read-only наблюдения production от 06.11.2026

Осмотр выполнен 06.11.2026 до контрольного времени 13:04 UTC; точные границы
отдельно не фиксировались. Источник — узкие `systemctl show`,
filesystem/xattr metadata, имена процессов/cron-файлов и только Mounts
Docker inspect, обработанные в памяти с выводом агрегатов. Результат является
снимком времени; отсутствие writer в списке не является fencing.

| Объект | Наблюдение |
| --- | --- |
| Основной unit | loaded, active/exited, enabled, NeedDaemonReload=no; oneshot, RemainAfterExit=yes, KillMode=control-group, Restart=no; start timeout 3 min, stop 1 min; User/Group=user, checkout working directory. Requires docker, After docker/network; Before backup service. |
| Backup service | loaded, inactive/dead, static, NeedDaemonReload=no; oneshot, RemainAfterExit=no, KillMode=control-group, Restart=no; start timeout infinity, stop 90 s; User/Group=user. Requires и After основного unit. |
| Backup timer | loaded, active/waiting, enabled, NeedDaemonReload=no. |
| Внешнее состояние | stage=success, protocol_version отсутствует; marker и selector отсутствуют. Каталог root:root 0700, существующий deploy.lock root:root 0600. Lock не захватывался и не менялся при осмотре. |
| Права | checkout/data — user:user 0755; SQLite-файл root:root 0644; credentials-файл user:user 0600. Содержимое файлов не читалось. POSIX ACL на data/credentials/socket отсутствуют по xattr metadata; getfacl недоступен. |
| Docker | socket root:docker 0660; user входит в группы user/docker. Два assistant running и **два прежних catcher exited**, все четыре имеют writable exact bind data. Новый foreign-mount gate отвергнет и два stopped writer; при осмотре они не удалялись/не менялись. |
| Процессы и cron | Наблюдались dockerd/containerd/shims, cron и несколько Python-процессов; аргументы не читались/не выводились. Имена cron-файлов ограничились штатными placeholder/e2scrub; произвольные другие scheduler/операторы этим не исключены. |

По решению пользователя от 06.11.2026 администратор VPS сохраняет доступ к
Docker socket и право записи в `data`. Это доверенный оператор: общий lock не
останавливает его ручной `docker run` или SQLite CLI. Поэтому на время выпуска
нужно объявлять окно обслуживания и не запускать вручную другие writers;
контроллер отдельно проверяет известные контейнеры. Два прежних stopped
containers с writable mount требуют разбора до rollout. Мы не утверждаем, что
проверка контейнеров доказывает отсутствие будущих ручных записей.
Linux-проверка нового unit/backup и эксплуатационный порядок ещё не выполнены;
на VPS права и контейнеры не менялись. Безусловный gate сохраняется.

## Локальные проверки

Используется `C:\Users\compadre\Documents\obsChatBot\.venv\Scripts\python.exe`
(Python 3.12); `.venv` данного worktree отсутствует. Проверены доступность
project dependencies и imports. Итог целевого прогона, точный SHA и digest
подготовленного архива фиксируются после завершения проверки. Первый целевой
прогон: 98 tests, `OK`, 2 Linux-only skips, 61.572 с; проверялись новые
snapshot/integration/backup tests и существующие deploy/switch/recovery,
prepare/final snapshot/predeploy compatibility. Полный набор приложения не
запускался. `git diff --check` не обнаружил whitespace ошибок.

Финальный повтор после commit controller: те же 98 tests, `OK`, 2 Linux-only
skips, 53.665 с. Отдельно `tests.test_migration_runner`,
`tests.test_runtime_schema`, `tests.test_deploy_metrics`: 28 tests, `OK`,
2.882 с. Всего 126 целевых tests и 2 пропуска. Повторить из worktree:

```powershell
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -B -m unittest tests.test_controller_snapshot tests.test_controller_integration tests.test_deploy_backup tests.test_deploy_database_switch tests.test_deploy_database_recovery tests.test_deploy_prod tests.test_deploy_prepare tests.test_deploy_final_snapshot tests.test_predeploy_compatibility
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -B -m unittest tests.test_migration_runner tests.test_runtime_schema tests.test_deploy_metrics
```

## Первый локальный архив — заменяется после продолжения 6.5

Первый архив создан из commit локальной подготовки контроллера
`4c7e6d43d6f1a63846f05f2599d97567cf2ce594`, после его commit. Источник
Git-байтов и manifest относится именно к этому SHA, а не к будущему merge
или Release. Файл находится вне checkout:
`C:\Users\compadre\.codex\worktrees\ci-release-review-plan\controller-artifacts\controller-4c7e6d43d6f1a63846f05f2599d97567cf2ce594.tar`.

SHA256: `9c5ab1a84474f897cc3d1a654837feb48e46e339fae962e90b494208e94e6032`.
`unpack` проверил все 57 файлов, синтаксис и изолированные imports настоящего
snapshot, включая `deploy_prod`, `controller_snapshot`, `deploy_backup`.
Staging: соседний `staging-4c7e6d4`; оба результата локальные, не установлены
на VPS. Документационные commits после этого SHA не меняют байты controller;
после продолжения boot/finalization этот архив больше не является окончательным.
Новый архив и его точный SHA/digest фиксируются после commit нового controller.

## Проверки продолжения 6.5 и точка остановки

После read-only inventory и локальной реализации выполнены 133 целевых tests:
`OK`, 2 Linux-only skips, 67.211 с. К прежним 126 добавлены 7 проверок
`tests.test_deploy_runtime`: adoption без start/create при занятом lock,
отказ для stopped ID при таком lock, start только durable ID и безопасный повтор,
возобновление незавершённой финализации, сохранение новой записи при rolled_back,
отказ при marker/неизвестной stage/чужом ID/повреждённых admission/selector и
readonly schema delivery, принимаемая настоящим прежним runtime validator
на отдельной мигрированной SQLite. Транспорт Docker/systemd подменён.
Основной прогон — предыдущая общая команда с добавленным
`tests.test_deploy_runtime` и тремя schema/metrics модулями.

Linux flock, effective unit/PartOf, реальный Docker start/readonly mount,
reboot/power loss и оба runtime images остаются непроверенными.
Read-only production inventory не является такими испытаниями. Полный CI
и VPS установка не выполнялись. На этой безопасной точке 6.5 заканчивается
текущий проход; 6.6 и этап 7 не начинались. Rollout gate остаётся безусловным.

## Окончательный локальный snapshot после продолжения 6.5

Источник controller: `27c1da95ad12acfa6b37847515dd1cdae0401f2b`.
Архив создан после этого commit из его Git blobs:
`C:\Users\compadre\.codex\worktrees\ci-release-review-plan\controller-artifacts\controller-27c1da95ad12acfa6b37847515dd1cdae0401f2b.tar`.

SHA256: `7d1e87d17a96dac0a88a8ec3683b6ef0c470a1260a80ef308f5a9de9d965164b`.
Проверены все 60 файлов, manifest, синтаксис и изолированные imports
`deploy_prod`, `controller_snapshot`, `deploy_backup`, `deploy_runtime`,
`deploy_schema_admission`. Staging — соседний `staging-27c1da9`.
Этот snapshot заменяет первый архив `4c7e6d4` в роли материала текущей 6.5.
Архив и staging находятся только локально. Для production после merge и полного
CI нужен новый архив точного проверенного SHA и отдельная установка/приёмка.

Tracked рабочая копия после commits чистая; исходные untracked
`recovery-tests.log`, `scripts/check_image_compatibility.py`,
`scripts/compatibility_probe.py` сохранены. Git push не выполнялся.
