# Этап 6, шаг 6.2: аварийная публикация

Отдельная команда `python -m scripts.publish_emergency_release` готовит
аварийный GitHub Release. Обычная `scripts.publish_release` сохраняет требование
успешного полного CI точного SHA. Серверный `deploy-prod --emergency` в этом
шаге ещё не реализован; создание настоящего тега и Release не выполнялось.

## Явный вызов

Из чистого checkout точного SHA tip `release/X.Y` владелец с token GitHub
`User 42288695` вызывает:

```powershell
$env:GITHUB_TOKEN = gh auth token
python -m scripts.publish_emergency_release v1.0.0 <полный-SHA> --change initial --reason "срочная причина" --notes-file C:\temp\emergency-notes.txt --pr 123 --check
python -m scripts.publish_emergency_release v1.0.0 <полный-SHA> --change initial --reason "срочная причина" --notes-file C:\temp\emergency-notes.txt --pr 123 --dry-run
# После просмотра точного манифеста и отдельного решения владельца:
python -m scripts.publish_emergency_release v1.0.0 <полный-SHA> --change initial --reason "срочная причина" --notes-file C:\temp\emergency-notes.txt --pr 123 --publish
```

Для PATCH указать `--change patch` и по одному `--pr N` на каждый PR обеих
пар. Причина обязательна и должна быть одной строкой; файл notes непустой.
`--check` и `--dry-run` ничего не записывают. `--publish` делает запись только
после полного повторного чтения. Команда сверяет `/user` с точным ID владельца;
недоступный endpoint или чужой token закрывает допуск.

## Что проверяется

- Канонический SemVer, последовательность и одноразовость версии; отсутствие
  и тега, и Release (включая draft), активная защита тега без bypass и
  включённые Immutable Releases.
- Совпадение API/origin проекта, чистый локальный checkout и `HEAD`, точный
  удалённый tip релизной ветки. Для новой линии SHA должен быть предком `main`.
- Каждый названный PR уже слит в нужную ветку, его merge commit является
  предком кандидата (для PATCH парный PR проверяется в текущем `main`), а
  второй родитель merge commit совпадает с head PR.
- PATCH сверяет все merge commits после последнего выпуска линии, обе взаимно
  связанные и слитые пары PR, достижимость в `main`, одинаковый Git diff либо
  точное подтверждение адаптации владельцем. Список `--pr` обязан в точности
  совпадать с найденными парами. Только финальное условие успешного
  `release-full` в этом режиме пропущено; обычный вызов сохраняет его.

Для новой линии список `--pr` фиксирует PR, которые владелец явно относит к
аварийному выпуску. Команда доказывает вхождение каждого указанного merge
commit в candidate, но не утверждает, что этот список описывает всю историю
`main` с начала проекта. Текст манифеста не должен трактоваться как такое
утверждение.

## Проверяемый снимок

Для каждого PR публикуются номер, целевая ветка, точный merge SHA и
`merged_at`, состояние `pr-fast` **на момент merge**, URL конкретного run и
attempt, состояние обхода и ID rule suite при наличии. `pr-fast` берётся из
`pr-fast.yml` по связи run с номером PR, его head SHA и base branch. Сам
`run.head_sha` для `pull_request` может относиться к синтетическому test merge,
поэтому не используется как head PR. Каждая попытка сверяется по временам
начала и завершения job относительно `merged_at`: результат, появившийся
после merge, не выдаётся за тогдашний успех. Если run не найден в доступной
истории, записывается `not_run`; при неполной/старой истории — `unverified`.

Rule suite ищется по целевому ref, merge SHA и времени merge, затем читается
детальная запись с branch ruleset `25067104`. `bypass=yes` допустимо только
при `result=bypass` и actor ID `42288695`; `result=pass` даёт `bypass=no`.
Отсутствие либо неоднозначность записи обозначается `unverified`.
GitHub API ограничивает список rule suites периодом до месяца; более старое
слияние нельзя задним числом объявить подтверждённым bypass.

Отдельная строка `Полный CI:` показывает `main-full` для новой линии либо
`release-full` для PATCH, состояние **на момент подготовки публикации** и
URL точной попытки, если она есть. Значения состояния: `success`, `failure`,
`pending`, `not_run`, `unverified`. Успех полного CI подтверждается строгим
selector обязательных jobs/steps; одной строки conclusion от GitHub
недостаточно. Release notes отдельно перечисляют все проверки без
подтверждённого успеха. Ошибка запроса GitHub не преобразуется в
`unverified`: она прекращает публикацию до записи.

Манифест содержит `Тип выпуска: аварийный`, точный `Commit`, `Ветка`,
`Причина`, `Полный CI`, список PR и пар, а также непроверенные проверки.
Шаг 6.3 сможет различать обычный и аварийный Release по типу, сохраняя
отдельный серверный допуск. `--publish` заново сверяет снимок, создаёт прямой
tag ref на SHA, повторно проверяет кандидата и журнал, затем создаёт
недрафтовый Release и требует read-back с `immutable=true`, точным body и
SHA. GitHub не даёт транзакции между tag ref и Release: частичный сбой
оставляет имя занятым для ручного разбора; автоматического удаления тега нет.

## Локальная проверка

Использовался только fake API. Проверены: результат быстрого CI до и после
merge, ожидающий/отсутствующий полный run, подтверждённый и неизвестный
bypass, обязательная причина и личность владельца, список пар PATCH,
повторный допуск перед записью, занятое имя, ошибка API и подтверждение
immutable Release. Реальный аварийный тег/Release не создавался.

Контракты API: [workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[rule suites](https://docs.github.com/en/rest/repos/rule-suites),
[test merge PR](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
