# Шаг 7.7: публикация v3.2.0 перед установкой

Здесь записан только GitHub-подэтап настоящего выпуска. Подготовка,
production baseline и порядок установки — в [предыдущем отчёте](STAGE7_7_PREPARATION.md).
Контроллер и runtime на VPS не обновлялись; `deploy-prod` не запускался.

## Подтвердить допуск и опубликовать

06.11.2026 перед записью проверены удалённый `main`, отсутствие
`release/3.2` и `v3.2.0`, exact полный CI, tag ruleset и Immutable Releases.
`main` и `main-full` [42249037867/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42249037867/attempts/1)
указывали на `cabc0e24d28947123868db8a60f6b55095feb63e`; run имел
`completed/success`. Активный tag ruleset `25102688` защищал `refs/tags/v*`
от update/deletion без bypass; immutable releases включены.

Черновик [RELEASE_V320.md](RELEASE_V320.md) сохранён в task-ветке
коммитом `78ac349963c9908d15889c5c25be20e3d27207a4` (русское сообщение).
Из этого же закреплённого worktree выполнен временный чистый detached checkout
точного candidate SHA; основной рабочий каталог и чужие worktree не менялись.
Штатный `scripts.create_release_branch` прошёл `--check-only`, создал
локальную `release/3.2` и удалённую ветку через GitHub API. После записи
удалённый ref повторно совпал с exact SHA. `AGENTS.md` перечитан.

`scripts.publish_release v3.2.0 <candidate> --change minor` прошёл `--check`
и `--dry-run`; просмотрены весь текст и служебные поля Commit/Ветка/Полный CI.
Затем `--publish` создал прямой защищённый tag и недрафтовый GitHub Release.
Отдельная read-only сверка после публикации:

| Объект | Результат |
| --- | --- |
| `main` | `cabc0e24d28947123868db8a60f6b55095feb63e` |
| `release/3.2` | тот же SHA |
| `v3.2.0` direct tag ref | тот же SHA |
| [Release v3.2.0](https://github.com/karateka-git/compadre-assistant/releases/tag/v3.2.0) | ID `436823646`, `target_commitish` exact SHA; `draft=false`, `prerelease=false`, `immutable=true`; published `2026-11-06T18:01:40Z` |
| Release body | сохранённый текст notes, `Тип выпуска: обычный`, exact Commit/Ветка, ссылка на `42249037867/1` |
| Полный CI после публикации | exact `headSha`, `headBranch=main`, `attempt=1`, `completed/success` |

После создания тега/Release `AGENTS.md` перечитан. Checkout возвращён на
`codex/ci-release-review-plan`; временная копия notes вне репозитория удалена.
Production VPS, установленный старый controller, systemd, боты, БД и backup
в этом подэтапе не менялись. GitHub Release сам по себе не устанавливает код.

## Условие продолжения

Шаг 7.7 **не завершён**: нужны свежий VPS baseline, отдельная проверенная
установка controller snapshot и selector units, внешний reviewed policy pin,
отдельный read-only GitHub API token, backup текущей БД, затем обычный
`sudo deploy-prod v3.2.0` и независимая проверка обоих каналов/данных.
Локальный `gh` OAuth token имеет широкие scopes и на VPS не копировался.
Живые ответы Telegram/VK остаются пользовательской приёмкой. До отдельного
review этого результата production запись не начиналась.
