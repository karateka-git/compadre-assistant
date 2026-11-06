# Шаг 7.3: подготовить код и допуск до остановки

Запись от 06.11.2026 описывает только первый подэтап 7.3: локальную подготовку
проверяемого gate и migration transport. Порядок продолжения — в
[плане](PLAN.md#шаги-этапа-7), исходные копии — в [7.1](STAGE7_1_PREPARATION.md),
предыдущая проверка snapshot — в [7.2](STAGE7_2_SNAPSHOT.md).
**Шаг 7.3 не завершён; остановка ботов пока заблокирована.** VPS, Docker daemon,
production DB/state/controller/units/marker в этом подэтапе не использовались
и не изменялись. Commit и push исполнитель подэтапа не выполнял.

## Проверить инфраструктурный gate

`Host.require_switch_rollout` теперь вызывает `deploy_rollout.require_infrastructure`.
Успех требует верификации всего установленного controller snapshot через
`controller_sha`, root-owned обычных файлов без hardlinks/чужой записи и
побайтного совпадения selector/backup drop-ins и обоих marker guards.
Проверяются `systemd-analyze verify`, отсутствие NeedDaemonReload, effective
boot contract, backup ExecStartEx с env -i и точными argv/root flags,
порядок backup drop-ins и структурированные D-Bus Conditions обоих backup
units. Полный Docker inventory отвергает известные сторонние writable mounts.
Неизвестное или недоступное состояние закрывает gate без fallback.

Source `deploy/obs-chat-bot-backup-selector.conf` устанавливается в
`/etc/systemd/system/obs-chat-bot-backup.service.d/zz-deploy-selector.conf`;
это определённое здесь целевое имя, после `90-deploy-guard.conf`. Основной
selector template заменяет `zz-deploy-pinned.conf`. На production эти файлы
пока не устанавливались: отдельная установка и effective unit приёмка
остаются в 7.7 до первого реального v2 deploy. Сам gate ничего не устанавливает.

В 7.3–7.5 controller работает с явной моделью внешних операций в отдельном
private harness, а не production Host. Наследование/подмена Python методов
служит тестовой моделью; обычные CLI конструируют Host и не имеют флага обхода.
Защита от произвольного root Python/native-кода не заявляется. Согласованное
окно без ручных writers сохраняется: inventory не блокирует создание нового
writer доверенным администратором после проверки.

## Повторить точный допуск перед остановкой

Для selector deploy после `ready`, но до marker, timer suspend, restart-policy
и stop вызывается `require_switch_candidate`. Повторно проверяются точный
SelectedRelease (включая repository/ref/SHA/release ID/workflow/run/attempt),
installed previous SHA/image/OCI/pin, candidate OCI/image/tag, доверенный
compatibility artifact и внешний policy pin, controller SHA. Artifact должен
совпасть с результатом predeploy, где настоящая online-копия уже мигрирована
и сверена по schema fingerprint, integrity/FK и сохранности общих данных.
Сохранённое evidence остаётся в state до `writers_stopped` и далее; gate не
превращает один infrastructure success в разрешение мигрировать произвольную пару.
Отказ до marker возвращает checkout и пишет failed_preflight без остановки.

| Предмет | Зафиксировано / требуется |
| --- | --- |
| Previous runtime | `dd71c2ba42a0744b5b654cedcf73009ce8879058`, v3.1.0; image `sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8`. Это снимок 7.1/7.2, нужен свежий read-only inventory. |
| Previous CI | Исторический полный run `41436078724/1` для previous; не допуск будущего candidate. |
| База текущей правки | HEAD `30b40fc` в `codex/ci-release-review-plan`; правки этого подэтапа ещё не закреплены commit. |
| Итоговый candidate | До остановки закрепить полный новый SHA после review/commit, точный immutable image ID и OCI revision. Сейчас не предъявлены. |
| Полный CI | Для того же итогового candidate нужны успешные обязательные jobs/steps, repository/ref/workflow/run/attempt; skipped/pending/API failure закрывают допуск. Для этих незакоммиченных байтов не предъявлен. |
| Compatibility evidence | Для пары exact previous выше / итоговый candidate нужны reviewed external policy SHA, artifact ID/digest и schema fingerprint из проверенного полного CI. Сейчас не предъявлены; historical CI не подставлять. |
| Snapshot | Архив 7.2 (`512cb57...`) больше не доказывает текущий код. После commit пересобрать из Git blobs, закрепить tar digest и повторить private проверку manifest/прав/imports точного нового SHA до остановки. |

## Подготовить отдельный migration broker

`stage7_transport.migration_command` по-прежнему только строит закрытые argv:
immutable image, pull never, network none, readonly rootfs, cap-drop ALL,
no-new-privileges, ipc none, pids limit, log-driver none, tmpfs и единственный
writable bind отдельного scenarios-каталога с app.db/sidecars. Fixtures,
production bind, credentials и произвольные команды не принимаются.

Новый `scripts.stage7_migration.migrate` подготовлен для отдельного доверенного
Linux-процесса вне audit guard. Он проверяет private owner/modes root и копии,
inspect точного image/OCI revision, отсутствие image VOLUME и неразрешённых
ENV, затем выполняет только этот мигратор. У Docker минимальное окружение,
фиксированный socket и отсутствующий private DOCKER_CONFIG без credentials.
Миграция не получает host env/token, stdin закрыт, stdout/stderr не публикуются.
После запуска или timeout broker удаляет только своё уникальное container name
и подтверждает его отсутствие через успешный Docker inventory; отказ cleanup
закрывает продолжение. Никаких Docker команд в этом подэтапе не выполнялось.

Audit guard controller не ослаблен: subprocess/socket всё ещё запрещены.
Перед Linux сценариями нужен review конкретного harness/broker вызова и
связи request/result с candidate/image/копией. Прямой вызов broker внутри guard
получит отказ, а запуск полного production CLI не является transport проверки.
Broker не проверяет CI самостоятельно: он принимает только точные image/SHA
после внешнего проверенного admission, не служит production deploy entry point.

## Проверить локально и продолжить

В worktree `.venv` отсутствует. Проверены unittest/sqlite3 явным
`C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe`.

```powershell
$env:PYTHONUTF8='1'
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -B -m unittest tests.test_deploy_rollout tests.test_stage7_transport tests.test_deploy_database_switch tests.test_deploy_database_recovery tests.test_deploy_runtime tests.test_stage6_integration
```

44 tests, OK, 21.035 с. После добавления broker тестов отдельно:
`tests.test_stage7_migration tests.test_deploy_rollout` — 3 tests, OK, 0.033 с.
После добавления позднего отказа допуска отдельно:
`tests.test_deploy_database_switch.DatabaseSwitchTests.test_final_candidate_refusal_does_not_stop_or_publish_marker`
— 1 test, OK, 0.101 с. Проверены реальные функции gate с моделями внешних
ответов, изменения exact selection/artifact/policy/controller/image/baseline,
отказы snapshot/effective boot/backup/guard/reload/writers, закрытые Docker
argv/env, OCI/credential/volume отказ, timeout/cleanup и отсутствие marker/stop
после позднего отказа. Linux effective units и реальная image migration этим
не подтверждены. Полная локальная матрица не повторялась.

Далее родитель проверяет код, фиксирует SHA и выполняет отдельно разрешённый
push для CI. До единственной остановки обязательны полный exact-SHA CI и
compatibility artifact, точный image/OCI и snapshot, private Linux preflight
модели и broker, свежий baseline/место/backup и окно без ручных writers.
Отказ сохраняет действующие каналы. После допуска продолжить штатный сценарий
на копии по 7.3 и единое окно 7.3–7.6; этот отчёт его не начинает.
