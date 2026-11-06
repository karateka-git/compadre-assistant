# Проверить границу допуска независимо: шаг 5.6

Запись от 03.11.2026 связывает локальные интеграционные проверки с
[планом](PLAN.md), [матрицей](STAGE5_MATRIX.md), [CI](STAGE5_CI.md) и
[predeploy](STAGE5_PREDEPLOY.md). Практическая расшифровка SAFE-кодов и поиск
диагностики — в [OPERATIONS.md](../OPERATIONS.md#разобрать-отказ-predeploy-по-safe-коду).
VPS, GitHub variable и controller snapshot не изменялись; это локальная
проверка механизма, а не эксплуатационная приёмка.

## Сопоставить сценарии и доказательства

| Сценарий | Исполненная проверка |
| --- | --- |
| Совместимые ADD и DROP неиспользуемого поля | `CompatibilitySqlMutationTests` исполняет полный manifest из 173 tests на настоящих SQL, без блокирующего schema diff. |
| Сломанное прежнее чтение | AFTER INSERT trigger сохраняет INSERT успешным, но меняет `display_name`; прежний read assertion падает. |
| Сломанная прежняя запись | DROP/RENAME используемого поля, CHECK, deny trigger и NOT NULL без default вызывают ошибки старых writes. |
| Неизменность previous | Exact Git export v3.1.0; до/после worker сравниваются все bytes и отдельно все non-SQL файлы с исходным manifest. Только SQL подменяются candidate fixture; old Python/tests не редактируются. |
| Неполный manifest | Удаление одного ID даже при заново согласованных ZIP/hash даёт `SAFE_TEST_MANIFEST`. |
| Installed/previous SHA mismatch | Настоящий verifier даёт `SAFE_INSTALLED_PREVIOUS_SHA` до создания копии, marker и stop. |
| Ранние проверки кандидата | Ошибка source schema, manifest, префикса миграций и candidate image имеет собственный SAFE-код в state/metrics и публичном `DeployError`; marker/stop отсутствуют. |
| Отказ доставки | Неверный ZIP digest и API exception дают соответственно `SAFE_ARTIFACT_DIGEST` и `SAFE_EVIDENCE_READ`. |
| Граница deploy | Для четырёх отказов выше выполнены ordinary и emergency через real Host.predeploy/deploy с fake внешними Git/Docker: checkout возвращён, channels работают, marker/stop отсутствуют. Тот же SAFE-код проверяется в state, private metrics и stderr main; внешний secret exception text отсутствует. |
| Настоящие candidate migrations на копии | `CopyTests` применяет реальные миграции проекта и дополнительную SQL настоящим runner к online backup, сверяет fingerprint, строки и отсутствие новой колонки в исходной БД. WAL, FK, потеря/изменение данных и неверный журнал проверяются отдельно, с точным SAFE-кодом. |
| Найти failed test | Mutation tests проверяют failed/error IDs в сохранённом worker JSON и соответствующее имя/traceback в `.log`; workflow сохраняет доступный output через `always()` artifact upload. |

Проверка полноты относится к закреплённым 26 SQLite-классам, 107 публичным
методам и 173 IDs. Она не доказывает все возможные значения данных. Матрица
содержит прямые assertions, а контроль inventory не позволяет молча потерять
метод или заменить полный список tests агрегатом успеха.

## Прочитать результат и ограничения

Использован явный проектный Python
`C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe`; `.venv`
этого worktree отсутствует, `pip check` успешен. Финальный targeted набор
`tests.test_predeploy_compatibility tests.test_deploy_prod`: 49 tests,
`OK (skipped=1)`; skip относится к Linux-only проверке. Полный unittest до
последнего уточнения публичного DeployError/ранних SAFE-кодов: 1069 tests,
`OK (skipped=2)`, 154.9 секунды; оба skips — Linux-only cases вне compatibility
suite. Последнее уточнение покрыто указанным финальным targeted прогоном.
Первый полный прогон выявил две неточности новых fixture-ожиданий: удаление
сначала нарушало FK. Fixtures разделены, чтобы независимо доказать FK,
потерю таблицы и потерю данных; повторный полный прогон успешен.

GitHub API/artifact metadata представлены fixture-ответами; Linux Docker,
живой CI upload/download и реальный VPS не проверялись. Интеграционные
mutation workers используют текущий project venv; отдельные previous/candidate
venv и точная пара проверены runner-прогонами 5.2–5.4, а здесь повторно
проверены их контракты, без объявления нового trusted evidence. Источник
candidate SQL mutation — временный fixture, не новый выпущенный commit.
Проверка копии вызывает настоящий runner локально; Docker CLI isolation и
cleanup timeout проверены fake subprocess, не Linux container acceptance.

До tests нет гарантии test ID; до создания output нет гарантии artifact.
Production-копия не передаётся в CI; stdout/stderr её мигратора не публикуются.
SAFE-код указывает отказавшую проверку, не содержит исходный SQL exception
или пользовательскую строку. Для неожиданных ошибок внешней библиотеки
сохраняется код этапа, а не выдуманный test ID.

Этап 6 не реализован: live migration/backup, runtime admission, граница
приёма сообщений, startup и rollback/recovery не менялись. Временный каталог
документации сохраняется до всей функциональности и эксплуатационной приёмки.
