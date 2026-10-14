# Этап 3, шаг 4: допуск по точному результату полного CI

Реализовано локально 2026-10-14. Документ временный; действующий контракт
перенести в постоянную документацию перед закрытием этапа.

## Read-only selector

`scripts/full_ci_admission.py` предоставляет
`select_successful_run(api, repository, workflow, sha, branch)`. Вызывающий
код явно задаёт ожидаемые `owner/repo`, caller `main-full.yml` либо
`release-full.yml`, полный 40-символьный SHA и ветку. Результат — неизменяемая
запись с repo, workflow, веткой, SHA, run ID и номером попытки либо `None`.
`None` запрещает выпуск/deploy. Исключение при ошибке API, некорректной
структуре ответа или неверном запросе также запрещает допуск; вызывающий код
не должен превращать исключение в разрешение.

Selector проверяет metadata workflow (`id` и путь), затем для каждого run на
точном SHA повторно сверяет repo и head repo, workflow ID, путь с ref ветки,
имя ветки, тип события, завершённый статус и `success`. Допускаются только
`push` и `workflow_dispatch`. Jobs запрашиваются через endpoint **конкретной
текущей попытки** `runs/{id}/attempts/{run_attempt}/jobs`, поэтому успех старой
попытки rerun не примешивается к новой. Каждый полученный job и каждый его
шаг должен быть завершён с `success`. Обязательны controller и reusable
`full-suite`, а также все закреплённые шаги обоих jobs. `skipped`, `cancelled`,
`queued`, `failure`, отсутствующий job/step, краткий SHA и неверная ветка не
дают допуска. Лишний неуспешный job также закрывает допуск.

При создании `release/X.Y` от того же SHA controller вызывает общий selector
для `main-full.yml` с веткой `main`; успешный результат повторно используется.
Для изменённого SHA релизной ветки требуется `release-full.yml`. Проверка
текущего tip ветки и происхождения тега остаётся обязанностью вызывающего
сценария: selector удостоверяет исторический CI конкретного commit, что
необходимо и для опубликованной версии, чей SHA позже перестанет быть tip.
Публикация версии и deploy в этом шаге не реализованы.

## Повтор и отмена устаревшего запуска

Caller workflows оставляют `concurrency` с `cancel-in-progress: true` по ref.
Controller дополнительно сверяет локальный checkout с `GITHUB_SHA`, а ref
GitHub с SHA события. Если ветка ушла вперёд, старый run завершается ошибкой
до полного набора. Отдельный новый dispatch не заменяет предыдущую полную
попытку того же SHA: ожидающий run и завершившийся неуспешный full job,
включая job без начатых steps, требуют явного GitHub **Re-run jobs**.
Лёгкий run со `skipped` full-suite не считается полной попыткой и допускает
ручной запуск до порога трёх PR. Rerun того же run проходит проверку, но
selector впоследствии читает только его текущую попытку.

## Проверка и ограничение

Локальные fixtures покрывают точный SHA, repo, workflow, ref, обязательные
jobs/steps, release reuse, rerun, queued/cancelled/skipped/failure,
некорректный API, старый tip и повторный dispatch. На фактическом
[release-full run 38604745834](https://github.com/karateka-git/compadre-assistant/actions/runs/38604745834)
selector вернул `Admission` для SHA
`15e6800b10f08155e8efca4a2dd4b5f8ea58358a`, ветки `release/0.0`,
workflow `release-full.yml`, run ID `38604745834`, attempt `1`. Неверные
ветка `release/0.1` и SHA `000…000` вернули `None`. После удаления временной
ветки исторический run того же SHA по-прежнему читается; проверка актуального
ref и тега относится к будущему сценарию выпуска. GitHub возвращает
`run.path` без `@ref`, что учтено после пробы.

Selector для `main-full` и реальный rerun/отмена concurrency остаются
непроверенными до появления caller в `main` и соответствующих событий.
Публикация выпуска и production deploy на этой пробе не выполнялись.
