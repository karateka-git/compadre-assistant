# Шаг 6.4: сквозная проверка аварийных сценариев

Статус на 16 октября 2026 года: **локальная техническая проверка завершена**.
Настоящие GitHub tags/Releases, `main`, рабочая ветка на remote и production
VPS не менялись. Испытания выполнялись в одноразовых локальных Git
репозиториях с fake API и fake host. Временный каталог сохраняется до конца
всей фичи по решению в [PLAN.md](PLAN.md).

## Сценарий A: PATCH действующей линии

Это исторический результат испытания прежнего протокола; будущие выпуски
используют URL в описаниях по [HOTFIX_PAIRS.md](HOTFIX_PAIRS.md).
В одноразовом Git создан baseline, два topic commits с одинаковым патчем и
два merge commits: PR #10 в `release/1.0` и PR #11 в `main`. Fake API вернул
взаимные `Hotfix-Pair`, состояния `pr-fast=failure`/`success` на момент merge,
`bypass=yes`/`no`, rule suites и `release-full=pending`. Реальная функция
`check_release` прошла по первой родительской линии локального Git и
сравнила бинарные Git diff; список только из одного PR был отвергнут.
Fake публикация создала прямой tag ref и immutable Release `v1.0.1`.
Обычный selector отказал; аварийный подтвердил пары, точный SHA, защищённый
тег и ожидающий полный CI. Fake host выполнил backup, migrate и попытку
переключения двух каналов; искусственный сбой candidate привёл к
`rolled_back`, старому image и сохранённым причинам в state. Подмена номера
парного PR и разрыв взаимной ссылки закрыли допуск.

## Сценарий B: новая линия из `main`

В одноразовом Git PR merge вошёл в `main`, от его точного SHA создана
`release/1.0`. Fake API дал неуспешный `pr-fast` на момент merge,
подтверждённый bypass владельца и `main-full=pending`. Аварийная публикация
прошла без успешного full CI; обычный selector отверг тот же Release.
Аварийный selector подтвердил tag/SHA/ветку и журнал PR. Fake host дошёл до
`success`: отдельный SQLite backup существует, оба канала запущены на общем
новом image ID; state сохранил отдельно причину публикации и причину команды.
Подмена состояния `pr-fast`, URL полного CI, неполный PR-журнал, отсутствие итога, изменение
Release между чтениями и отказ API закрыли допуск.

## Исправление по результатам проверки

Серверный аварийный selector прежде проверял только формат PR-строк, не
сопоставляя их с GitHub. Теперь он заново вычисляет полный журнал PR из
данных API, сверяет пары PATCH и доказывает полноту merge commits от прошлого
выпуска по первой родительской линии. Заявленный `full CI=success` требует
строгой проверки конкретного run/attempt. Publisher запрещает служебные
заголовки внутри произвольных notes. Серверный вывод показывает обе причины
раздельно, чтобы причина команды не скрыла опубликованную.

## Проверки и границы

Запущены `python -m unittest tests.test_emergency_end_to_end
tests.test_select_deploy_release tests.test_publish_emergency_release
tests.test_deploy_prod -q` и полный
`.\.venv\Scripts\python.exe -m unittest discover -s tests -q`:
673 теста успешны, 1 пропущен. Системный Python без project dependencies
дал 14 ошибок импорта `openai`, `httpx`, `aiogram`; это не дефект кода.
Проверены существующие fault injection для backup, миграций, обоих каналов,
rollback/degraded и серверного state. Новые интеграционные тесты используют
настоящие локальные Git merge commits, но GitHub API, Docker, systemd и VPS
заменены fake объектами. Настоящие правила защиты tag/ruleset и обход через
PR ранее проверены на временной ветке в [EMERGENCY_BYPASS.md](EMERGENCY_BYPASS.md);
настоящая аварийная публикация и VPS deploy ожидают отдельного решения
владельца. Текст причины Release и исторические неизвестные статусы имеют
границу доверия, описанную в [EMERGENCY_DEPLOY.md](EMERGENCY_DEPLOY.md).
