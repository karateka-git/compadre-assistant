# Этап 3, шаг 2: общий полный набор проверок

Реализовано 2026-10-14 в `codex/auto-deploy-production-main`.
Документ временный; перед закрытием этапа перенести действующие правила в
постоянную документацию по матрице [PLAN.md](PLAN.md).

## Контракт reusable workflow

`.github/workflows/full-suite.yml` вызывается через `workflow_call` и
принимает обязательный `previous-release-sha`: полный SHA commit предыдущего
опубликованного выпуска. До первого `v1.0.0` исходным выпуском для теста служит
фактически развёрнутый production commit
`ac2ac981ba628a881b483238a4eaf191a644de6a`. Он содержит ту же применённую
`0001_initial_schema.sql`; это bootstrap-точка, а не версия или тег задним
числом. После первого выпуска вызывающий workflow должен передавать SHA
последнего опубликованного выпуска: для `main` и новой релизной линии —
последнего вообще, для существующей линии `release/X.Y` — последнего в этой
линии. Выбор по тегу и `published_at` описан в
[FULL_CI_TRIGGERS.md](FULL_CI_TRIGGERS.md). Опубликованный выпуск может
находиться в соседней Git-ветке: проверка требует точного префикса и
содержимого прежних SQL, а не отношения Git ancestry. Отсутствующий либо
неверный SHA завершает проверку ошибкой.

Workflow делает checkout точного `github.sha`, проверяет его, использует
`contents: read`, `persist-credentials: false`, закреплённые SHA checkout и
setup-python. Он запускает полный `unittest`, три автономных smoke,
`scripts/check_full_migrations.py`, затем сборку Docker candidate и
`scripts/check_candidate.py`. Последний шаг всегда пишет JSON-манифест в
`RUNNER_TEMP` и таблицу в GitHub job summary: repository, ref, SHA, предыдущий
SHA, workflow ref, run ID/attempt и результат каждого обязательного шага.
`success` в манифесте возможен только при успехе всех перечисленных шагов;
сам GitHub job завершается ошибкой при провале обязательной проверки.
Постоянная публикация artifact пока не нужна для допуска: итог и SHA доступны
в run/jobs API, а summary служит диагностикой. GitHub API selector этапа 3,
шага 4 здесь не реализован.

`main-full` и `release-full` на шаге 3 будут вызывать этот же workflow.
События, счётчик трёх PR, отмена устаревших запусков и автоматический выбор
предыдущего выпуска в этом шаге отсутствуют. Reusable workflow сам по себе
не запускается при push/PR. Его caller должен передать предыдущий SHA и
выбрать точный commit; проверить его происхождение и допуск предстоит
следующим шагам.

## Искусственные БД и образ

Миграционная проверка читает прежние SQL непосредственно из Git-дерева
`previous-release-sha` и сверяет их имена, порядок и содержимое с текущими
файлами. На временной **новой** SQLite применяется вся текущая цепочка.
На временной **прежней** SQLite применяется прежняя цепочка, добавляется
строка `app_users`, затем применяются ожидающие текущие миграции. Для обеих
проверяются точный `schema_migrations`, `integrity_check`, `foreign_key_check`
и повторное применение без изменений. Сохранность строки проверяется до и
после. Код из архивированного предыдущего commit выполняет read/write этой
строки через прежний `SQLiteAppUserRepository` после upgrade. Это адресная
проверка совместимости прежнего кода с обновлённой схемой, а не доказательство
совместимости всех сценариев. Для каждого прогона дополнительно создаётся
только во временном каталоге synthetic следующая миграция с `ALTER TABLE` и
`UPDATE`: она проверяет реальный путь pending SQL и сохранение тестовой строки
даже пока в репозитории нет настоящей `0002`. Файл `0001` не меняется,
production `0002` не добавляется.

Candidate собирается из того же checkout в `compadre-assistant:ci-<SHA>`.
Контекст исключает `.env`, `data/`, `.git/` и `.venv/` по `.dockerignore`;
Dockerfile копирует только код и зависимости. Два `docker run --rm --network
none` на временно примонтированном пустом каталоге выполняют `--migrate`,
затем `--healthcheck --channels telegram,vk`. Передаются только фиктивные
Telegram/VK значения, пустой список возможностей и путь тестовой БД.
Healthcheck проверяет форму конфигурации и доступность SQLite без реальных
запросов к Telegram, VK, LLM или GitHub. Образ production не развёртывается.

## Локальный повтор

В виртуальном окружении с `requirements.txt`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m compadre_assistant --sqlite-smoke
.\.venv\Scripts\python.exe -m compadre_assistant --pipeline-smoke
.\.venv\Scripts\python.exe -m compadre_assistant --analysis-smoke
.\.venv\Scripts\python.exe -m scripts.check_full_migrations --previous-sha ac2ac981ba628a881b483238a4eaf191a644de6a
.\.venv\Scripts\python.exe -m scripts.check_candidate --expected-sha (git rev-parse HEAD)
```

Последняя команда требует работающего Docker Engine и чистого checkout
точного commit; незакоммиченные изменения в образ не считаются проверенным
SHA. При локальной проверке 2026-10-14 `unittest` (561 тест), три smoke,
миграции и `compileall` прошли. После запуска Docker Engine команда
`check_candidate` на чистом commit `eca4728e5caf6f3758245811bf33c8a5957929fd`
успешно собрала образ, выполнила `--migrate` и candidate healthcheck обоих
каналов на временной SQLite без сети. Image ID:
`sha256:9b466222b3fe45c575f9ad9191f2920971a81a12a377e394478d016537256b22`.
Ни production-секреты, ни пользовательская SQLite не применялись. Результат
GitHub runner всё равно нужен до допуска выпуска.

## Границы

- Прежний код проверяется на базовых чтении и записи профиля; новые SQL,
  затрагивающие другие сценарии, требуют дополнительных адресных проверок
  и плана возврата до deploy.
- Предыдущие миграции могут быть неизменны в Git, но фактическая production-БД
  могла отличаться из-за ручных операций. Перед первой реальной миграцией
  сравнить её схему с ожидаемой базой, как требует [BASELINE.md](BASELINE.md).
- Результат CI относится только к точному SHA. `skipped`, `cancelled`, частичный
  успех или недоступный API не создают допуска; реализация selector — шаг 4.
