# 7.6.1 — инвентаризация перед переносом документации

Проверено в отдельном worktree от `origin/main` 20.10.2026. Это рабочая карта,
а не постоянная инструкция. Источник требований — [PLAN.md](PLAN.md), раздел
«Перенос временных документов после проверки всей фичи». Сверены все 26 файлов
каталога, постоянные документы, workflows и основные скрипты. Состояние GitHub
rulesets и VPS в этом шаге **не перечитывалось**; не выдавать эту сверку за
новую эксплуатационную проверку.

## Что оставить в постоянной документации

| Действующий смысл и временный источник | Постоянное место | Сейчас и необходимая правка |
| --- | --- | --- |
| Отдельные ветка и worktree чата; временный каталог на **всю функциональность**, проверка переноса и удаление только по её завершении. [PLAN](PLAN.md), [BASELINE](BASELINE.md) | `AGENTS.md` | Ветка/worktree уже есть. Формулировка «до завершения этапа» и удаление «до слияния» противоречат принятому для этой фичи порядку. Согласовать общую формулировку с пользователем, не переносить исключение как безусловное правило для всех задач. Запрет push, русские коммиты и production-БД уже описаны. |
| Простая схема PR → `main` → `release/X.Y` → версия → **ручная** команда; тег может опережать VPS. [PLAN](PLAN.md), [DEPLOY_SELECTOR](DEPLOY_SELECTOR.md) | `docs/RELEASES.md`, кратко `README.md` | `RELEASES` хорошо объясняет тег/SHA/VPS, но не содержит цельной схемы разработки. В README нет ссылки на `RELEASES`. Не дублировать подробный runbook. |
| Защита веток, merge commit, два required checks release PR, ссылки `Hotfix-Links`/`Hotfix-Main-Commit`, независимость релизных линий, fan-out rerun и auto-merge. [BRANCH_RULES](BRANCH_RULES.md), [MERGE_POLICY](MERGE_POLICY.md), [HOTFIX_PAIRS](HOTFIX_PAIRS.md), [STEP7_5_EMERGENCY_ACCEPTANCE](STEP7_5_EMERGENCY_ACCEPTANCE.md) | `docs/RELEASES.md` | Алгоритм в основном есть. Удалить историческую простыню проб #19–#27 и устаревшее «новый fan-out требует проверки шага 7.5»; описать принятый результат #34–#35 кратко в `HISTORY`. Удалить ссылку на временный `HOTFIX_PAIRS`. Реальные правила GitHub при необходимости сверить отдельно. Код: `.github/workflows/pr-fast.yml`, `.github/workflows/hotfix-deps-rerun.yml`, `scripts/check_hotfix_pairs.py`, `scripts/rerun_hotfix_deps.py`. |
| SemVer, последовательный PATCH, следующая MINOR/MAJOR, неизменные защищённые теги/Release, публикация через `--check`, `--dry-run`, `--publish`, частичный отказ между тегом и Release. [PUBLISH_RELEASE](PUBLISH_RELEASE.md), [PLAN](PLAN.md) | `docs/RELEASES.md` — правила; `docs/OPERATIONS.md` — команды | В `RELEASES` почти нет правил версий или команд публикации. Устаревшее требование в `PUBLISH_RELEASE` о слиянии в **все** старшие линии не переносить: линии независимы (шаг 7.5). Сверять `scripts/publish_release.py`/`scripts/github_publishing.py`. Историю отдельных версий держать в GitHub Releases и `HISTORY`, не копировать длинный список в правила. |
| `pr-fast`: события, короткие проверки, повтор при новом коммите/изменении описания; изменение базы само по себе не вызывает новый run; merge commit и GitHub mergeability. [PR_FAST](PR_FAST.md), [MERGE_POLICY](MERGE_POLICY.md) | `docs/RELEASES.md`; локальные команды `docs/RUN.md` | Нынешний `RELEASES` не объясняет быстрый CI. Точный состав брать из `.github/workflows/pr-fast.yml`: syntax, tracked files/миграции, пары PR, фиксированный unit subset, synthetic Compose. Не писать «полный прогон» для PR. Настройка required checks должна соответствовать фактическому ruleset. |
| `main-full`: каждые 3 подтверждённых merge PR либо ручной запуск перед релизом; `release-full` на новых SHA; один reusable full-suite, reuse полного успеха того же SHA. [FULL_CI](FULL_CI.md), [FULL_CI_TRIGGERS](FULL_CI_TRIGGERS.md), [FULL_CI_ADMISSION](FULL_CI_ADMISSION.md) | `docs/RELEASES.md`; `docs/RUN.md` для локального аналога | Этого в постоянной документации почти нет. Сверить `.github/workflows/{main-full,release-full,full-suite}.yml`, `scripts/full_ci_controller.py` и реальную точку отсчёта счётчика. Controller-only success без `full-suite` не является допуском. Никаких production secrets/данных в CI. |
| Допуск по exact repo/workflow/ref/SHA/run/attempt/jobs/steps, отказ при pending/skipped/ошибке API; разница между новым dispatch и rerun. [FULL_CI_ADMISSION](FULL_CI_ADMISSION.md), [DEPLOY_SELECTOR](DEPLOY_SELECTOR.md) | `docs/RELEASES.md` — правило; `docs/OPERATIONS.md` — диагностика | Сейчас упоминается только «полный CI точного SHA». Добавить краткий проверяемый контракт, не переписывая каждое условие Python построчно. Источник: `scripts/full_ci_admission.py`, `scripts/select_deploy_release.py`. |
| Создание `release/X.Y` от проверенного `main` SHA, PATCH в старой линии, включение фикса в `main`, уникальная версия. [RELEASE_BRANCH](RELEASE_BRANCH.md), [PUBLISH_RELEASE](PUBLISH_RELEASE.md) | `docs/RELEASES.md`; команды `docs/OPERATIONS.md` | Объяснение старых/новых линий есть лишь частично. Указать `scripts/create_release_branch.py` и обязательную проверку tip перед записью. Не повторять весь опыт пробной `release/0.0`. |
| Активная и 3 прежние линии, дополнительные удерживающие причины, предварительная инвентаризация; **фактическая команда удаления ещё не реализована** из-за гонки GitHub REST delete. [STEP7_2_RETENTION](STEP7_2_RETENTION.md) | `docs/RELEASES.md` — политика; `docs/OPERATIONS.md` — ограничения/планирование | В постоянных документах правила нет. Нельзя обещать автоматическую очистку или готовую команду execute: `scripts/release_branch_retention.py` строит и повторно валидирует план, а удаление требует отдельной безопасной процедуры и разрешения push/эквивалентной записи. Теги и образы этим правилом не удаляются. |
| Обычный `deploy-prod`: immutable Release и тег, SHA из релизной линии (необязательно tip), ручной выбор версии, preflight, образ, два writer, backup, миграции, health, state и rollback. [DEPLOY_SELECTOR](DEPLOY_SELECTOR.md), [STAGE5_STEP2](STAGE5_STEP2.md), [STAGE5_STEP3](STAGE5_STEP3.md), [STAGE5_STEP4](STAGE5_STEP4.md) | `docs/RELEASES.md` — допуск; `docs/OPERATIONS.md` — процедура; `docs/ARCHITECTURE.md` — 2–3 предложения о границе контроллера | `OPERATIONS` содержит краткий happy path и systemd, но recovery отсылает к временной папке. Перенести таблицу действий по `failed_preflight`/`preparing`/`ready`/`rollback_started`/`rolled_back`/`degraded`/`success`, marker и SQLite-совместимости, без исторических shell-скриптов первого окна. Сверить `scripts/deploy_prod.py`, `deploy/`. |
| Отдельная миграционная CLI `python -m compadre_assistant --migrate`, изолированная БД, совместимость старой/новой схемы и предел возврата. [MIGRATIONS](MIGRATIONS.md), [FULL_CI](FULL_CI.md), [STAGE5_STEP4](STAGE5_STEP4.md) | `docs/RUN.md` — локальный вызов; `docs/OPERATIONS.md` — production-порядок; `docs/RELEASES.md` — граница CI | В `RUN` подробная development-БД, но отдельная CLI не описана. Production-БД нельзя пересоздавать по временному разрешению из `AGENTS`. Не обещать автоматический rollback после несовместимых `0002+`: текущий контроллер fail closed. |
| `systemd` service/drop-in и root-only pinned Compose; точные проверки `ExecStartEx`/`ExecStopEx`, `no-setuid`, права 0700/0600. [STEP7_5_SYSTEMD_CLEANUP](STEP7_5_SYSTEMD_CLEANUP.md), [STAGE5_STEP4](STAGE5_STEP4.md) | `docs/OPERATIONS.md`; фактический статус `docs/PRODUCTION.md` | Операционные сведения уже есть, но есть устаревшие фразы «90 ещё может быть установлен» и ссылка на временный runbook. После подтверждённого удаления 20.10.2026 оставить только действующий порядок `channels.conf` → `zz-deploy-pinned.conf`; краткий факт очистки — в `HISTORY`/`PRODUCTION`. Не переносить maintenance bundle и полную хронологию отказов в общую инструкцию. |
| Аварийный PR-only bypass владельца, обычный и emergency Release, причина, состояния `pr-fast`/full CI, ручной `deploy-prod --emergency`; сохраняются GitHub/SHA/backup/rollback проверки. [EMERGENCY_BYPASS](EMERGENCY_BYPASS.md), [EMERGENCY_PUBLISH](EMERGENCY_PUBLISH.md), [EMERGENCY_DEPLOY](EMERGENCY_DEPLOY.md), [STEP6_4_VERIFICATION](STEP6_4_VERIFICATION.md) | `docs/RELEASES.md` — политика; `docs/OPERATIONS.md` — команды и отказ | Постоянный маршрут отсутствует. Реальный emergency Release/VPS **не испытывались**; явно обозначить эту границу. Не переносить ложное требование готовности PR иных релизов. Внешняя запись решения/проверенного текста Release важна: API не доказывает первоначальный текст notes. Код: `scripts/publish_emergency_release.py`, `scripts/deploy_prod.py`. |
| Риски: CI на synthetic БД, не подтверждает ответы реальных каналов; backup на диске VPS, не спасает от потери VM; notification пока только серверный журнал; автотаймер deploy отложен. [FULL_CI](FULL_CI.md), [STAGE5_STEP4](STAGE5_STEP4.md), [PLAN](PLAN.md) | `docs/RELEASES.md`, `docs/OPERATIONS.md`; будущие задачи `docs/BACKLOG.md` | Backup limitation и timer уже записаны; внешние уведомления тоже есть в backlog. Уточнить границы CI и реальных каналов компактно. Не обещать внешнее оповещение. |
| Факт v1.0.4, ночной backup, приёмка 7.5 и очистка systemd; закрытие всей фичи только после переноса. [PLAN](PLAN.md), [STEP7_5_EMERGENCY_ACCEPTANCE](STEP7_5_EMERGENCY_ACCEPTANCE.md) | `docs/HISTORY.md` — короткий итог; `docs/PRODUCTION.md` — только актуальный VPS; `docs/BACKLOG.md` — закрытие пункта | `PRODUCTION/HISTORY` уже содержат live v1.0.4/backup/очистку. Добавить короткий итог 7.5 и финальной документационной приёмки. В backlog пункт пока открыт и обещает уведомления, хотя принято только серверное журналирование — уточнить критерий. Не утверждать, что merge #35 уже выпущен на VPS: tag/Release/deploy после него не делали. |

