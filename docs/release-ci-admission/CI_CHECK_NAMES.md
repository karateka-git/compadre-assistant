# Карта имён проверок CI

Карта задаёт имена для этапа 7 [плана](PLAN.md) и показывает, где их нужно
заменить перед переключением. В GitHub Actions есть отдельные имена workflow
и задач. GitHub проверяет при слиянии имя задачи, указанное в ruleset.
Сохраняем пути `.yml` и внутренние `job ID`, чтобы ссылки на файлы, зависимости
между задачами и идентичность workflow в API остались стабильными.

## Что переименовать

| Согласованное имя | Workflow и его видимое имя | `job ID` и видимое имя задачи | Где требуется |
| --- | --- | --- | --- |
| `pr-check` | `pr-fast.yml`, `PR fast` → `pr-check` | `pr-fast`, `pr-fast` → `pr-check` | Ruleset для `main` и `release/*`, снимок проверки PR для аварийного выпуска и ротации policy, отчёт CI |
| `pr-merge-main-check` | Тот же `pr-fast.yml` | `hotfix-deps`, `hotfix-deps` → `pr-merge-main-check` | Ruleset для `release/*`, поиск задачи для rerun |
| `pr-merge-main-rerun` | `hotfix-deps-rerun.yml`, `Hotfix dependency rerun` → `pr-merge-main-rerun` | `rerun`, `rerun` → `pr-merge-main-rerun` | Событие merge связанного PR в `main` |
| `pre-release-check-full` | `main-full.yml`, `Main full` → `pre-release-check-full` | `controller`, `main-full-controller` → `pre-release-check-full-controller`. Вызов общего workflow имеет ID `full-suite` и имя `main-full` → `pre-release-check-full`. | Допуск новой релизной линии, ротация policy, отчёт CI |
| `release-check-full` | `release-full.yml`, `Release full` → `release-check-full` | `controller`, `release-full-controller` → `release-check-full-controller`. Вызов общего workflow имеет ID `full-suite` и имя `release-full` → `release-check-full`. | Допуск PATCH существующей линии, отчёт CI |
| `check-full-repository-compatibility-check` | Общий `full-suite.yml` | `repository-compatibility`, `repository-compatibility` → `check-full-repository-compatibility-check` | Первый обязательный job полного CI, verifier и ротация |
| `check-full-suite` | `full-suite.yml`, `Full suite` → `check-full-suite` | `full-suite`, `full-suite` → `check-full-suite` | Второй обязательный job полного CI, контроллеры caller и отчёт CI |

GitHub API возвращает имена вызванных задач в форме «имя вызова / имя задачи».
В новом полном прогоне ожидаются `pre-release-check-full / check-full-suite`
и `pre-release-check-full / check-full-repository-compatibility-check` для
`main`. Для `release/*` у этих задач префикс `release-check-full`. Если
контроллер пропустил полный набор, метрики видят пропущенный вызов с одним
именем `pre-release-check-full` или `release-check-full`.

Имена `ci-metrics`, шагов, на которые опирается допуск, concurrency groups,
файлов доказательств и артефакта `repository-compatibility-<run>-<attempt>`
остаются идентификаторами своих контрактов. Выходной файл
`full-suite-manifest.json` тоже сохраняет имя.

## Что сверить в скриптах

- [Селектор полного CI](../../scripts/full_ci_admission.py) сравнивает
  имя контроллера и два составных имени задач. Для нового допуска проверить
  полный успешный набор, пропуски, ошибки и смесь прежних и новых имён,
  в том числе старые задачи рядом с полным новым набором. Сохранить проверки
  репозитория, пути, ветки, SHA, ID и номера попытки. Прежнее исключение
  `LEGACY_FULL_SHA` для проверки v3.1.0 остаётся в историческом коде.
  [База совместимости](../../scripts/ci_repository_compatibility.py)
  устанавливается независимо через Release, тег и SHA.
- [Контроллер полного CI](../../scripts/full_ci_controller.py) использует
  имена jobs для поиска выполненного прогона. Сохранённый путь
  `main-full.yml` оставляет исходную точку отсчёта `33ae77a`. После
  перехода счётчик не зачтёт прежние полные прогоны как новый успешный
  результат. Подтвердить новый полный прогон точного SHA.
