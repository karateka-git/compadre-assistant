# Шаг 7.1: техническая приёмка до merge

Проверка выполнена 16 октября 2026 года для исходного кода локального commit
`7b2356ce78167492c9a043bae509d3a9e299a1d7` ветки
`codex/auto-deploy-production-main`; этот отчёт добавлен следующим
документационным commit. Это **локальный результат для review**,
а не закрытый pre-merge gate: опубликованный head [PR #9](https://github.com/karateka-git/compadre-assistant/pull/9)
остаётся `4f8a5a4ccf01c87c6074fbd9b883ce473ec8016e` и не содержит 12
последующих commits реализации и этот отчёт. Обновление PR требует отдельного разрешения
владельца на push текущей ветки. `main`, настоящая релизная линия, теги,
Releases и VPS не менялись.

## Локальный повтор

| Проверка | Результат |
| --- | --- |
| `unittest discover -s tests -q` в `.venv` | 673 теста, OK; 1 skipped |
| `--sqlite-smoke`, `--pipeline-smoke`, `--analysis-smoke` | Все три прошли |
| `check_full_migrations --previous-sha ac2ac981ba628a881b483238a4eaf191a644de6a` | Fresh/upgrade/previous code прошли на временных SQLite |
| `check_pr_fast.py --base-sha origin/main`, `compileall`, `git diff --check` | Прошли |
| `docker compose --env-file .env.example config --quiet` с временным синтетическим `.env` | Прошёл; временный `.env` удалён |
| `check_candidate --expected-sha 7b2356c…` | Не выполнен: локальный Docker Engine не запущен (`dockerDesktopLinuxEngine` недоступен). Это ограничение среды, не успешная проверка образа текущего SHA. |

Полный набор включает локальные fake host сценарии: отказ обычного selector
для непроверенной версии, единый image ID обоих каналов, backup SQLite,
rollback и `degraded` state, в том числе аварийный PATCH и новую линию.
Подробные сценарии и границы: [STAGE5_STEP4.md](STAGE5_STEP4.md) и
[STEP6_4_VERIFICATION.md](STEP6_4_VERIFICATION.md). Реальные Docker,
systemd, reboot, каналы и production SQLite эти fake проверки не заменяют.

## Пробные GitHub события и review контрактов

- `pr-fast` при открытии, новом commit, отмене старого run, изменении цели
  на `release/*` и повторном открытии зафиксирован в [PR_FAST.md](PR_FAST.md).
  Повторное чтение GitHub подтвердило отменённый
  [run 38542112822](https://github.com/karateka-git/compadre-assistant/actions/runs/38542112822)
  и успешный новый
  [run 38542149579](https://github.com/karateka-git/compadre-assistant/actions/runs/38542149579).
  Сценарий изменения базы без нового commit и новый check после обновления
  head проверены на пробных PR #6/#7 в [MERGE_POLICY.md](MERGE_POLICY.md).
- Действующий branch ruleset `25067104` повторно прочитан через GitHub API:
  `main` и `release/*` требуют PR, merge commit и `pr-fast`; создание ветки
  допускается, force push запрещён. Единственный bypass actor — владелец
  `42288695` с режимом `pull_request`. Tag ruleset `25102688` активен для
  `v*`, запрещает update/deletion и не содержит bypass actors.
- Пробный [PR #8](https://github.com/karateka-git/compadre-assistant/pull/8)
  слит во временную `release/0.0`; его
  [release-full run 38604745834](https://github.com/karateka-git/compadre-assistant/actions/runs/38604745834)
  повторно прочитан: `success` у controller и reusable `full-suite`, включая
  unittest, три smoke, миграции, сборку и healthcheck образа. Это доказательство
  для пробного SHA `15e6800b10f08155e8efca4a2dd4b5f8ea58358a`, не для
  текущего рабочего SHA.
- Пробный [PR #11](https://github.com/karateka-git/compadre-assistant/pull/11)
  во временную `release/test-*` имел неуспешный
  [pr-fast run 38870536039](https://github.com/karateka-git/compadre-assistant/actions/runs/38870536039)
  и был слит владельцем через разрешённый PR bypass. Проверенные rule suite,
  причина и cleanup описаны в [EMERGENCY_BYPASS.md](EMERGENCY_BYPASS.md).
  Обычный прямой push в защищённую ветку не выполнялся; запрет подтверждён
  действующим ruleset. Пробный `release-full` на ветке с тестовым именем
  ожидаемо завершился отказом controller и не даёт релизного допуска.
- Локальные тесты покрывают порог трёх PR, ручной запуск до порога,
  неизменный SHA создания релизной ветки, точный workflow/run/attempt/jobs/steps
  в selector и парные PR hotfix. Код workflow не передаёт production secrets:
  caller читает Actions и GitHub, reusable workflow использует синтетическую
  SQLite и фиктивные credentials. Аварийный маршрут повторно проверяет
  опубликованный SHA, protected tag, PR журнал, причину, CI и защитные стадии
  VPS. Текущий код reviewed по этим контрактам; подтверждённого блокирующего
  дефекта в доступных проверках не обнаружено.

## Открытые условия pre-merge gate

1. После отдельного разрешения на push опубликовать **этот и все предыдущие
   локальные commits** в PR #9. Обновить его устаревшее описание: сейчас оно
   утверждает, что этапы 5–7 ещё не выполнены, и перечисляет 618 тестов.
   Записать техническую приёмку и границы в PR, получить успешный `pr-fast`
   точного нового head и убедиться, что PR не использует аварийный bypass.
2. Candidate build/healthcheck исходного кода `7b2356c…` локально не прошёл
   из-за недоступного Docker Engine. Его можно повторить на работающем Docker;
   успешный пробный `release-full` выше проверял более старый commit. Это
   ограничение локальной проверки, а полный CI настоящего выпуска остаётся
   обязательным после merge.
3. Настоящий полный `main-full`, `release/1.0`, `v1.0.0`, первый выпуск и
   проверки VPS относятся к post-merge gate. Автоматический счёт трёх PR,
   ручной `main-full` и отмена его устаревшего run доступны для фактической
   проверки только после появления caller в `main`. Cleanup действующих
   релизных линий относится к шагу 7.2 и здесь не выполнялся.

Временный каталог сохраняется до завершения всей фичи по исключению в
[PLAN.md](PLAN.md). Шаг 5.5 и весь этап 7 этим документом не закрываются.