## Что оставить лишь в истории, а не в постоянных правилах

- `BASELINE`, `STAGE5_READINESS`, `STEP7_1_PREMERGE`: исходные снимки,
  временные blockers и pre-merge пробы. Достаточно краткого итога в `HISTORY`.
- `PR_FAST`, `BRANCH_RULES`, `MERGE_POLICY`, `FULL_CI*`, `RELEASE_BRANCH`,
  `PUBLISH_RELEASE`, `DEPLOY_SELECTOR`, `MIGRATIONS`: переносить только
  действующий контракт, не логи проб и не устаревшие фразы «ещё нет в main».
- `HOTFIX_PAIRS`, `STEP7_5_EMERGENCY_ACCEPTANCE`, `STEP6_1_BYPASS_RULE_SUITE.json`,
  `STEP6_4_VERIFICATION`: доказательства, номера пробных PR, временные rulesets,
  снимки API и fake-host результаты. Оставить короткий итог приёмки в `HISTORY`.
- `STAGE5_STEP2`, `STAGE5_STEP3`, `STAGE5_STEP4`, `STEP7_5_SYSTEMD_CLEANUP`:
  одноразовые install scripts, окна, maintenance bundles, старые IP/boot ID,
  повторы причин прежних сбоев. Сохранить действующий recovery и системные
  ограничения; исторические факты уже есть в `PRODUCTION/HISTORY`.
