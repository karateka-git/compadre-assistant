# Шаг 2.2: защита `main` и `release/*`

14 октября 2026 года создан активный [repository ruleset «PR and pr-fast for main and release»](https://github.com/karateka-git/compadre-assistant/rules/25067104) (ID `25067104`). Перед изменением API вернул пустой список rulesets, а запрос защиты `main` — `404 Branch not protected`. Настройки репозитория допускали merge commit, squash и rebase; этот шаг не менял общие настройки способов слияния.

## Действующие настройки

Ruleset применяется к `refs/heads/main` и `refs/heads/release/*` и содержит:

- обязательный PR; в этом ruleset разрешён только способ слияния `merge`;
- обязательный check `pr-fast` для обычного слияния;
- `strict_required_status_checks_policy=false`: изменение базовой ветки само по себе не требует нового прогона; новый commit в PR требует актуального check;
- `do_not_enforce_on_create=true`: создание новой релизной ветки не требует check ещё не существующей ветки;
- запрет force push (`non_fast_forward`);
- `required_approving_review_count=0`, без обязательного code-owner review или одобрения последнего push;
- пустой `bypass_actors`: аварийный обход будет настроен только на этапе 6.

Правило PR не позволяет обычный прямой push в существующие целевые ветки. Прямой и force push не выполнялись в ходе проверки, чтобы не изменять `main` или реальный релиз. Запрет подтверждён конфигурацией активного ruleset через GitHub API; фактическая проверка ниже касается допуска PR.

В текущем составе проекта владелец одновременно автор и проверяющий своих PR. Обязательное одобрение самого себя GitHub не допускает, поэтому требование одного review сделало бы такие PR несливаемыми. Изменения workflow, deploy-скриптов и миграций владелец просматривает перед слиянием вручную; технически обязательный независимый review потребует второго участника с правом review и отдельного решения. Отсутствие обязательного review не отменяет проверки `pr-fast`.

## Проверка на GitHub

После включения правила получены следующие состояния GitHub GraphQL `mergeStateStatus` и check runs. Все PR оставлены без merge и закрыты после проверки.

| Цель и сценарий | PR | Check | Состояние обычного слияния |
| --- | --- | --- | --- |
| `main`, успешный | [#2](https://github.com/karateka-git/compadre-assistant/pull/2) | [Run 38542821797](https://github.com/karateka-git/compadre-assistant/actions/runs/38542821797) — `success` | Во время выполнения `BLOCKED`, после успеха `CLEAN` |
| `main`, tracked `.env` | [#3](https://github.com/karateka-git/compadre-assistant/pull/3) | [Run 38542939201](https://github.com/karateka-git/compadre-assistant/actions/runs/38542939201) — `failure` | `BLOCKED` |
| `release/0.0-branch-rules-probe`, успешный | [#4](https://github.com/karateka-git/compadre-assistant/pull/4) | [Run 38543023910](https://github.com/karateka-git/compadre-assistant/actions/runs/38543023910) — `success` | Во время выполнения `BLOCKED`, после успеха `CLEAN` |
| `release/0.0-branch-rules-probe`, tracked `.env` | [#5](https://github.com/karateka-git/compadre-assistant/pull/5) | [Run 38543046088](https://github.com/karateka-git/compadre-assistant/actions/runs/38543046088) — `failure` | `BLOCKED` |

Запрос эффективных правил GitHub для временной `release/*` ветки вернул все три правила этого ruleset: `pull_request`, `required_status_checks`, `non_fast_forward`. Ветка была создана из неизменённого `main` через GitHub API. После проверки закрыты PR #2–#5 без merge, удалены обе временные удалённые ветки и локальный пробный worktree. Пробный commit с `.env` не попал в `main` или релиз.

`CLEAN` означает, что GitHub считает PR пригодным для обычного слияния при текущих настройках; само слияние здесь не выполнялось. Гарантию поведения после изменения базы проверяет следующий шаг 2.3.

## Восстановление

До этого шага rulesets отсутствовали, `main` не был защищён. Если правило потребуется откатить, администратор может удалить ruleset ID `25067104` в [настройках GitHub](https://github.com/karateka-git/compadre-assistant/settings/rules/25067104) или через `DELETE /repos/karateka-git/compadre-assistant/rulesets/25067104`. Это вернёт исходное отсутствие защиты; перед удалением следует проверить, не появились ли другие правила, и не использовать удаление как аварийный обход CI.
