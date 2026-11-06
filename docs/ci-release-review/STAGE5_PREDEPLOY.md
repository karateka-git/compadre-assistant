# Проверить CI evidence и установленную базу до остановки: шаг 5.5

Запись от 03.11.2026 описывает локально подготовленный predeploy-контракт
для [плана](PLAN.md). Он связывает [CI evidence](STAGE5_CI.md) с фактической
установкой и предварительной SQLite-копией. Эксплуатационные команды — в
[OPERATIONS.md](../OPERATIONS.md), разрешения на VPS — в
[PRODUCTION.md](../PRODUCTION.md). **Новый controller на VPS не установлен**;
проверки здесь выполнены на fake host и временных локальных БД.

## Подготовить внешний pin и доступ к artifact

Перед будущей установкой reviewed controller snapshot владелец должен
согласовать отдельное эксплуатационное действие и подготовить вне checkout,
в `/var/lib/compadre-assistant-deploy`, два файла root:root с mode `0600`:

- `compatibility-policy.sha`: полный lowercase SHA reviewed policy commit,
  совпадающий с `COMPATIBILITY_POLICY_SHA` успешного CI. Это независимый
  источник доверия, а не значение из artifact, candidate input или ref.
- `github-read-token`: credential с доступом только на чтение Actions и
  Contents этого репозитория. Токен не передаётся candidate, не записывается
  в evidence и удаляется из заголовков при redirect на artifact storage.

Каталог состояния уже проверяется как root:root `0700`. Отсутствующие файлы,
symlink, неправильные права, пустой token, недоступный API или неизвестный
policy pin закрывают установку. Эта работа не создаёт токен, не меняет
repository variable, файлы VPS или controller snapshot.

