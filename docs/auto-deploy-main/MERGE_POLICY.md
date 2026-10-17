# Шаг 2.3: merge commit и изменение базы PR

14 октября 2026 года поведение проверено на временной ветке `release/0.0-merge-policy-probe`, созданной из `c60c6593e8871d7ea3054d0b86b4b35a5b39c191`. Ветка попадала под действующий [ruleset 25067104](BRANCH_RULES.md). Два независимых PR меняли разные пробные файлы.

| Событие | Наблюдение |
| --- | --- |
| [PR #6](https://github.com/karateka-git/compadre-assistant/pull/6) и [PR #7](https://github.com/karateka-git/compadre-assistant/pull/7) открыты к одной базе | [Run #38543578044](https://github.com/karateka-git/compadre-assistant/actions/runs/38543578044) и [run #38543583345](https://github.com/karateka-git/compadre-assistant/actions/runs/38543583345) завершились успешно; оба PR получили `mergeStateStatus=CLEAN`. |
| PR #6 слит способом `merge` | Создан commit [`200357f`](https://github.com/karateka-git/compadre-assistant/commit/200357f490acd2399bf10b50a04f61e3a7a23364) с двумя родителями: прежний tip базы `c60c659` и head PR `afbd051`. В первой родительской линии появился ровно один новый merge commit, связанный с PR #6. |
| База PR #7 стала `200357f`, head остался `eb6e37d` | GitHub показывал `mergeable=MERGEABLE` и `mergeStateStatus=CLEAN`; прежний успешный run #38543583345 остался единственным `pr-fast` run этого head. Повторного прогона из-за одного только изменения базы не было. |
| Head PR #7 обновлён до `50baf1d` | Запущен новый [run #38543749196](https://github.com/karateka-git/compadre-assistant/actions/runs/38543749196). Пока он выполнялся, `mergeStateStatus=BLOCKED`; после успеха стало `CLEAN`. PR #7 не сливался. |

Следовательно, выбранный ruleset с `strict_required_status_checks_policy=false` работает согласно плану. Для будущего счётчика на `main` можно обходить первую родительскую линию от последнего учтённого commit и считать merge commits PR; при реализации счётчика нужно отдельно исключить любые не-PR merge commits и проверить границы отсчёта. Эта проверка подтверждает форму истории и поведение GitHub для двух независимых изменений; конфликты при пересечении файлов она не исследовала. Полный CI после merge относится к этапу 3.

После наблюдений PR #7 закрыт без слияния, все три временные удалённые ветки и пробный локальный worktree удалены. `main` и настоящие релизные ветки не менялись.
