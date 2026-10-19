# Этап 4, шаг 3: версия и публикация

`scripts/publish_release.py` готовит обычный выпуск. Версия строго
`vMAJOR.MINOR.PATCH` без ведущих нулей. Первый выпуск — `v1.0.0` из
`release/1.0`. В существующей линии допускается следующий PATCH без
пропусков. Новая линия — следующий MINOR текущего MAJOR или следующий MAJOR
с `MINOR=0`, всегда с `PATCH=0`. Выбор `--change` подтверждает смысл
изменения контракта; человек описывает его в release notes. Исправление
старой линии допустимо после появления новой.

Команда сверяет origin и API с `karateka-git/compadre-assistant`, чистый
checkout `HEAD`, текущий tip `release/X.Y`, историю выпусков, занятость
тега или Release (в том числе draft), действующую защиту тега и Immutable
Releases. Для первого выпуска линии candidate должен быть предком текущего
`main` и иметь успешный `main-full` точного SHA. Это повторно использует
результат создания ветки. Если линия уже выпускалась, проверяются все merge
commits после последнего выпуска, полные URL выбранных PR в описаниях,
достижимость нового main merge либо существующего main commit и слияние
выбранных старших линий, а также успешный
`release-full` точного SHA. Используются общие selector и проверка этапов
3 и 4.2. Ветка и свободное имя читаются повторно перед записью. Сбой любого
Git/GitHub API означает отказ в допуске.

Подготовьте UTF-8 файл с изменениями, влиянием на публичный контракт и,
для MAJOR, причиной несовместимости. Пример после публикации релизной ветки:

```powershell
git fetch origin main release/1.0
# Выполнять из чистого checkout точного candidate SHA.
$env:GITHUB_TOKEN = gh auth token
python -m scripts.publish_release v1.0.0 <полный-SHA> --change initial --notes-file C:\temp\release-notes.txt --check
python -m scripts.publish_release v1.0.0 <полный-SHA> --change initial --notes-file C:\temp\release-notes.txt --dry-run
# Только после review и явного решения о публикации:
python -m scripts.publish_release v1.0.0 <полный-SHA> --change initial --notes-file C:\temp\release-notes.txt --publish
```

Файл notes должен быть вне checkout или в игнорируемом пути, иначе проверка
чистоты откажет. `--check` и `--dry-run` читают API и локальный Git без
записи. Dry-run выводит тело Release: текст человека, SHA, ветку, ссылку
на конкретные run и attempt полного CI, а для PATCH — URL проверенных PR
и точный SHA основания main: merge PR либо уже существующий commit.
`--publish` повторяет весь допуск, затем атомарно создаёт новый
`refs/tags/vX.Y.Z` на точном SHA через GitHub REST API, сверяет тег,
создаёт недрафтовый GitHub Release и ещё раз сверяет тег. Другая операция,
успевшая занять имя, приводит к отказу. Версия не переиспользуется.
REST API не даёт транзакции для тега и Release: сбой после создания тега
может оставить занятый тег без Release. В таком случае остановиться и
разобрать состояние вручную; тег не удаляется и не перемещается автоматически.
`make_latest=true` применяется к новой наибольшей SemVer-версии;
`make_latest=false` не переключает указатель latest при PATCH старой линии.

Публикация не содержит вызова VPS и не даёт автоматического deploy. В
`.github/workflows/` нет trigger по tag или Release; `release-full` слушает
только push в `release/*`. Команда для VPS — отдельный шаг этапа 5.

## Защита на GitHub: настройка и проверка 14.10.2026

Репозиторий публичный. GitHub допускает tag rulesets для публичных
репозиториев на GitHub Free. Исходное состояние: один активный branch
ruleset `PR and pr-fast for main and release` (ID 25067104), tag ruleset
отсутствовал, GET `/immutable-releases` возвращал `enabled: false`.

Создан отдельный активный tag ruleset
[Protect version tags v*](https://github.com/karateka-git/compadre-assistant/rules/25102688)
(ID 25102688): `target=tag`, только `refs/tags/v*`, пустые `exclude` и
`bypass_actors`, правила `update` и `deletion`. После создания GET по ID
подтвердил все поля. Branch ruleset 25067104 повторно прочитан и не менялся.
Затем включены Immutable Releases. GET `/immutable-releases` с API
`2026-03-10` и используемый командой `GitHub.get` (`2022-11-28`)
подтвердили `enabled: true, enforced_by_owner: false`. Вызов проверки
`_protection` настоящим клиентом GitHub для `v1.0.0` прошёл. Списки тегов
и Releases остаются пустыми; ни тег, ни Release не создавались.

Команда принимает только явно распознаваемые шаблоны
`refs/tags/v*`, `refs/tags/*` или точный ref, без exclusions.
После публикации GitHub блокирует перемещение и удаление связанного тега и
изменение assets, но название и release notes остаются редактируемыми.
Tag ruleset закрывает промежуток между созданием тега и Release. Администратор
репозитория всё ещё может изменить сами правила.

Источники GitHub:

- [Создание ruleset и доступность по планам](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository)
- [Правила update и deletion](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
- [Проверка Immutable Releases через REST API](https://docs.github.com/en/rest/repos/repos#check-if-immutable-releases-are-enabled-for-a-repository)
- [Поведение Immutable Releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)

Локально проверены позитивные и негативные пути функции. До локального
коммита `--check` останавливался на проверке чистого checkout; после коммита
он доходит до GitHub API и получает 404 для ещё не созданной
`release/1.0`. Успешный
`main-full` для `main`, настоящие парные PR и создание версии остаются
фактическими проверками после появления релизной линии.