- [Rerun](../../scripts/rerun_hotfix_deps.py) находит текущий run по тому же
  пути `pr-fast.yml` и ищет в нём `pr-merge-main-check`. Сохранить проверку
  связи PR и привязку к актуальным head и base. Для открытого релизного PR
  нужен свежий прогон с новым именем до автоматического rerun.
- [Метрики](../../scripts/ci_metrics.py) различают `pr-check`, выполненные
  и пропущенные полные прогоны по именам задач API. Классификацию
  `full-main`, `full-release`, `skipped` и формат отчёта сохранить.
  PR workflow запускает доверенный collector из `pull_request.base.sha`.
  Пока в базе старый collector, на PR перехода возможен отчёт об ошибке
  сбора. После слияния проверить новый отчёт на PR с обновлённой базой.
- [Обычная публикация](../../scripts/publish_release.py), [создание линии](../../scripts/create_release_branch.py)
  и [проверка PATCH](../../scripts/check_hotfix_pairs.py) используют общий
  селектор полного CI. Обычный [манифест Release](../../scripts/select_deploy_release.py)
  закрепляет ссылку на точный run/attempt. Аварийный манифест содержит путь
  caller и состояние проверки PR на момент merge. У
  [публикатора](../../scripts/publish_emergency_release.py) и читателя
  аварийного манифеста должны совпадать новое имя поля и проверка job.
  [Ротация базы CI](../../scripts/rotate_ci_baseline.py) использует тот же
  снимок проверки PR для подтверждения reviewed merge policy.
- [Verifier VPS](../../scripts/predeploy_compatibility.py) в аварийном
  пути отдельно ищет точное составное имя задачи совместимости. Для обычного
  и аварийного пути сохраняется сверка байтов `main-full.yml`,
  `release-full.yml` и `full-suite.yml` с reviewed pin. Переименование
  меняет эти байты. Перед новым выпуском нужен pin, где все три файла уже
  совпадают с кандидатом, и controller snapshot с новым селектором.
  [Порядок ротации](../OPERATIONS.md#закрепить-базу-полного-ci-после-установки)

## Снимок GitHub от 10.11.2026

При чтении через `gh api` открытых PR было ноль. Активные правила требуют
`pr-fast` на `main` и `release/*` и `hotfix-deps` на `release/*`.
Для первого правила предусмотрен обход владельцем через PR. У второго
правила список обходов пуст. При смене обязательных статусов сохраняются
остальные параметры обоих правил.
[Правила веток](https://github.com/karateka-git/compadre-assistant/rulesets)

Под шаблон `release/*` попали `release/1.0`, `release/2.0`, `release/3.0`,
`release/3.1` и `release/3.2`. Их проверенные Git-объекты содержат старые
имена PR jobs. В `release/1.0` и `release/2.0` вершины на один merge
опережают последние опубликованные версии `v1.0.4` и `v2.0.0`.
Остальные три линии совпадают с последними тегами своих выпусков.
До переключения определить область действующих релизных линий и проверить
готовность каждой ветки в этой области. Если применить новые контексты
ко всему шаблону, PR из веток со старым workflow останутся заблокированы
до появления новых обязательных задач.

Полный прогон `main` на `7502764` содержит оба обязательных reusable jobs.
Внешняя переменная `COMPATIBILITY_POLICY_SHA` указывала на `09f15a5`.
Проверенный прогон создания `release/3.2` содержит только контроллер,
метрики и пропущенный вызов полного набора. Перед сменой правил заново
проверить открытые PR, правила, новые имена на реальных попытках для `main`
и `release/*`, а также rerun на связанной паре. Перед активацией CI pin
повторно подтвердить GitHub variable, принятую версию и отсутствие recovery
на VPS. При снятии снимка production не проверялся.
[Полный main](https://github.com/karateka-git/compadre-assistant/actions/runs/42347774532/attempts/1) ·
[Создание release/3.2](https://github.com/karateka-git/compadre-assistant/actions/runs/42266727750/attempts/1)
