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

## Локальные проверки

Используется `C:\Users\compadre\Documents\obsChatBot\.venv\Scripts\python.exe`
(Python 3.12); `.venv` данного worktree отсутствует. Проверены доступность
project dependencies и imports. Итог целевого прогона, точный SHA и digest
подготовленного архива фиксируются после завершения проверки. Первый целевой
прогон: 98 tests, `OK`, 2 Linux-only skips, 61.572 с; проверялись новые
snapshot/integration/backup tests и существующие deploy/switch/recovery,
prepare/final snapshot/predeploy compatibility. Полный набор приложения не
запускался. `git diff --check` не обнаружил whitespace ошибок.
