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

## Исправить отказы первого полного CI на Linux

06.11.2026 полный [main-full 42242176741/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42242176741)
для merge SHA `14c8306d035769726e9982a92c8b6a91db5031e9` завершился failure.
Compatibility job успешен, но это не полный допуск: full-suite остановился
на unittest. В job `127975536683` выполнены 1129 tests, 110 errors и 5 failures.
Подготовка harness остановлена; VPS и боты не затрагивались.

Разобраны все группы traceback: 104 ERROR возникают при root-only проверке
selector во временных non-root Linux fixtures. Ещё 6 ERROR (`KeyError: old_tests`)
возникают у SQL mutation tests после раннего отказа worker на hosted system
Python. Четыре FAIL — assertions положительных worker scenarios и проверки
missing module, которые также получили ранний отказ из-за отсутствия venv.
Последний FAIL — отдельный subprocess transport test с той же ошибкой owner
selector. Итого обе причины объясняют все 110 errors/5 failures; сообщения
ожидаемых инъекций ошибок в основном логе не являются отдельными CI отказами.

Исправления ограничены tests. `FixtureStatePath` используется только fake
Host и моделирует root uid/gid только у `active-db.json`; реальные файл,
mode, type, nlink, inode и проверки selector/state остаются. Новый процесс
Stage6 получает ту же явную test-path модель. Runtime-код и требование
настоящего root:root selector в production не менялись. Отдельный тест
включает POSIX ветку validator и подтверждает отказ для non-root owner,
неприватного mode и alias без запуска root-процесса.

Worker tests теперь явно получают venv через `worker_python()`. Если suite
уже работает в проектном venv без system site, используется его Python.
При hosted system Python один раз создаётся отдельный temporary venv,
requirements устанавливаются в него и выполняется pip check; system
site-packages не подключаются. Workers сохраняют обязательные `-I -B` и
собственную проверку изоляции. Workflow и trusted policy bytes не менялись.

После исправления выполнены через явный проектный venv:
`tests.test_deploy_database_switch tests.test_deploy_database_recovery
tests.test_deploy_runtime tests.test_stage6_integration tests.test_stage7_transport
tests.test_repository_compatibility_runner tests.test_repository_compatibility_contract`
— **61 tests OK, 61.419 с**. Дополнительные tests модели owner и ветки
подготовки среды hosted Python — **3 tests OK, 0.010 с**.
`git diff --check` успешен. Настоящие SQL mutation/old API workers выполнены;
системные вызовы подготовки новой среды в дополнительном unit test моделируются.

Локальный Linux Docker daemon недоступен, WSL docker-desktop не содержит
Python. Реальный non-root Linux прогон не выполнен; тест POSIX условия на
Windows не объявляется его заменой. Требуются review, новый commit/merge SHA
и новый успешный полный CI точной пары; прежний compatibility artifact для
`14c8306...` не переносится на новые байты. Исполнитель commit/push не делал.
Остановка ботов и исполнение migration broker остаются заблокированы.

Следующий [полный run 42243898274/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42243898274)
для `62e029a0532e901a18aae1aebb38e95dc3f59254` выполнил 1132 tests с одним
ERROR: backup v2 fixture создавал selector через `write_text`, то есть с mode
0644 на Linux. Root uid/gid уже моделировались через SwitchHost; оставался
реальный небезопасный mode файла. Fixture переведён на настоящий
`prod._atomic_text`, который пишет 0600 и fsync. Runtime guard и модель uid/gid
не менялись. Локально явным проектным Python выполнены
`tests.test_deploy_backup tests.test_deploy_fixture_paths` — 5 tests OK,
0.477 с. Нужен новый exact-SHA полный CI; этот run допуска не даёт.
Черновой `stage7_3_harness.py` пока не проверен и не входит в этот CI fix.

## Native proof и исправление fingerprint — 06.11.2026

Для candidate `595d1432ea4c39e1356f3f35dcb9c74530fa6b17` полный CI
`42245216949/1` success; native Linux verifier, exact snapshot и private image
build успешны. Native synthetic boundary обнаружил несовпадение schema
fingerprint CI/runtime и корректно закрыл дальнейший допуск. Подробные
receipts, границы изоляции и порядок повторного допуска —
[STAGE7_3_NATIVE_PROOF.md](STAGE7_3_NATIVE_PROOF.md).

Подготовлено узкое исправление runtime под стандартное JSON-кодирование и
ORDER BY pinned trusted worker; общий compact SQL digest и policy не меняются.
Нужны новый merge SHA, полный CI, snapshot/image и Linux proof. Предыдущие
утверждения «Docker не менялся» выше относятся к раннему локальному подэтапу:
теперь выполнен только private build, без runtime/migration контейнеров.
Боты не останавливались, production state/units/controller/БД не менялись.

Повтор для исправленного exact SHA `cabc0e24d28947123868db8a60f6b55095feb63e`
успешен: manual full CI `42249037867/1`, native verifier/snapshot 63 files,
exact image/OCI, Linux synthetic с совпадающим CI/runtime fingerprint.
[Новые receipts и граница review](STAGE7_3_NATIVE_PROOF.md#повторить-доказательства-исправленного-кандидата).
Реальный closed-copy migration broker не запускался; боты работают. Следующее
действие — review родителем до единственного окна остановки, шаг 7.3 открыт.

Перед stop подготовлен и успешно выполнен только `prepare()` нового закрытого
bootstrap: [код, staging hashes, stop и recovery sequence](STAGE7_3_STOP_REVIEW.md).
Harness real receipt связан с exact reviewed native CI/image/synthetic bytes;
самозаявленный full_ci_success не допускает запуск. Подготовленный real01 root
содержит main copy без sidecars, настоящий case не начат. Исходные fixture
WAL0/SHM сохранены и проверяются по exact metadata; live контур неизменен.

Первое разрешённое окно real01 завершено recovery после отказа stop
attestation; realcase не стартовал. Старые service/IDs/image/health/polling
восстановлены, интервал stopattempt→verifiedrecovery32.235с. Исправлены private
bootstrap dynamic systemctl hash и /proc scan; 8 testsOK, native read-only
baseline успешен. [Причина, evidence и условие нового review](STAGE7_3_STOP_REVIEW.md#разобрать-первое-окно).
Второй stop требует отдельного go после нового staging.

Real03 прошёл actual closed-copy case17:04:37UTC и bounded proof17:05:49UTC;
родитель независимо сверил hashes result/state/selector и serviceinactive/
timeractive. [Шаг7.3 завершён; результат и caveats](STAGE7_3_RESULT.md).
Текущее maintenance окно остаётся открытым для7.4–7.5, боты OFF. Два image
CLI/RPC прогона не означают новыеSQL (schema1–4); polling/start нового runtime
и production units/controller установка остаются непроверенными до7.7.
