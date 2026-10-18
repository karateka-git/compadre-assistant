# Этап 4, шаг 2: связанные PR исправления

Для hotfix создают PR в `main` и по одному PR в каждую явно выбранную
`release/X.Y`. Описание каждого PR содержит отдельный блок `Hotfix-Links:`:

```text
Hotfix-Links:
https://github.com/karateka-git/compadre-assistant/pull/21
https://github.com/karateka-git/compadre-assistant/pull/22
```

В блоке указывают **все остальные** PR группы, без собственной ссылки.
Примерные номера заменяют на реальные URL. Main PR перечисляет каждый
выбранный release PR; каждый release PR перечисляет main и остальные
выбранные release PR. Одна строка — один полный URL PR этого репозитория.
Блоки должны быть взаимными, целевые ветки — `main` и различные
`release/X.Y`. Никакой автоматический алгоритм не определит намеренно
забытую линию: выбор линий делает владелец явно.

Main PR проходит `pr-fast` и сливается **первым** обычным merge commit.
Release PR до этого держат в draft. После main merge выбранные release PR
переводят в ready for review по порядку от старой линии к новой. Для каждой
следующей линии ранее выбранные release PR уже должны быть слиты. Main PR
не ждёт merge release PR, поэтому цикл зависимостей отсутствует.

`pr-fast` читает актуальные описания и проверяет блоки ссылок. Для ready
release PR отдельный `hotfix-deps` ещё проверяет, что main PR и PR всех
выбранных более старых линий имеют merge commit в своих целевых ветках и
commit достижим из текущих head этих веток. Оба check перезапускаются при
`ready_for_review`, `edited` и `synchronize`. Перевод в draft блокирует
merge средствами GitHub; skipped `hotfix-deps` в draft не считается
эксплуатационным допуском. В GitHub ruleset для `release/*` позднее нужно
добавить **оба** required check (`pr-fast` и `hotfix-deps`); для `main`
достаточно `pr-fast`. Пока ruleset не обновлён, второй check информативен.

Изменение описания одного PR запускает workflow этого PR, но не других
PR группы. После поздней правки связанных описаний следует повторно
запустить проверки release PR перед merge, например draft → ready.
Workflow `pull_request` исполняется из PR test merge, поэтому required
check сам по себе не защищает от автора, который меняет workflow. Это
предел модели GitHub checks, а не доказательство защищённого исполнения.

Перед публикацией `scripts/check_hotfix_pairs.py release` независимо читает
живые описания, проверяет все merge commits первой родительской линии после
последнего опубликованного baseline, соответствие PR целевым веткам,
взаимность URL, фактический merge main и старших выбранных release линий,
точный текущий candidate и успешный `release-full`. Для первого выпуска
линии baseline — её точка создания с успешным `main-full`. Ошибка Git/API
закрывает допуск. Бинарное сравнение патчей и owner attestation не являются
условиями новой публикации. Различия реализации описывают в PR и release
notes; ссылки не доказывают семантическое равенство.

```powershell
$env:GITHUB_TOKEN = gh auth token
python -m scripts.check_hotfix_pairs release release/1.0 <candidate-SHA> <baseline-SHA>
```

Проверка только читает GitHub и локальный Git. Успешный Release manifest
содержит URL связанных PR и проверенный main merge SHA. Уже опубликованный
`v1.0.3` остаётся историческим выпуском по прежним правилам.
