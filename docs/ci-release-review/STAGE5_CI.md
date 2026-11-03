# Подключить доверенную проверку SQLite к полному CI: шаг 5.4

Документ описывает подготовленный обязательный CI-контракт для точной пары
previous/candidate и его локальную проверку 03.11.2026. Матрица assertions
описана в [5.3](STAGE5_MATRIX.md), изоляция — в [runner](STAGE5_RUNNER.md).
Проверка artifact перед deploy остаётся шагом 5.5 [плана](PLAN.md).

## Закрепить policy перед первым полным CI

Внешняя repository variable `COMPATIBILITY_POLICY_SHA` должна содержать полный
lowercase SHA проверенного commit с этой реализацией. Её задаёт владелец
репозитория после review; input кандидата, branch и tag не заменяют pin.
В этой локальной работе variable не создавалась, commit/push/PR не выполнялись.
**Без pin новый full CI отказывает**. Это ожидаемое условие активации,
а не уже подтверждённый зелёный GitHub run.

Policy хранится в
[`repository_compatibility_policy.json`](../../scripts/repository_compatibility_policy.json).
Первая версия закрепляет `v3.1.0`, Release ID `431378458`, previous SHA
`dd71c2ba42a0744b5b654cedcf73009ce8879058`, 26 классов, 107 методов и 173 test IDs.
CI повторно получает Release/tag через GitHub API: нужны тот же ID/tag/SHA,
`immutable=true`, `draft=false`, `prerelease=false`, дата публикации и прямой
tag типа `commit`. Input `previous-release-sha` используется только для
проверки равенства этой базе. Controller продолжает выбирать выпуск по
действующим правилам линии; несогласие с policy закрывает CI.

Для следующей базы или другой линии требуется отдельный review policy:
инвентаризация её API/tests, собственный manifest и новый внешний pin.
Нельзя автоматически заменить baseline последней публикацией или SHA
установленного приложения. Для изменений workflows также нужен новый
проверенный pin: candidate `full-suite.yml`, `main-full.yml`, `release-full.yml`
должны совпасть с Git bytes policy commit. Первое закрепление делается после
создания commit, поскольку SHA нельзя достоверно записать заранее.

## Выполнить оба набора

Reusable [full-suite.yml](../../.github/workflows/full-suite.yml) сначала
запускает отдельный `repository-compatibility` job на чистом Ubuntu runner.
Он получает policy и candidate в разные checkout; candidate requirements
и tests до проверки policy не исполняются. Trusted orchestrator сверяет
собственные исполняемые файлы/JSON с точными Git blobs внешнего pin и
проверяет все публичные методы выбранных классов previous через AST.

Дальше он запускает trusted runner с точными SHA. Два checkout, два venv,
SQL-only overlay, старые Python/tests и imports проверяются по контракту 5.2.
API token, `GITHUB_*` control files и `ACTIONS_*` token не передаются runner.
Оба обязательных результата сохраняются независимо: candidate migrations/
data integrity и old repository behavior; требуется также двусторонний обмен.
Старый Docker image не создаётся, не скачивается и не запускается.

Validator требует все 173 закреплённых test IDs, точное число выполненных
tests, отсутствие duplicates, failures/errors/skips/expected failures.
Пустой набор и успешный агрегат без manifest дают отказ. Матрица указывает
на выполненные IDs; source inventory проверяется отдельно. Imports должны
принадлежать соответствующему точному export. SQL overlay сверяется с
candidate manifest, остальные bytes — с previous manifest. Fingerprint
fresh/upgrade/old behavior/финального чтения связывает одну candidate-схему;
равенство прежней и новой структуры не требуется.

`full-suite` зависит от успеха compatibility job и непустого SHA256 artifact
digest. Selector требует новый job и все его шаги; failure/skip/отсутствие
закрывают обычный допуск. Отказ compatibility job считается полной попыткой
даже при skipped зависимом full-suite: нужен rerun существующего run.

## Прочитать evidence и границу доверия

