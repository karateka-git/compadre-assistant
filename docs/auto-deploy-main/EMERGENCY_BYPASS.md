# Шаг 6.1: аварийный обход `pr-fast` владельцем через PR

Статус на 16 октября 2026 года: **шаг 6.1 завершён**. Настройка владельца
подтверждена API, блокировка обычного merge — пробным PR, фактический bypass —
слиянием PR с неуспешным `pr-fast` только во временную `release/test-*`
ветку и записью rule suite `result=bypass`. Временные refs/worktree удалены,
`main` не изменён. Шаги 6.2–6.4 здесь не выполнялись.

## Права и доступность

Репозиторий `karateka-git/compadre-assistant` публичный, владелец — личный
аккаунт `karateka-git` (`User`, ID `42288695`). `GET /user` подтвердил тот же
аккаунт, а `GET /repos/karateka-git/compadre-assistant` — `permissions.admin=true`.
Поле тарифа в ответе `/user` отсутствовало; фактический тариф не установлен.
Публичным репозиториям rulesets доступны уже на GitHub Free, а текущие права
и доступность точечного обхода подтверждены успешным изменением API.
См. [доступность rulesets и режим обхода через PR](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository).

[REST API версии `2026-03-10`](https://docs.github.com/en/rest/repos/rules?apiVersion=2026-03-10#update-a-repository-ruleset)
поддерживает `actor_type=User` с обязательным `actor_id` и
`bypass_mode=pull_request` для branch ruleset. Поэтому доступ выдан одному
пользователю; роли Repository admin, Write, приложения, команды и deploy keys
в список обхода не добавлялись.

## Изменение и подтверждение

До шага в [ruleset `25067104`](https://github.com/karateka-git/compadre-assistant/rules/25067104)
было `bypass_actors=[]`, `current_user_can_bypass=never`.
16 октября 2026 года в `07:34:05 UTC` выполнен единственный запрос изменения:

```http
PUT /repos/karateka-git/compadre-assistant/rulesets/25067104
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2026-03-10
```

```json
{
  "bypass_actors": [
    {
      "actor_id": 42288695,
      "actor_type": "User",
      "bypass_mode": "pull_request"
    }
  ]
}
```

Ответ — `HTTP 200`. Независимый повторный GET подтвердил ровно одного actor
и `current_user_can_bypass=pull_requests_only`. В истории ruleset появилась
версия `53458094`, actor `User 42288695`, время
`2026-10-16T13:34:05.291+06:00`; предыдущая версия — `53134954`.
История доступна через
`GET /repos/karateka-git/compadre-assistant/rulesets/25067104/history`.
Версия API и точный actor подтверждены фактическим ответом GitHub,
а не предположением о поведении интерфейса.

Автоматическое сравнение JSON до изменения и после повторного чтения
подтвердило неизменность имени, типа, enforcement, условий, всех правил
и их параметров. Из значимых настроек изменился только список обхода;
GitHub обновил `updated_at` и вычисляемое право текущего пользователя.

| Проверка после изменения | Фактический результат |
| --- | --- |
| Область и активность | `active`; `refs/heads/main`, `refs/heads/release/*`; исключений нет |
| Обычное слияние | `pull_request`, только `merge`, 0 обязательных approvals |
| Обязательный быстрый CI | `required_status_checks`: `pr-fast` |
| Политика check | `strict_required_status_checks_policy=false`, `do_not_enforce_on_create=true` |
| Force push | Сохранён `non_fast_forward` |
| Обход | Только `User 42288695`, `pull_request`; владельцу доступен `pull_requests_only` |
| Защита тегов | Ruleset `25102688` структурно совпадает с исходным; bypass пуст |
| `main` | SHA до и после `73b0a4e58b8b654e0593ba634ca71b94264f2bd1` |

Эффективные правила получены отдельно для `main` и имени
`release/0.0-emergency-bypass-probe`: API в обоих случаях вернул
`pull_request`, `required_status_checks` с `pr-fast` и `non_fast_forward`
из ruleset `25067104`. Запрос правил допускает ещё не существующую ветку;
пробная ветка этим запросом не создаётся.
См. [контракт запроса эффективных правил](https://docs.github.com/en/rest/repos/rules?apiVersion=2026-03-10#get-rules-for-a-branch).

Read-only просмотр существующего draft [PR #9](https://github.com/karateka-git/compadre-assistant/pull/9)
показал успешный `pr-fast` для head
`4f8a5a4ccf01c87c6074fbd9b883ce473ec8016e`
([run `38699291851`](https://github.com/karateka-git/compadre-assistant/actions/runs/38699291851))
и `mergeStateStatus=CLEAN`. Этот PR не проверяет обход неуспешного check:
его содержимое, состояние draft и целевая ветка не менялись.

## Границы обхода

`For pull requests only` позволяет владельцу явно обойти правила при merge
PR. Он не даёт обхода для прямого push. Защита force push сохранена;
прямой и force push в защищённые ветки не выполнялись. Ограничения подтверждены
конфигурацией и контрактом GitHub, но фактический отказ записи не испытывался.

Обход применяется ко всему этому ruleset, а не к одному check. Для аварийного
сценария владелец всё равно выбирает merge commit по правилам проекта.
Технический actor ограничивает инициатора обхода, но не автора PR и не
требует от него текстовой причины. Решение и причина остаются явным
операционным действием владельца; публикация и фиксация аварийного выпуска
относятся к последующим шагам. Обход не отменяет уже запущенный Actions run
и не даёт допуска обычному selector выпуска без полного CI.

## Живая проба без merge

После подготовки настройки пользователь отдельно разрешил push временной
тестовой ветки, создание пробного PR и его очистку; на этой первой пробе
слияние ещё не было разрешено.
Рабочая ветка `codex/auto-deploy-production-main` не публиковалась.

В отдельном временном worktree от уже опубликованного SHA
`4f8a5a4ccf01c87c6074fbd9b883ce473ec8016e` рабочего PR #9 создана ветка
`codex/emergency-bypass-probe-20261016`. Единственный новый файл относительно
этого SHA — безобидный Markdown-маркер
`docs/auto-deploy-main/EMERGENCY_BYPASS_PROBE.md`. Локальные незакоммиченные
материалы, секреты, `.env`, код с искусственной ошибкой и реальные данные
в пробу не добавлялись. В диффе с `main` присутствовали и ранее опубликованные
изменения рабочего PR; они не создавались заново для пробы.

| Факт | Результат |
| --- | --- |
| Пробный PR | [#10](https://github.com/karateka-git/compadre-assistant/pull/10), не draft, цель `main` |
| Base SHA | `73b0a4e58b8b654e0593ba634ca71b94264f2bd1` |
| Head SHA | `74e137afb6ec62dc4cc4539d21d3663d3b7563dc` |
| Workflow | [PR fast, run `38868561864`](https://github.com/karateka-git/compadre-assistant/actions/runs/38868561864), event `pull_request`, attempt 1 |
| Обязательный check | [Job `116866983177`](https://github.com/karateka-git/compadre-assistant/actions/runs/38868561864/job/116866983177), имя `pr-fast`, conclusion `cancelled` |
| Состояние PR до закрытия | `mergeable=MERGEABLE`, `mergeStateStatus=BLOCKED`, `merged=false` |
| REST право владельца | `current_user_can_bypass=pull_requests_only`, единственный actor — `User 42288695` |
| GraphQL viewer | `karateka-git`, database ID `42288695`; `viewerCanMergeAsAdmin=false` |
| Очистка | PR закрыт `2026-10-16T07:40:16Z`, `merged=false`; удалённый ref вернул `404`; локальные пробные ветка и worktree удалены |
| `main` после очистки | Тот же SHA `73b0a4e58b8b654e0593ba634ca71b94264f2bd1` |

Пробный run отменён явной командой во время выполнения; итог `cancelled`
не выдаётся за `failure` тестов. Отмена успела попасть в итоговый результат
job, хотя его рабочие шаги завершились успешно. GitHub всё равно заблокировал
обычный merge из-за отсутствия успешного обязательного check.
После каждого из двух разрешённых push (создание и удаление временной
ветки) `AGENTS.md` полностью перечитан.

Флаг `viewerCanMergeAsAdmin` возвращался `false` и во время run, и после
отмены. Его описание в GraphQL schema: может ли viewer обойти branch
protections и немедленно слить PR. При одновременном REST
`current_user_can_bypass=pull_requests_only` этих ответов недостаточно для
вывода, что обход конкретного PR действительно доступен или недоступен.
На первой пробе настройка actor была подтверждена, но фактический доступ
к merge ещё не был доказан. Неопределённость снята второй пробой ниже.

Попытка read-only просмотра PR в in-app browser показала `Sign in`:
авторизованной браузерной сессии GitHub нет. Вход и выбор обхода не
выполнялись. Поэтому наличие кнопки/опции владельца в интерфейсе не
подтверждено. Другой авторизованный аккаунт не использовался: ограничение
обхода только владельцем доказано точным списком actor, но функциональный
отказ другому пользователю не испытывался.

## Функциональная проверка с merge только во временную цель

Пользователь затем явно разрешил слияние пробного PR с bypass во временную
`release/test-*` ветку и её удаление. Ruleset для пробы не изменялся.

Через GitHub refs API создана новая целевая ветка
`release/test-emergency-bypass-20261016` на опубликованном доверенном SHA
`4f8a5a4ccf01c87c6074fbd9b883ce473ec8016e`. Создание новой ветки прошло
обычные правила (`do_not_enforce_on_create=true`): rule suite `4557258944`
зафиксировал `result=pass`, before SHA из нулей и тот же after SHA.
Существующая защищённая ветка прямым push не обновлялась.

В отдельном временном worktree создана topic-ветка
`codex/emergency-bypass-merge-probe-20261016`. Единственный новый файл —
`docs/auto-deploy-main/EMERGENCY_BYPASS_MERGE_PROBE.md`, безопасный текстовый
маркер. Ни код приложения, ни секретные файлы, ни миграции не менялись.
Открыт [PR #11](https://github.com/karateka-git/compadre-assistant/pull/11)
из topic-ветки только в указанную временную цель.

| Факт | Подтверждённое значение |
| --- | --- |
| Base SHA перед merge | `4f8a5a4ccf01c87c6074fbd9b883ce473ec8016e` |
| Head SHA | `962f3064c35ab4a7516d3e14aa52ac7bd7de876c` |
| `pr-fast` | [Run `38870536039`](https://github.com/karateka-git/compadre-assistant/actions/runs/38870536039), attempt 1, `failure` |
| Обязательный job | [Job `116873307349`](https://github.com/karateka-git/compadre-assistant/actions/runs/38870536039/job/116873307349), `pr-fast`, `failure` |
| Ожидаемая причина отказа | `Check linked hotfix PRs`: у синтетического PR отсутствует блок `Hotfix-Links:` с полными URL |
| Перед bypass | Не draft, `mergeable=MERGEABLE`, `mergeStateStatus=BLOCKED`, `viewerCanMergeAsAdmin=false` |
| Merge SHA | `b56810079c3dfa0c6cb816d63a1d2d66cb452e05` |
| Родители merge commit | Base `4f8a5a4ccf01c87c6074fbd9b883ce473ec8016e`, head `962f3064c35ab4a7516d3e14aa52ac7bd7de876c` |
| Кто и когда слил | `karateka-git`, User ID `42288695`, `2026-10-16T07:58:46Z` |
| Rule suite | `4557275818`, actor `42288695`, ref `refs/heads/release/test-emergency-bypass-20261016`, `result=bypass` |

Перед операцией API повторно проверены точные base/head, имена веток и
открытое состояние PR. Выполнен явный обход через GitHub CLI:

```powershell
gh pr merge 11 --repo karateka-git/compadre-assistant --admin --merge `
  --match-head-commit 962f3064c35ab4a7516d3e14aa52ac7bd7de876c `
  --subject 'Проверить обход pr-fast во временной release/test-* ветке' `
  --body-file "$env:TEMP/obsChatBot-stage6-step1-merge/merge-message.md"
```

`--admin` явно запрашивает обход требований; отдельный `--match-head-commit`
защищает от слияния изменившегося head. Сообщение merge содержит причину
проверки, цель и IDs неуспешного CI. Команда вернула exit code 0; независимый
GET PR подтвердил `merged=true`, merge SHA и `merged_by` владельца.
Тег и Release не создавались, рабочий PR #9 и `main` не менялись.

Подробный ответ
`GET /repos/karateka-git/compadre-assistant/rulesets/rule-suites/4557275818`
сохранён без изменений значений в [STEP6_1_BYPASS_RULE_SUITE.json](STEP6_1_BYPASS_RULE_SUITE.json).
Он связывает actor, временный ref, before/after SHA с ruleset `25067104`:

- общий `result=bypass`;
- `required_status_checks` — `fail`: обязательный `pr-fast` неуспешен;
- `pull_request` — `pass`;
- `non_fast_forward` — `pass`.

Это подтверждает обход именно обязательного check при merge через PR.
См. [контракт rule suites API](https://docs.github.com/en/rest/repos/rule-suites?apiVersion=2026-03-10).
Предыдущий `viewerCanMergeAsAdmin=false` не является надёжным основанием
отказать в ruleset bypass: в этом реальном случае CLI успешно слил PR,
а GitHub независимо записал `bypass`. Авторизованный браузер для шага
не потребовался; внешний вид опции в веб-интерфейсе отдельно не проверялся.

События создания и merge временной целевой ветки запустили также `Release full`: run
`38870495604` после создания и `38870641134` после merge. Оба завершились
`failure`; во втором `release-full-controller=failure`, `release-full=skipped`.
Никакого полного допуска выпуска эти пробы не дают. Попытка отменить второй
run застала его уже завершённым; его результат не менялся. Публикации
и серверного развёртывания не было.

## Очистка и итог шага 6.1

PR #11 находится в `closed`, `merged=true`. Обе временные удалённые ветки
удалены разрешённым `git push --delete`; локальная topic-ветка и её worktree
удалены после проверки чистоты и абсолютного пути. Запросы matching refs
для `release/test-emergency-bypass` и `codex/emergency-bypass` вернули пустые
массивы, открытых пробных PR не осталось. Пробные маркеры в рабочую ветку
этой фичи не переносились. После каждого из двух push второй пробы
`AGENTS.md` полностью перечитан.

После очистки полное структурное сравнение текущих branch ruleset
`25067104` и tag ruleset `25102688` с состоянием перед второй пробой
подтвердило отсутствие изменений. `main` до и после остался на
`73b0a4e58b8b654e0593ba634ca71b94264f2bd1`. Реальные релизные линии,
VPS, теги и GitHub Releases не затрагивались.

Шаг 6.1 закрыт: точный владелец получил обход только через PR, обычное
слияние без успешного check заблокировано, реальный обход подтверждён
merge commit и rule suite. Функциональный отказ другому аккаунту и прямой/
force push в защищённую ветку не испытывались; их ограничения подтверждены
сохранённой конфигурацией. Аварийная публикация, серверный `--emergency`
и сценарии этапов 6.2–6.4 ещё не реализованы этим шагом.

## Отмена только выданного обхода

Если аварийный доступ потребуется отозвать, после чтения текущего ruleset
администратор отправляет в тот же endpoint только `{"bypass_actors":[]}`
и повторно проверяет `current_user_can_bypass=never`, сохранность `pr-fast`,
PR и `non_fast_forward`. Если список уже изменён другими работами, сначала
согласовать отзыв новых прав. Удалять ruleset или отключать защиту не нужно.
Этот запрос в ходе шага не выполнялся.