- `STEP7_2_RETENTION`: synthetic fixtures и гипотетические кандидаты не
  являются производственной процедурой очистки.

## Перед удалением временного каталога

1. Сверить операционные инструкции со свежим `scripts/`/`.github/workflows/`
   и, если нужно описывать фактическую конфигурацию GitHub/VPS, проверить её
   чтением отдельно. Старые временные документы местами противоречат уже
   принятому поведению; эта карта приоритетом считает код и результаты 7.5.
2. Заменить все действующие ссылки на временную папку. Вне неё ссылки сейчас
   есть как минимум в `docs/RELEASES.md`, `docs/OPERATIONS.md`,
   `docs/PRODUCTION.md`; последняя ссылается на исторические runbook, поэтому
   сохранить смысл цитирования в `HISTORY` или постоянном recovery-разделе.
3. Проверить все строки таблицы, внутренние Markdown-ссылки и поиск
   `auto-deploy-main/` по всему репозиторию. Удалить временный каталог
   отдельным завершающим изменением, затем закрыть backlog. До этого он
   остаётся рабочим источником для сверки.

## Результат шага 7.6.2

Действующий контракт перенесён в `AGENTS.md`, `docs/RELEASES.md`,
`docs/OPERATIONS.md`, `docs/RUN.md`, `docs/ARCHITECTURE.md` и
`README.md`; короткий факт приёмки 7.5 добавлен в `docs/HISTORY.md`.
`docs/PRODUCTION.md` теперь ссылается на постоянный порядок возврата,
`docs/BACKLOG.md` уточняет серверный журнал и оставляет всю фичу открытой.
Это документационный перенос: свежую конфигурацию GitHub, VPS и реальный
аварийный выпуск заново не проверяли. Временный каталог сохранён для
сверки и завершающего удаления. Исторические разделы `PRODUCTION.md`
описывают состояние на указанные даты; их нельзя читать как текущий статус.
Старая ссылка на ресурс в `HISTORY.md` исправлена при проверке
относительных Markdown-ссылок; отсутствующих локальных целей больше нет.
