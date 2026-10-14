# Шаг 2.1: быстрый CI для PR

Реализация проверена локально и на временном GitHub PR
[#1](https://github.com/karateka-git/compadre-assistant/pull/1). Настройки
защиты веток и production не менялись; пробный PR не сливался.

## Что запускается

Workflow `.github/workflows/pr-fast.yml` реагирует на `opened`, `reopened`,
`synchronize` и `edited` у PR в `main` и `release/*`. Единственный job и точное
имя обязательного check — `pr-fast`. Checkout берёт тестовый merge GitHub;
`fetch-depth: 0` нужен для сравнения миграций с `base.sha` PR. Незавершённый
run того же PR отменяется при следующем событии. Токену дан только
`contents: read`, credentials не сохраняются в checkout, production secrets
не передаются.

Проверки идут в таком порядке:

1. `compileall` для Python-пакетов, тестов и скриптов.
2. `scripts/check_pr_fast.py`: запрет tracked `.env` (кроме `.env.example`),
   PEM/ключей/SQLite и PEM-маркеров приватного ключа внутри tracked-файлов;
   прежние SQL-миграции должны совпадать с базой PR по Git blob; новые
   миграции имеют формат `NNNN_name.sql` и непрерывную нумерацию.
3. После установки фиксированных runtime requirements — `test_config.py`
   (27 тестов), `test_capability_composition.py` (14) и
   `test_migration_runner.py` (1). Набор не зависит от изменённых файлов.
4. `docker compose config --quiet` с созданным из `.env.example`
   синтетическим `.env`; сервисы и внешние API не запускаются.

Зависимости Actions закреплены по SHA: checkout v5.0.1 и setup-python v6.0.0.
После первого пробного run checkout v4.2.2 заменён на v5.0.1, потому что
GitHub предупреждал об устаревшем Node 20; следующий run с v5.0.1 прошёл.
На локальной Windows-машине установка requirements в чистое venv заняла
67,2 с, все 42 теста — 2,8 с, проверка Compose — около 3 с. Это измерение
не является временем GitHub-hosted Ubuntu runner: успешные пробные jobs
заняли 17–21 с без учёта ожидания runner. Предельное время job — 10 минут.

## Что проверено локально

- Существующий проект проходит проверку tracked-файлов и миграций.
- Отдельный временный Git-репозиторий: изменение `0001`, пропуск номера
  новой миграции и добавленный tracked `.env` правильно завершаются ошибкой.
- `compileall`, фиксированный набор из 42 тестов и Compose на синтетическом
  `.env` завершились успешно. `git diff --check` выполнен.

## Проверка на GitHub

Временный PR #1 сначала был направлен в `main`, затем в созданную только для
пробы `release/0.0-pr-fast-probe`. PR закрыт без merge; обе временные
удалённые ветки и локальный пробный worktree удалены. Ни один пробный commit
не попал в `main` или production release.

| Сценарий | Наблюдение |
| --- | --- |
| `opened` в `main` | [Run 38541749204](https://github.com/karateka-git/compadre-assistant/actions/runs/38541749204): `pr-fast` = `success`, 17 с |
| `synchronize` с tracked `.env` | [Run 38541899373](https://github.com/karateka-git/compadre-assistant/actions/runs/38541899373): `pr-fast` = `failure` на проверке файлов, 7 с |
| Два последовательных новых commit | [Run 38542112822](https://github.com/karateka-git/compadre-assistant/actions/runs/38542112822) для старого commit отменён; [run 38542149579](https://github.com/karateka-git/compadre-assistant/actions/runs/38542149579) для нового commit = `success`, 21 с |
| `edited`: цель изменена на `release/*` | [Run 38542242756](https://github.com/karateka-git/compadre-assistant/actions/runs/38542242756): новый тестовый merge, `pr-fast` = `success`, 20 с |
| `reopened` | [Run 38542330948](https://github.com/karateka-git/compadre-assistant/actions/runs/38542330948): `pr-fast` = `success`, 18 с |

На шаге 2.2 ещё предстоит сделать check обязательным и убедиться, что
`failure` блокирует обычное слияние. Изменение базы без новых commits и
строгость требования актуальной базы относятся к проверке шага 2.3.

Изменения workflow, deploy-скриптов и миграций требуют просмотра владельцем;
само правило review настраивается вместе с защитой веток на шаге 2.2.
