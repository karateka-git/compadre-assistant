# Этап 3, шаг 3: события полного CI

Реализация и проба GitHub выполнены 2026-10-14. Рабочий документ временный; после
приёмки этапа перенести действующие правила в постоянную документацию по
матрице [PLAN.md](PLAN.md).

## `main-full`

Workflow запускается на `push` в `main` и вручную через `workflow_dispatch`
только с выбранной веткой `main`. Лёгкий job `main-full-controller` получает
полную первую родительскую линию точного SHA события. Неизменяемая точка
отсчёта — **первый commit в этой линии, в дереве которого появился
`.github/workflows/main-full.yml`**. При обычном PR merge этот commit есть
активационный merge в `main`, поэтому он даёт счёт 0. Его полный SHA
контроллер пишет в log; после слияния реализации его следует записать в
постоянную документацию. Привязка к Git-истории не зависит от времени запуска
или изменяемого имени ветки.

Далее controller идёт от текущего SHA назад до bootstrap commit или последнего
полного успешного `main-full`. Merge commit считается PR только при двух
родителях и подтверждении GitHub API: `merged_at`, `merge_commit_sha` равен
commit и base равен `main`. Побочные merge commits и commits до активации не
считаются. После третьего PR запускается reusable `full-suite`. Ручной запуск
до порога запускает тот же набор; если SHA уже успешно проверен, остаётся
только лёгкий controller. Успех controller без job `full-suite` не сбрасывает
счётчик. Для повторного использования controller проверяет точный SHA, имя
ветки, успех run, успех reusable job и каждого обязательного шага. Новая
попытка после уже начатого, но неуспешного полного прогона допускается через
GitHub **Re-run jobs** того же run, а не новый dispatch.

`concurrency` по `main` отменяет незавершённый прежний run при новом push
или dispatch. Controller сверяет SHA события с текущим ref и отказывает при
гонке. Уже завершившийся успешный run сохраняет результат своего SHA.

## `release-full`

Workflow запускается на `push` в `release/*`, но принимает только имена
`release/X.Y`. На событии создания ветки (`created=true`) он проверяет
успешный `main-full` точного SHA с полным reusable job и пропускает повтор
набора. Создание ветки от непроверенного SHA завершается ошибкой. При
изменении существующей ветки выполняется `full-suite`; уже успешно
проверенный тот же SHA повторно не запускается. `concurrency` действует
отдельно для каждой релизной ветки, отменяя её устаревший run.

## Предыдущая схема и пределы

Оба caller передают `previous-release-sha` как полный SHA commit. До первого
опубликованного выпуска это зафиксированный production commit
`ac2ac981ba628a881b483238a4eaf191a644de6a`. Затем controller читает
опубликованные нечерновые SemVer GitHub Releases, разрешает ссылку на tag
через GitHub Git API до commit. Для `main`
берётся последний по `published_at` выпуск; для существующей релизной линии —
последний выпуск той же `X.Y`, для новой линии — последний выпуск вообще. Если тег
ведёт в другую Git-линию после PATCH релизной ветки, SHA всё равно передаётся
в `full-suite`: его миграции обязаны быть точным префиксом миграций candidate
по именам и содержимому SQL. При различии префикса полный job завершится
ошибкой. Это проверяет схему реально последнего опубликованного выпуска, а не
только старого общего предка веток. До первого опубликованного выпуска
bootstrap production SHA по-прежнему должен быть предком candidate. Ошибка
API, неоднозначное время публикации, неправильный тег или недоступный commit
также закрывают проверку ошибкой.

Caller использует `actions:read`, `contents:read`; `main-full` дополнительно
`pull-requests:read`. Reusable job ограничен `contents:read` и не получает
production secrets. Нет `workflow_run`, `repository_dispatch`, write token
или внешнего trigger; оба caller запускаются событиями своего репозитория.
Отказ GitHub API закрывает controller ошибкой. Успех CI как допуск к публикации
или deploy будет независимо проверяться selector на шаге 4; здесь selector
VPS не создаётся.

## Локальная и фактическая проверка GitHub

Локальные fixtures проверяют порог трёх, игнорирование побочного merge,
отличие лёгкого run от полного и обязательность всех шагов job, bootstrap
SHA и отказ для несовместимой линии выпуска:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_full_ci_controller -v
```

Ветка `codex/auto-deploy-production-main` отправлена по SSH с разрешения
пользователя. HTTPS push был отклонён GitHub из-за отсутствия `workflow`
scope у OAuth token; SSH push с существующим ключом прошёл без изменения
настроек доступа.

Для пробы создана только временная `release/0.0` от SHA
`c266347658544c2275825988d778dcc8f6805115`. Её первый
[run 38604417209](https://github.com/karateka-git/compadre-assistant/actions/runs/38604417209)
получил `BRANCH_CREATED=true` и завершился с ошибкой до полного набора:
`main-full` не зарегистрирован на default branch, поэтому API вернул 404.
Это ожидаемый отказ для ветки, созданной без успешного `main-full` её SHA.

Пробный [PR #8](https://github.com/karateka-git/compadre-assistant/pull/8)
в эту ветку прошёл
[pr-fast](https://github.com/karateka-git/compadre-assistant/actions/runs/38604633951)
и слит способом merge в SHA
`15e6800b10f08155e8efca4a2dd4b5f8ea58358a`. Его
[release-full run 38604745834](https://github.com/karateka-git/compadre-assistant/actions/runs/38604745834)
завершился `success`: `release-full-controller`, reusable
`release-full / full-suite`, все обязательные шаги включая миграции,
Docker build и candidate healthcheck завершились успешно. В ответе GitHub
`run.path` равен `.github/workflows/release-full.yml` без `@ref`; selector
исправлен для этой реальной формы. При изменении существующей release-ветки
controller теперь не запрашивает недоступный `main-full`: новый merge SHA
не мог иметь полного результата `main` при принятой политике merge commit.
После пробы временная `release/0.0` удалена; PR #8 остался слитым как
историческое свидетельство проверки. `main` и реальные `release/*` не менялись.

Автоматический счёт трёх PR в `main`, ручной `main-full`, повторное
использование его результата при создании настоящей релизной ветки и
фактическая отмена устаревших runs остаются pending до слияния caller в
`main` и соответствующих событий. Для проверки отмены не создавали
лишний второй полный прогон.
