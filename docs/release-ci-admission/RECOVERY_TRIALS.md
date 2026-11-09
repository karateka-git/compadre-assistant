# Испытания восстановления выпуска

Здесь описано, как воспроизвести сценарии из пункта 5 [плана](PLAN.md) после
перехода на допуск по аннотированному тегу. Порядок переключения показан
в [схеме SQLite](CI_AND_DEPLOY.md#переключить-поколение-sqlite), действия
оператора описаны в [рабочей инструкции](../OPERATIONS.md).

## Подготовить окружение

Каждый сценарий создаёт свой `TemporaryDirectory`. Внутри него контроллер
пишет `state.json`, `active-db.json` и marker `deploy-in-progress`. SQLite,
журнал миграций, WAL, снимок через SQLite backup API и атомарная запись
файлов настоящие. Docker, systemd и GitHub заменены управляемыми ответами.
Используются синтетические SHA, ID контейнеров и строки сообщений. Проверка
тега создаёт временный Git-репозиторий с локальными commit и annotated tag.
Испытания проходят без VPS, production-БД, публикации и живого polling.

Запускать из корня worktree. `-p` выбирает модуль, `-k` — отдельный сценарий.

```powershell
$recoveryPython = if (Test-Path '.venv/Scripts/python.exe') {
    (Resolve-Path '.venv/Scripts/python.exe').Path
} else {
    $primaryWorktree = (git worktree list --porcelain | Select-Object -First 1) -replace '^worktree ', ''
    Join-Path $primaryWorktree '.venv/Scripts/python.exe'
}
$env:PYTHONIOENCODING = 'utf-8'
& $recoveryPython -m unittest discover -s tests -p 'test_predeploy_compatibility.py' -k 'annotated_evidence_reads_four_pinned_files_without_artifact' -v
& $recoveryPython -m unittest discover -s tests -p 'test_predeploy_compatibility.py' -k 'copy_preserves_live_wal' -v
& $recoveryPython -m unittest discover -s tests -p 'test_deploy_database_switch.py' -k 'snapshot_selector_runtime_and_admission_order' -v
& $recoveryPython -m unittest discover -s tests -p 'test_deploy_database_recovery.py' -k 'before_admission_recovers_old_at_every_prestart_stage' -v
& $recoveryPython -m unittest discover -s tests -p 'test_deploy_forward_recovery.py' -k 'recovers_same_ids_and_new_database' -v
```

Если ни в одном worktree нет проектного Python, подготовить его по
[инструкции настройки](../SETUP.md).

Эти пять проверок прошли при подготовке. Первая сверяет точные байты локального
Git-тега, четыре чтения из GitHub fixture по закреплённому SHA и восстановление
доказательства без артефакта. Вторая проверяет сохранение записей WAL в копии.
Третья проверяет, что до `docker start` записаны выбор новой БД и
`admission_may_have_started=true`. Последние две проверяют базовые сценарии
автоматического и forward recovery с подставленным доказательством. Проверки
целых путей с новым допуском и результаты испытаний записываются отдельно.

## Пройти сценарии

| Шаг | Точка входа и существующий сценарий для `-k` | Что подтвердить |
| --- | --- | --- |
| 5.2. Штатная установка | [`test_deploy_database_recovery.py`](../../tests/test_deploy_database_recovery.py), `test_annotated_deploy_and_automatic_recovery_boundaries`. | Пройти `prod.deploy` по annotated v2, сверить `success`, `last_good`, selector `new`, образ и SHA обоих каналов, исходную запись в новой БД и проверенный backup. |
| 5.2. Отказ до миграции и после миграции предварительной копии | [`test_predeploy_compatibility.py`](../../tests/test_predeploy_compatibility.py), `test_annotated_route_keeps_writers_before_failed_local_or_remote_admission`. | Вызвать отказ чтения CI до миграции, затем несовпадение отпечатка или pin после миграции копии. Проверить `failed_preflight`, прежние SHA и БД, отсутствие marker и остановки каналов. |
| 5.2. Отказ после остановки каналов | [`test_deploy_database_recovery.py`](../../tests/test_deploy_database_recovery.py), `test_annotated_deploy_and_automatic_recovery_boundaries` и `test_before_admission_recovers_old_at_every_prestart_stage`. | До отметки приёма проверить возврат старых ботов на `old`. После отметки добавить запись в WAL новой БД и сверить её после возврата прежних ботов на `new`. Повторить со сменой доказательства перед запуском старых ботов и проверить закрытый запуск. |
| 5.3. Остановить автоматику | [`test_deploy_database_recovery.py`](../../tests/test_deploy_database_recovery.py), `test_bad_evidence_pin_selector_database_or_boundary_stop_every_writer`, `test_malformed_or_missing_state_preserved_and_both_entry_points_guard`. | Проверить, какие stage, причина и сведения о guards доступны оператору. Сверить выбранную БД, marker, остановку обоих writers и состояние таймера. Отдельно отметить случаи, когда подтверждение остановки получить не удалось. |
| 5.3. Выполнить forward recovery | [`test_deploy_forward_recovery.py`](../../tests/test_deploy_forward_recovery.py), `test_recovers_same_ids_and_new_database`, `test_rejects_selector_schema_or_evidence_drift`. | Вызвать `forward.recover` после сбоя финализации. Сверить тот же candidate SHA, пару container ID и выбранную БД, записи, `success`, снятие marker и восстановление таймера. `ForwardHost` подменяет `require_recovery_evidence`. Для проверки нового допуска связать этот вызов с `Host.require_recovery_evidence` и временным Git из проверки тега. |
| 5.4. Повторить после отказа | [`test_deploy_database_switch.py`](../../tests/test_deploy_database_switch.py), `test_final_candidate_refusal_can_be_retried_without_stopping_channels`. | Повторить `prod.deploy` после `failed_preflight`. Сверить состояние, прежние каналы, selector и записи. Проверить, что изменившийся marker или selector закрывает безопасный повтор. |
| 5.4. Возобновить после прерывания | [`test_deploy_database_recovery.py`](../../tests/test_deploy_database_recovery.py), `test_recovery_crash_boundaries_converge`, `test_repeated_command_recovery_retains_gate_and_single_pair`. [`test_deploy_forward_recovery.py`](../../tests/test_deploy_forward_recovery.py), `test_retry_after_process_crash_uses_same_running_ids`. | Прервать процесс после записи состояния или после `docker start`. Сверить результат повтора, выбранную БД, записи и инвентарь активных контейнеров. При неоднозначности проверить остановку известных writers. |
| 5.5. Разделить готовность каналов | `Host.verify_both` из [`deploy_prod.py`](../../scripts/deploy_prod.py) с подменой `containers`, `image`, `logs` и монотонных часов. Для проверки отсутствия дублей есть `test_duplicate_polling_detected_without_logging_token` в [`test_deploy_prod.py`](../../tests/test_deploy_prod.py). | Задать один готовый канал и второй без health или сигнала polling. Проверить текущий исход, затем возможность сохранить готовый канал при доказанном образе, выбранной БД и отсутствии чужих writers. Проверить ограниченный повтор, полный возврат после подтверждения обоих и безопасный отказ при подмене. `RecoveryHost.verify_both` упрощён и сам по себе эти случаи не покрывает. |

## Записать результат

До внесения отказа записать целевой SHA и ожидаемый backup. После отказа
прочитать сохранённое состояние с диска, сверить selector с `DATABASE_PATH`
обоих каналов и проверить SHA через OCI revision образа. Для созданного
снимка сверить путь, SHA-256, целостность, foreign keys и исходную строку
`original`. После отметки приёма проверить строку `accepted-after-admission`
на новой БД. Отдельно записать факт записи marker и состояние таймера.

Если испытание фиксирует время, измерять для каждого канала от подтверждённой
остановки до подтверждённой готовности на монотонных часах. При замене часов
указывать модельную длительность. Если есть только порядок событий либо
готовность до конца сценария не подтверждена, писать «не измерен» и причину.
Простой реального канала в этих испытаниях не измеряется.

| Сценарий и команда | Целевой SHA | Backup и проверки | Условие входа, отказ и остановка | Фактический stage, БД, SHA и ID каналов | Тестовые записи и риск потери | Простой Telegram и VK | Решение |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Заполнить при выполнении сценария | | | | | | | |

## Результаты 5.2. Установка и автоматический возврат

```powershell
& $recoveryPython -m unittest discover -s tests -p 'test_deploy_database_recovery.py' -k 'annotated_deploy_and_automatic_recovery_boundaries' -v
& $recoveryPython -m unittest discover -s tests -p 'test_predeploy_compatibility.py' -k 'annotated_route_keeps_writers_before_failed_local_or_remote_admission' -v
```

9 ноября 2026 года все пять вариантов сквозного `prod.deploy` прошли.
Целевой SHA в каждой строке — `51e628720402e528dd57c4a38c5e304ed829763a`,
commit временного аннотированного тега. После сохранения допуска тест сверяет
его с `candidate.sha`. Обозначение `old SHA` — прежний SHA из `last_good`
и OCI revision. Контроллер применял SQL, добавляющий индекс и
запись в журнал миграций, к двум отдельным копиям. Итоговый снимок
`final-<generation_id>.db` сверялся по пути и SHA-256, исходному журналу,
целостности, foreign keys и строке `original`. Идентификатор снимка
формируется заново при каждом прогоне.

| Сценарий | Целевой SHA | Backup | Фактический исход | Сохранённые записи |
| --- | --- | --- | --- | --- |
| Штатная установка | SHA кандидата | Проверен, путь записан в `last_good.backup_id` | `success`, выбрана `new`, оба канала на SHA кандидата. | `original` на `new`, файл `old` без изменений. |
| Несовпадение reviewed workflow до миграции | SHA кандидата | Снимок ещё отсутствует | `failed_preflight` с `SAFE_WORKFLOW_BYTES`, прежняя БД, оба канала на old SHA продолжают работу. | `original` на прежней БД, файл без изменений. |
| Ошибка после миграции и создания контейнеров до разрешения приёма | SHA кандидата | Проверен и сохранён | `rolled_back`, selector вернулся на `old`, оба канала на old SHA. | `original` на `old`, мигрированная копия сохранена. |
| Ошибка готовности после записи кандидатом | SHA кандидата | Проверен и сохранён | `rolled_back`, selector остался на `new`, оба канала на old SHA. Доказательство подтвердилось при выборе пути возврата и перед запуском. | `original` и `accepted-after-admission` на `new`, байты WAL сохранены. |
| Reviewed workflow изменился после подготовки прежних контейнеров к запуску | SHA кандидата | Проверен и сохранён | `degraded`, selector остался на `new`, оба канала остановлены, marker сохранён. | `original` и `accepted-after-admission` на `new`, байты WAL сохранены. |

В отдельном прогоне предварительного допуска несовпадение отпечатка или
локального pin после миграции пробной копии тоже оставляло старые каналы
работающими. Новый verifier читает Contents через GitHub fixture и настоящий
локальный тег. Результат выбора Release и CI предоставляется fixture,
работа Docker, systemd, polling и таймера моделируется. Простой Telegram и
VK не измерялся. В модели подтверждены порядок действий, выбор SQLite и
сохранность записей.