Endpoint и поля `digest`, `expired`, `workflow_run`, `expires_at` сверены с
[официальным REST-контрактом GitHub artifacts](https://docs.github.com/en/rest/actions/artifacts?apiVersion=2022-11-28).
Живой artifact нового gate пока отсутствует; фактический download и Linux
Docker запуск требуют отдельной приёмки после CI.

## Проверить кандидат под deploy lock

[`deploy_prod.py`](../../scripts/deploy_prod.py) сохраняет существующий lock
на протяжении подготовки и переключения. До `marker`, остановки таймера и
`writers_stopped` он вызывает
[`predeploy_compatibility.py`](../../scripts/predeploy_compatibility.py):

1. Требует полный `last_good`, равенство исходного checkout SHA, image ID и
   pinned tag. Проверяет внешний pin/systemd, оба работающих контейнера,
   локальный образ и OCI revision каждого контейнера и образа.
2. Повторно получает точный GitHub run/attempt, repo, workflow/ref/head SHA,
   jobs и steps. Для обычного выпуска нужен полный успех; для emergency
   допустим прежний обход других full-suite ошибок, но обязателен успешный
   `repository-compatibility` со всеми его steps в той же попытке.
3. Получает policy/harness blobs по внешнему точному SHA через GitHub API,
   сверяет Git blob IDs и полную policy 26 классов / 107 методов / 173 tests.
   Перепроверяет immutable baseline Release/tag и равенство previous SHA
   фактически установленному SHA. Три workflow кандидата должны совпасть
   по bytes с policy commit, независимо от похожих имён jobs/steps.
4. Получает единственный artifact `repository-compatibility-<run>-<attempt>`
   из этого run. Проверяет ID, run/head SHA, expiry и SHA256 скачанного ZIP
   против GitHub metadata. ZIP не распаковывается в filesystem; path traversal,
   дубликаты и объём больше 64 MiB до/после распаковки запрещены.
5. Проверяет точные поля evidence, внешний policy SHA, policy file hashes,
   хеш каждого внутреннего файла, оба обязательных результата, двусторонний
   обмен, manifest tests, sources/imports и fingerprints через общий validator.
   Один `passed=true` или JSON с чужой попыткой не даёт допуска.
6. Проверяет предварительную копию по процедуре ниже. После неё повторяет
   GitHub/evidence-проверку и требует тот же artifact ID/digest/policy/schema.
   Ещё раз сверяет installed baseline. Непосредственно перед `marker` выполняет
   последнюю локальную проверку `last_good`, pin, image ID и OCI revisions.

При отказе до critical checkout возвращается к исходному SHA, сохраняется
`failed_preflight`; marker, stop и миграция live-БД не выполняются. Отказ
возврата checkout оставляет прежний recovery manifest для ручного разбора.
Повтор уже установленной версии сохраняет прежний idempotent маршрут и не
использует исторический CI как допуск нового кандидата.

## Мигрировать только предварительную копию

SQLite online backup API получает согласованную копию работающей БД с WAL
в приватном временном каталоге состояния. Вторая копия помещается в отдельный
подкаталог `candidate`; только он монтируется в candidate image ID. Исходный
снимок, рабочая БД, `.env`, credentials, Docker socket и соседние каталоги
в контейнер не передаются. Сеть выключена, root filesystem read-only,
capabilities удалены, задан `no-new-privileges` и лимиты ресурсов.

Candidate запускает настоящий CLI `--migrate`. Timeout — 180 секунд; при
завершении или timeout контроллер удаляет именованный одноразовый контейнер.
Online backup ограничен 60 секундами и удаляет неполную копию при отказе.
Временный каталог удаляется по выходу из predeploy, в том числе при ошибке.
Это не окончательный backup и не граница приёма сообщений: writers ещё работают.

После миграции обязательны точный журнал, integrity, отсутствие FK violations
в исходной и новой копиях и fingerprint candidate-схемы из CI. Для каждой
исходной таблицы, кроме служебных SQLite и `schema_migrations`, сравнивается
мультимножество строк по всем сохранившимся колонкам. Проверяются значения и
кратность строк; добавление колонки и удаление неиспользуемой колонки разрешены.
Исчезновение таблицы, всех её колонок, строки или изменение общего поля дают
отказ. Содержимое БД не выводится и не отправляется в GitHub.

Это консервативный контракт сохранности: намеренное изменение значений или
backfill существующих таблиц, перестройка FTS shadow data либо удаление целой
больше не используемой таблицы требуют отдельного reviewed расширения проверки.
Нельзя автоматически принять такие изменения по одному равенству counts.
Fingerprint также может отказать при дополнительной локальной статистике
SQLite; это повод для разбора, а не для удаления проверки. Сравнение данных
работает в памяти контроллера; для очень большой БД потребуется измерение
потребления памяти и отдельное улучшение алгоритма.

## Проверить результат и границу следующего этапа

Проектный Python —
`C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe`;
`.venv` текущего worktree отсутствует, `pip check` успешен.
Полный локальный набор: 1067 tests прошли, 2 Linux-only skips. Targeted
`tests.test_predeploy_compatibility`, `tests.test_deploy_prepare`,
`tests.test_deploy_prod` проверяют положительное evidence, отказ на API outage,
expiry/digest/run/attempt/policy/baseline/workflow/hash/manifest mismatch,
emergency без успешного compatibility job, late runtime drift и отсутствие
marker/stop при preflight отказе. Реальные SQLite cases проверяют WAL,
add/drop-unused-column, потерю/изменение строк, FK, journal и fingerprint.
Отдельный тест применяет настоящие миграции проекта и дополнительную fixture
SQL настоящим candidate runner только к копии. Docker timeout/изоляция
проверены по аргументам fake subprocess; это не Linux container acceptance.

Этап 6 не реализован: live миграция, окончательный backup, startup, admission,
rollback/recovery и переключение БД остались прежними. После попытки новой
миграции прежний recovery по-прежнему запрещает автоматический возврат.
Успех локального predeploy не объявляет новую recovery-схему готовой.