Artifact называется `repository-compatibility-<run_id>-<run_attempt>` и
хранится 90 дней. Он содержит `evidence.json`, runner/worker reports и logs.
Evidence связывает repository, точные candidate/previous/policy SHA,
immutable baseline, caller workflow ref, run ID/attempt, самостоятельные
результаты и SHA256 каждого файла. Оно также содержит SHA256 файлов policy.
Upload action выдаёт digest ZIP artifact; он передаётся в full-suite manifest.
При отказе сохраняются доступные diagnostic files, но успешного evidence нет.

JSON и job summary сами по себе не являются подписью или admission.
Шаг 5.5 должен получить artifact из доверенного GitHub run/attempt, проверить
repo/workflow/head SHA, полные jobs/steps, имя/ID/срок и digest artifact,
внешний policy pin и хеши внутренних файлов. Нельзя доверять `policy_sha`
только потому, что он написан внутри artifact, или принять artifact другого
rerun. Проверка Git bytes workflow нужна также снаружи candidate job: кандидат,
удаливший весь verifier, не должен получить доверие по похожим именам steps.
Эта доставка/проверка до остановки ботов локально реализована в
[шаге 5.5](STAGE5_PREDEPLOY.md); живой GitHub artifact и VPS ещё не проверялись.

Pin и GitHub provenance защищают от подмены источника проверки и случайного
смешивания exports. Произвольный враждебный Python в candidate migrations,
requirements или tests не становится безопасным sandbox: GitHub runner
исполняет reviewed repository code. Отдельный job исключает предварительное
влияние обычных unit tests, но не является границей ОС между worker и verifier.
Production-БД, credentials и пользовательские строки в CI не передаются.

Историческое исключение selector ограничено точным SHA установленной v3.1.0:
его прежний full CI остаётся допустимым без нового job. Это не допуск новой
схемы и не расширяется input/ref. Другие старые SHA этим исключением не
охвачены; их старые runs не удовлетворяют новому контракту. Старый production
controller и recovery не менялись. Контроль installed SHA и predeploy — 5.5;
startup/admission/runtime recovery — последующие этапы.

## Проверить локальный результат

Использован проектный
`C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe`:
venv текущего worktree отсутствует, `pip check` успешен.

Первый targeted прогон: 59 tests, `OK`, 41.5 секунды. Он включает настоящие
старые tests из точного v3.1.0, полный manifest на совместимой добавленной
колонке и удалении synthetic неиспользуемого поля, отказ на DROP/RENAME
используемой `app_users.display_name`, новом CHECK, trigger и NOT NULL без
default. Исходные выпущенные SQL не меняются: mutations существуют только
во временных export/SQL fixture. CHECK/trigger/NOT NULL отклоняются из-за
реальных старых writes; отдельного блокирующего schema diff нет.

Тесты контрактов дополнительно проверяют подмену baseline, SQL, imports,
матрицы, workflow, неполный test manifest, artifact digest/run/attempt и
отсутствие token у worker. Реальный GitHub API и artifact service в unit
tests заменены подготовленными ответами; live CI ещё не запускался.

Exact-pair прогон `data/step54-run1` завершился успешно:

- previous: `dd71c2ba42a0744b5b654cedcf73009ce8879058`;
- candidate source: `3862c7f442f67f247e1582aba7924d87e1dbafea`;
- 9 candidate migration tests, 147 прежних + 26 дополнительных tests;
- 179 наблюдений candidate-схемы; source manifests неизменны;
- final candidate read прежних updates и новый `verify_report` успешны.

Это локальный механизм на Windows с незакоммиченным новым harness, а не
доверенное CI evidence нового candidate commit. Оба SQL-набора этой пары
совпадают; совместимые и несовместимые изменения отдельно проверены mutations.
Полный unittest: 1056 tests, `OK (skipped=2)`, 115.1 секунды; два пропуска —
существующие Linux-only cases на Windows, не compatibility suite. После
уточнения tamper-проверок повторно прошли все 5 контрактных tests.
YAML разобран локальным PyYAML, dependency job проверен; actionlint и Linux
GitHub run не выполнялись. VPS, deploy и два отменённых image-probe scripts не менялись.
