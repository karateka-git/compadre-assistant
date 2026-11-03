# Проверить прежний SQLite API на схеме кандидата: шаг 5.3

Эта запись описывает локальную проверку всех 107 публичных операций 26
SQLite-классов из [аудита 5.1](STAGE5_AUDIT.md). Для каждой операции точный
unittest ID и уровень assertion приведены в [матрице](STAGE5_MATRIX.md).
Изоляция точных Git sources описана в [runner 5.2](STAGE5_RUNNER.md). Доверенное
CI evidence и predeploy остаются шагами 5.4 и 5.5 [плана](PLAN.md).

## Запустить точную пару

Из корня закреплённого worktree выбрать Python проектного venv по
[AGENTS.md](../../AGENTS.md#рабочий-порядок) и вызвать runner с полными SHA и
новым output-каталогом. Это воспроизведение пары аудита, а не выбор
следующего release:

```powershell
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' scripts/check_repository_compatibility.py --previous-sha dd71c2ba42a0744b5b654cedcf73009ce8879058 --candidate-sha e185b2cad0db6ed4a8197b36d72e2a72f7ab86c8 --output data/step53-run4
```

Runner извлекает два дерева из точных Git blobs, создаёт отдельные venv и
применяет только SQL кандидата к одноразовому previous export. Он запускает
147 неизменных старых tests и внешние
[`repository_compatibility_cases.py`](../../scripts/repository_compatibility_cases.py)
после настройки isolated `sys.path`. Их imports приложения, `document_chunker`
и старых test helpers разрешаются только из previous checkout. Отчёт содержит
SHA256 внешнего файла, test IDs и фактические module origins. После работы
сверяются bytes обоих checkout.

## Проверить поведение и обмен

Дополнительные tests используют отдельные временные БД, реальный
`apply_migrations()` на SQL кандидата и включённые FK/CHECK. Каждый сценарий
сверяет candidate journal до операций и `integrity_check`/`foreign_key_check`
после. Матрица указывает, где прямой assertion уже был в старом release test,
а где добавлен отдельный. Число вызовов repository не считается покрытием.

Сценарии проверяют owner и чужие IDs, duplicate/not-found, revision/CAS и
неизменность loser, lease owner/expiry/release, атомарность
`GitHubAccountAccessWriter` и `complete_applied` с fault trigger, каскад
vault/installation/note/chunk и отсутствие устаревшей FTS выдачи после
обновления и удаления. Fault triggers существуют только во временной БД
соответствующего теста и отмечены как fault injection. Ограничения SQLite
не отключаются.

Upgrade fixture создаётся **прежними** API на прежней схеме: core user,
identity, preference, chat mode; Obsidian article, analysis, incoming message,
GitHub account/installation, vault, note и instruction. Candidate migrator
проверяет смысловые поля и связи после upgrade; candidate API пишет новые
core/Obsidian строки. Previous API читает их, меняет имя, identity,
preference, chat mode, article, note и instruction. Финальный candidate
subprocess читает результат старой записи и проверяет смысл, журнал,
integrity и FK. Процессы обмениваются только временной SQLite.

На настоящем SQL неизвестный `articles.status` разрешён без CHECK: test
подтверждает `UNKNOWN`, сохранность raw значения и отказ old API записывать
неизвестный статус. `obsidian_proposals.status` имеет CHECK и отвергает
незнакомое значение. Синтетические UNKNOWN tests прежнего release остаются
отдельными unit cases, не доказательством записи на реальной схеме.

## Зафиксировать результат и пределы

Локальный прогон `data/step53-run4` завершился с exit code 0. Candidate
fresh/upgrade и 9 тестов мигратора прошли; прежний код выполнил 147
неизменных и 26 дополнительных тестов без ошибок и пропусков, с 179
наблюдениями схемы кандидата. Финальный candidate subprocess подтвердил
записи старого API. Все 107 строк [матрицы](STAGE5_MATRIX.md) ссылаются на
выполненные test IDs; SHA256 трёх файлов harness совпадают с `report.json`,
оба source manifest остались неизменными.

Это проверено по `report.json`, `candidate.json`, `behavior.json`,
`verify.json` и журналам. Дополнительно локально прошли 26 тестов
совместимости и 10 тестов runner из проектного venv.
Candidate SQL этой пары совпадает с previous SQL: прогон подтверждает
механизм с настоящими FK/CHECK/FTS, но не предсказывает будущую миграцию.
Обмен включает данные обоих модулей, но не каждую таблицу; операции прочих
adapters проверяются отдельными временными БД по матрице. Доверенная
baseline policy, CI run/attempt, неизменность доставленного evidence и
predeploy mismatch ещё не реализованы. POSIX-ветка process lock требует
проверки в Linux CI; локальный прогон проведён на Windows.
