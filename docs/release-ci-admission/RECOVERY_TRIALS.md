# Испытания восстановления выпуска

Здесь собраны результаты испытаний из пункта 5 [плана](PLAN.md) и команды
для их повтора с допуском по аннотированному тегу. Порядок переключения
показан в [схеме SQLite](CI_AND_DEPLOY.md#переключить-поколение-sqlite),
действия оператора описаны в [рабочей инструкции](../OPERATIONS.md).

## Решение по восстановлению

Испытания подтвердили действующий порядок автоматического возврата.

- После `failed_preflight` до остановки каналов можно повторить установку
  той же версии, когда причина устранена, пауза истекла, а состояние прежней
  установки, selector и отсутствие marker подтверждены.
- При подтверждённом `admission_may_have_started=false` контроллер может
  вернуть прежних ботов на прежнюю SQLite. При `true` он сохраняет новую
  SQLite и допускает на неё прежний образ после повторной проверки
  совместимости. Если снова выбрать прежний файл, в нём не будет записей,
  сделанных после границы приёма.
- Если один канал готов, контроллер ограниченно ждёт готовности обоих,
  проверяя ту же пару без повторного запуска. При устойчивом сбое
  сохраняется защитная остановка. Действующий протокол завершает
  восстановление unit и таймера для проверенной пары контейнеров.
- При неоднозначности оператор сверяет selector, snapshot и записи,
  фактическое состояние контейнеров и таймера, marker и доказательство CI.
  Состояние `degraded` само по себе не подтверждает остановку процессов.
  Если отказала финализация уже принятого выпуска, оператор может
  выполнить forward recovery после проверки допуска того же выпуска,
  поколения и двух точных container ID. В остальных случаях дальнейший
  путь определяет владелец по фактическому состоянию.

Проверки выполнялись 9 ноября 2026 года на временной SQLite с синтетическими
данными. Целевой commit аннотированного тега —
`51e628720402e528dd57c4a38c5e304ed829763a`. В сценариях после границы приёма
проверяли исходную строку в backup и сохранение новой записи на выбранной
SQLite. Docker, systemd и GitHub моделировались. Простой Telegram и VK
не измерялся. Время в испытании готовности отсчитано от запуска recovery-пары
по модельным часам и показывает длительность проверки.

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
| 5.3. Выполнить forward recovery | [`test_deploy_forward_recovery.py`](../../tests/test_deploy_forward_recovery.py), `test_annotated_finalization_failure_recovers_only_the_same_release` и `test_rejects_selector_schema_or_evidence_drift`. | Вызвать `forward.recover` после сбоя финализации. Проверить реальный `Host.require_recovery_evidence` на временном Git-теге и смоделированном API. Сверить тот же candidate SHA, пару container ID и выбранную БД, записи, `success`, снятие marker и восстановление таймера. |
| 5.4. Повторить после отказа | [`test_deploy_database_recovery.py`](../../tests/test_deploy_database_recovery.py), `test_annotated_deploy_retries_after_pre_stop_refusal`. [`test_deploy_database_switch.py`](../../tests/test_deploy_database_switch.py), `test_final_candidate_refusal_can_be_retried_without_stopping_channels`. | Повторить `prod.deploy` после отказа локальной сверки аннотированного тега. Сверить состояние, прежние каналы, selector и записи. Проверить, что изменившийся marker или selector закрывает безопасный повтор при уже включённом SQLite v2. |
| 5.4. Возобновить после прерывания | [`test_deploy_database_recovery.py`](../../tests/test_deploy_database_recovery.py), `test_recovery_crash_boundaries_converge`, `test_repeated_command_recovery_retains_gate_and_single_pair`. [`test_deploy_forward_recovery.py`](../../tests/test_deploy_forward_recovery.py), `test_retry_after_process_crash_uses_same_running_ids`. | Прервать процесс после записи состояния или после `docker start`. Сверить результат повтора, выбранную БД, записи и инвентарь активных контейнеров. При неоднозначности проверить остановку известных writers. |
| 5.5. Разделить готовность каналов | [`test_deploy_recovery_channels.py`](../../tests/test_deploy_recovery_channels.py) запускает возврат с реальным `Host.verify_both`, модельными монотонными часами и имитацией Docker. | Задать один готовый канал и второй без health или сигнала polling. Сверить выбранную БД, образ и ID. Проверить ожидание на той же паре, завершение возврата после готовности обоих, защитную остановку после таймаута и отказ до запуска при чужом polling. |

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

## Результаты 5.3. Остановка автоматики и ручное восстановление

```powershell
& $recoveryPython -m unittest tests.test_deploy_database_recovery tests.test_deploy_forward_recovery
```

9 ноября 2026 года прошли 28 тестов. После запуска кандидата с SHA
`51e628720402e528dd57c4a38c5e304ed829763a` тест сохранил строку
`accepted-after-admission` в выбранной SQLite. Ошибка принятия контейнеров
основным systemd unit прервала `finalization_intent`. При попытке
автоматического возврата смоделированный отказ GitHub Contents закрыл
проверку доказательства для прежних ботов.

| Сценарий | Фактический исход | Граница действий |
| --- | --- | --- |
| Повреждённый или потерянный `state.json` | Обычная установка и `--verify-pinned` отказали без повторного запуска известных writers. Повреждённый файл сохранился, взамен потерянного новый state не появился. В `recovery-blocked.json` записана причина `STATE_UNREADABLE`. | Оператор сохраняет файлы, отдельно подтверждает selector, остановку writers и таймера. Пригодное поколение SQLite нужно определить по сохранённым файлам. |
| Не завершилась финализация, автооткат не подтвердил доказательство | `degraded`, маркер `critical` сохранён, таймер выключен, оба writers остановлены. Выбрана `new`, в `last_good` сохранился прежний SHA. | Сверить `rollback_origin_stage`, `boot`, выбранное поколение, точные контейнеры и результат допуска. После возможной записи прежнюю SQLite оставляют как источник для разбора. |
| Ручной forward при изменившейся попытке CI | Отказ без `docker start`, сохранены `degraded` и защитные условия. | Для ручного продолжения нужно подтвердить тот же выпуск и ту же попытку. |
| Ручной forward при восстановленном доступе и точном доказательстве | `success` с тем же SHA, поколением `new` и двумя исходными container ID. Реальный verifier прочитал локальный тег и сделал четыре запроса к подменённому Contents API. Контроллер запустил эти ID, снял marker и восстановил таймер. | Допустимо продолжить ту же финализацию после проверки `boot.outcome=success`, `migration_attempted=false` и отсутствия чужих writers. |

В сценарии с точным тегом снимок `final-<generation_id>.db` совпал с
сохранённым SHA-256. Проверки целостности и foreign keys прошли на снимке
и обеих БД. `original` сохранилась в снимке и обоих поколениях,
`accepted-after-admission` осталась на `new`. Повтор использовал существующие
контейнеры и SQLite. Мигратор и `docker compose create` после отказа
финализации не вызывались. Простой Telegram и VK не измерялся, поскольку
Docker, systemd и готовность каналов моделировались.

Обычный контроллер сообщает, что возврат не подтверждён и запуск закрыт.
Короткое сообщение CLI forward recovery показывает класс ошибки. Для
подтверждения остановки оператор сверяет фактические Docker ID и их
restart policy, marker и состояние таймера. Тест с отказом `docker stop`
подтвердил, что даже сохранённый `degraded` не доказывает остановку
каждого writer. Порядок действий описан в [инструкции по возврату](../OPERATIONS.md#отказ-выпуска-и-возврат).

## Результаты 5.4. Повтор после отказа и прерывания

```powershell
& $recoveryPython -m unittest tests.test_deploy_database_recovery tests.test_deploy_database_switch tests.test_deploy_forward_recovery
```

9 ноября 2026 года прошли 45 тестов. Целевой SHA аннотированного
выпуска в тесте повтора — `51e628720402e528dd57c4a38c5e304ed829763a`.
Остальные сценарии этого шага используют синтетический кандидат
`bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`.

| Сценарий | Backup и записи | Фактический исход |
| --- | --- | --- |
| Повтор `deploy-prod` после локального `SAFE_POLICY_PIN` | При первом отказе снимка ещё нет, прежняя SQLite побайтно совпадает. После успешного повтора проверены снимок, SHA-256 и `original` в новой БД. | После миграции предварительной копии контроллер записал `failed_preflight`. Оба прежних ID продолжили работу без stop, create или start. Немедленный повтор остановился на cooldown. После восстановления pin и имитации истечения cooldown тот же Release дошёл до `success` и `new`. Каждый допуск сделал четыре чтения Contents, закрытый cooldown не сделал ни одного. |
| Отказ при уже включённом SQLite v2 | Выбранная SQLite сохранилась побайтно. Новый backup до допуска не создаётся. | Повтор дошёл до сверки без остановки. Это подтвердилось и с историческим `failed_preflight`. При несовпадении marker или selector безопасный повтор закрывается и фиксируется `degraded`. |
| Повтор автоматического возврата после прерывания | Реальный recovery сверил снимок из `state.json` и его SHA-256. В восьми точках до границы приёма сохранилась `original` на `old`. В шести точках после границы сохранились `original` и `accepted-after-admission` на `new`. | После восстановления из сохранённого состояния получен `recovery_verified`. Работают два точных ID, записанных в `recovery_containers`, на прежнем образе. Прерванная попытка может потребовать остановки и пересоздания пары. Тестовый Docker запрещает create или start при уже работающем writer. |
| Повтор forward recovery после частичного `docker start` | Используются существующая `new` и ранее сделанный снимок. Проверка записей для точного forward recovery приведена в результате 5.3. | Telegram уже работал. Повтор запустил только сохранённый ID VK и дошёл до `success`. Если marker пропал или API перестал подтверждать доказательство, запущенный writer останавливается, сохраняются `degraded` и marker. |

Простой Telegram и VK не измерялся. Испытания фиксировали выбор поколения,
данные и порядок команд через имитацию Docker, systemd и GitHub.

## Результаты 5.5. Готовность одного канала при возврате

```powershell
& $recoveryPython -m unittest tests.test_deploy_recovery_channels -v
```

9 ноября 2026 года прошли три теста с пятью вариантами. Целевой SHA
кандидата — `51e628720402e528dd57c4a38c5e304ed829763a`, прежний образ
проверялся по `old_sha` и OCI revision. После границы возможного приёма
тест записал `accepted-after-admission` и вызвал штатный recovery. Он
сверил выбранную `new`, прежний образ и подтверждение совместимости по
аннотированному тегу перед запуском. Готовность проверял реальный
`Host.verify_both` с ответами тестового Docker.

| Условие | Фактический исход |
| --- | --- |
| Telegram готов, health VK появился на 12-й модельной секунде | Пара подтверждена на 24-й секунде, финализация завершилась на 36-й. Прежние боты остались на `new`. Точные ID сохранились, второго `create` или `start` прежней пары не было. |
| Telegram готов, сигнал polling VK появился на 12-й секунде | Те же времена и итог `rolled_back`. В начале ожидания оба контейнера имели `healthy`, а в логах сигнал был только у Telegram. Таймер восстановился после финализации пары. |
| VK не стал `healthy` или его polling не подтвердился | По истечении 120 модельных секунд контроллер остановил оба канала, хотя Telegram был готов. Сохранены `degraded`, marker, выключенный таймер и `new`. При отсутствующем health общая проверка не доходила до чтения логов. |
| В инвентаре есть чужой активный контейнер с тем же VK credential | Возврат остановлен до `create` и `start` прежних ботов. Контроллер оставил известные каналы остановленными, сохранил `degraded`, marker и `new`. |

Во всех вариантах снимок `final-<generation_id>.db` совпал с сохранённым
SHA-256, прошёл проверку журнала миграций, целостности и foreign keys,
содержал `original`. Выбранная БД сохранила `original` и
`accepted-after-admission`. Времена отсчитываются от запуска прежней пары
на модельных монотонных часах. Простой реального Telegram и VK не измерялся.

Краткая задержка уже покрыта ограниченным ожиданием с повторным чтением
статуса и логов той же пары. После таймаута безопасное продолжение с одним
каналом в текущем протоколе не подтверждено. Boot intent и проверка
готовности привязаны к двум точным ID, штатный unit принимает пару, а
таймер возвращается при финализации. Повтор из незавершённой стадии снова
закрывает writers. Простое предупреждение оставило бы работающий канал
без сохранённого состояния для безопасного повтора или перезагрузки.
Проверка credentials охватывает видимые Docker-контейнеры в момент
проверки. Живой polling и посторонние процессы вне Docker в испытании
не проверялись. При устойчивом отказе сохраняем текущую защитную остановку.
