# Этап 2.1: постоянный отчёт CI

Этот документ описывает, как получить короткий отчёт длительностей GitHub Actions для конкретного SHA и попытки. Правила допуска выпуска остаются в [RELEASES.md](../RELEASES.md); общий план и исходные замеры — в [PLAN.md](PLAN.md#2-оставить-постоянные-метрики-и-короткий-отчёт) и [CI_BASELINE.md](CI_BASELINE.md).

## Найти отчёт после запуска

В `pr-fast`, `main-full` и `release-full` job `ci-metrics` выполняется после проверочных jobs с `if: always()`. Откройте его Step Summary или artifact `ci-metrics-<run_id>-<attempt>` у нужного run. Artifact содержит `ci-metrics.json` и `ci-metrics.md`, хранится 90 дней, в том числе при отказе полного набора. В случае ошибки API дополнительно сохраняются `ci-metrics-error.json` и краткая причина в Markdown; job становится красным. Проверочный отказ не маскируется успешным отчётом.

Отчёт закрепляет repository, workflow, ветку, точный `head_sha` run, отдельно `checkout_sha` события и SHA каждого job, `run_id` и `run_attempt`. Длительности job и step считаются только из их собственных `started_at`/`completed_at`. Длительность проверок — от `run_started_at` до последнего завершённого проверочного job. Пока `ci-metrics` работает, GitHub run ещё не завершён, поэтому окончательный `updated_at` и conclusion всего run следует смотреть на странице GitHub после закрытия отчётного job. Пустая граница отображается как `unknown`. У skipped jobs/steps длительность тоже `unknown`: GitHub иногда возвращает их конец раньше начала. Jobs, завершённые до начала текущей attempt, отмечаются `carried_over` (результат прежней попытки); их конец не увеличивает длительность текущих проверок.

Типы `pr-fast`, `full-main`, `full-release` и `skipped` различаются явно. `skipped` означает пропущенный `full-suite`, даже если controller завершился успешно. Отчёт сравнивает длительность только с ближайшим завершённым run того же workflow, ветки, события, типа и набора перенесённых jobs среди последних 30; более новые run при rerun старого запуска исключаются. Если сопоставимый run отсутствует, сравнение не показывается. Из доступных кандидатов выбирается ближайший предыдущий run. Сравнение служит наблюдению, **не является допуском** точного SHA. Скрипт допуска по-прежнему отдельно сверяет обязательные jobs и steps.

## Разобрать отказ сбора

При HTTP/сетевой ошибке или повреждённом ответе API `ci-metrics` завершится ошибкой и сохранит безопасную короткую причину без token, URL запроса и тела ответа. Проверьте права `actions: read`, страницу указанного run/attempt и повторите сбор после восстановления API. Не трактуйте отсутствие отчёта как успешный полный CI. Короткая история лежит в artifacts отдельных попыток и хранится до 90 дней. Автоматическое сравнение читает REST историю runs/jobs, а не artifacts. Следует отдельно перенести важные наблюдения в рабочую запись выпуска.

Для PR отчётный job берёт скрипт из точного SHA базовой ветки PR (`pull_request.base.sha`), а не из проверяемого PR. Если collector ещё отсутствует в базе первого PR или старой релизной линии, шаг сбора пропускается: Summary и `ci-metrics-unavailable.json` сохраняют `result=unavailable`, причину `collector_missing_in_base`, run/attempt, head SHA и SHA базы. Само отсутствие collector не делает job красным, но и не подтверждает сбор метрик. Код PR вместо доверенной базы не исполняется. Ошибки checkout, Python, существующего collector или artifact остаются ошибками. При отмене всего run, потере runner или недоступности artifact service сохранение не гарантируется.

Недоступность истории для сравнения не скрывает уже собранный текущий отчёт: Markdown показывает измерения и отдельную строку «Сравнение недоступно». Ошибка получения текущей попытки по-прежнему завершает сбор отказом. Для сравнения дополнительно должны совпадать результат и имена/результаты jobs: короткий failed run не показывается как ускорение успешного.

Collector начинает работу после проверок; живое предупреждение реализовано отдельно обёрткой `scripts/ci_watch.py`. Каждые 60 секунд незавершённой команды она печатает `::warning::CI stage=... result=waiting seconds=... reason=long_stage`. В `pr-fast` обёрнуты установка зависимостей, три unit-команды и проверка Compose; в `full-suite` — зависимости, полный unittest, каждый из трёх smoke, проверка схем и build/healthcheck. Интервал отсчитывается отдельно для каждой команды, а не суммируется по всем командам шага. Checkout, setup actions, контроллеры и token-bearing hotfix-проверки этой обёрткой не охвачены.

Обёртка использует только стандартную библиотеку, поэтому работает до установки зависимостей. Она не вводит deadline, не меняет job timeout, ids проверок или их код завершения; сигналы отмены runner пересылаются дочерней команде, отдельная process group не создаётся. Сообщение содержит фиксированную метку и монотонную длительность: argv, env и содержимое вывода в него не копируются. Обычные stdout/stderr команды наследуются и остаются её прежним логом. Ошибка записи предупреждения не прерывает команду. Предупреждение deploy реализовано отдельно в [STAGE2_DEPLOY.md](STAGE2_DEPLOY.md); итог проверки — [STAGE2_REVIEW.md](STAGE2_REVIEW.md).

Локальный subprocess-тест подтверждает появление предупреждения до завершения команды и сохранение её failure code. Фактическая видимость warning в GitHub live log/annotation и отмена на Linux runner ещё требуют нового запуска; это не доказательство уже завершённой эксплуатационной приёмки.


## Проверено 01.11.2026

Локальные тесты проверяют failure suite, ошибку API с безопасной причиной,
раздельные SHA PR/job, чужую попытку, skipped, перенесённые результаты rerun,
сравнение типов и подключение always jobs в трёх workflow.
Read-only REST запросы к attempt endpoints и jobs подтвердили расчёты:

| Run/attempt | Тип | Проверки, секунды |
| --- | --- | ---: |
| [41005120556/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41005120556/attempts/1) | pr-fast | 30 |
| [41000379721/2](https://github.com/karateka-git/compadre-assistant/actions/runs/41000379721/attempts/2) | pr-fast, pr-fast job перенесён из прежней попытки | 12 |
| [41005193599/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41005193599/attempts/1) | skipped | 10 |
| [41005012029/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41005012029/attempts/1) | full-main | 106 |
| [41000609812/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41000609812/attempts/1) | full-release | 113 |

В проверенной выборке PR API `job.head_sha` совпадает с `run.head_sha`.
Скрипт не считает это обязательным контрактом для PR: идентичность подтверждают
endpoint attempt и поля job `run_id/run_attempt`, а оба SHA сохраняются отдельно.
`checkout_sha` берётся из `GITHUB_SHA` события (тестовый merge PR), а не из
checkout коллектора. Для main/release равенство SHA job/run обязательно.
Skipped reusable job в живом API называется `main-full`, без `/ full-suite`.
Эти замеры не включают время нового отчётного job. Полноценный отчёт новым
workflow ещё требует живой проверки; локальные тесты не заменяют
эксплуатационную приёмку.

01.11.2026 новый [PR fast run 41315349925/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41315349925/attempts/1)
для draft [PR #59](https://github.com/karateka-git/compadre-assistant/pull/59),
head SHA `7a01407a563ceee339f47a8f6f15ed98bba75a11`, завершился успешно.
Artifact `ci-metrics-41315349925-1` содержит проверенный
`ci-metrics-unavailable.json` с `result=unavailable`,
`reason=collector_missing_in_base`, run/attempt, head SHA и base SHA.
Подтверждён bootstrap при отсутствии collector в доверенной базе.
Полный PR metrics report остаётся непроверенным до запуска с collector
в base SHA; остальные границы проверки — в [STAGE2_REVIEW.md](STAGE2_REVIEW.md#живая-проверка-pr-bootstrap).
