# Шаг 6.5: автономный controller snapshot и интеграционные ограничения

Запись от 06.11.2026 фиксирует локальную подготовку snapshot и backup для
SQLite v2. Порядок отдельной установки и возврата находится в
[OPERATIONS.md](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot),
общий статус — в [плане](PLAN.md#шаги-этапа-6).
**Шаг 6.5 выполнен частично: production rollout остаётся закрыт.**
На VPS не выполнялись ни чтение, ни запись; установленный snapshot не проверен
заново. Последний исторический источник — [PRODUCTION.md](../PRODUCTION.md).

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
| Compose/systemd | Legacy pinned, critical и marker-guard шаблоны не менялись. Текущие проверки effective unit/Compose сохраняются. `database-compose.yml` и durable admission v2 остаются за gate; штатная финализация/boot ещё не завершены. |
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
без env/CLI bypass. Сохраняются и отдельные отказы финализации
`candidate_started`/`recovery_verified`; marker/timer вручную не меняются.

## Продолжить до включения v2

1. Дополнить локальную часть 6.5: определить и реализовать штатную финализацию
   candidate/recovery и boot path, который проверяет durable state/selector,
   runtime pin и admission перед polling. Проверить повтор без create/start
   чужого поколения. Не заменять это прямым systemd `compose up` без проверки.
2. Определить реальное ограничение доступа host CLI и ручных Docker writers,
   порядок maintenance/reboot и доверенный круг операторов; доказать, что
   writer не появится после проверки. Простого списка процессов недостаточно.
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

### Получить отсутствующий контракт Linux-стенда

В repository есть только drop-ins, нет базовых `obs-chat-bot.service` и
`obs-chat-bot-backup.service`. Перед продолжением интеграции нужны безопасные
read-only наблюдения effective `Type`, `RemainAfterExit`, `KillMode`, `Restart`,
`TimeoutStartUSec`, `TimeoutStopUSec`, `User`/`Group`, lifecycle команд и
зависимостей обоих units; затем фиксация reviewed boot/shutdown контракта
в repository. Не публиковать Environment/credentials и полные команды, если
они содержат секреты. Дополнительно нужны owner/mode/ACL каталога `data`,
файлов credentials, Docker socket и группы операторов, inventory других
systemd/cron/host CLI и Docker writers. На VPS это отдельное действие по
PRODUCTION; в этом шаге доступа к серверу не было.

Причина зависимости конкретна: controller удерживает эксклюзивный deploy lock
во время `systemctl start`. Новый boot helper с тем же lock не сможет
завершиться, а helper без lock оставляет гонку запуска writer относительно
stop/snapshot. Нужна отдельная durable finalization state machine с
согласованным выходом из lock и повтором после прерывания. Предполагать
семантику отсутствующих base units нельзя. Recovery старого образа на новой
схеме также требует реализации доставки проверенных admission/context
`runtime_schema` в readonly mount, а не только проверки CI evidence.

Это оставшиеся задачи реализации 6.5, а не уже доказанные свойства 6.4 или
сценарии, которые можно закрыть зелёным fake transport test. На автономном
Linux-стенде можно разработать и проверить новый базовый unit/операционный
контракт; его перенос на VPS затем требует отдельной проверки совместимости
с существующей установкой. Наличие только Windows workspace не подтверждает
Linux права, systemd stop/start или исключение ручных writers.

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

## Подготовленный локальный архив

Архив создан из окончательного commit локальной подготовки контроллера
`4c7e6d43d6f1a63846f05f2599d97567cf2ce594`, после его commit. Источник
Git-байтов и manifest относится именно к этому SHA, а не к будущему merge
или Release. Файл находится вне checkout:
`C:\Users\compadre\.codex\worktrees\ci-release-review-plan\controller-artifacts\controller-4c7e6d43d6f1a63846f05f2599d97567cf2ce594.tar`.

SHA256: `9c5ab1a84474f897cc3d1a654837feb48e46e339fae962e90b494208e94e6032`.
`unpack` проверил все 57 файлов, синтаксис и изолированные imports настоящего
snapshot, включая `deploy_prod`, `controller_snapshot`, `deploy_backup`.
Staging: соседний `staging-4c7e6d4`; оба результата локальные, не установлены
на VPS. Документационные commits после этого SHA не меняют байты controller;
если изменится код, архив надо собрать заново из окончательного commit.
