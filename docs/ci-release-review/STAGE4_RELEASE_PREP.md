# Подготовить выпуск базовой версии: шаг 4.5

Read-only проверка 02.11.2026 подготовила маршрут выпуска и установки базового
runtime. Выпуск ещё не допущен: PR остаётся draft, окончательного merge SHA
и его полного CI нет. Правила — в [RELEASES.md](../RELEASES.md), команды и
эксплуатационные границы — в [OPERATIONS.md](../OPERATIONS.md), доступ —
в [PRODUCTION.md](../PRODUCTION.md). Эта запись продолжает
[ревью 4.4](STAGE4_REVIEW.md) после возобновления работы пользователем;
прежняя запись об остановке описывает состояние до возобновления.

## Сверить кандидата и GitHub

Снимок API и worktree получен 02.11.2026 около 11:08–11:13 UTC.

| Проверка | Наблюдение |
| --- | --- |
| Worktree | `ci-release-review-plan/obsChatBot`, ветка `codex/ci-release-review-plan`; до этой записи чистый |
| HEAD и remote PR head | `fd0759147c0647b54bcca5eb0078e757dbed011b` |
| Локальные main/origin/main и актуальный remote main | `9f5b8622169acec640fdf84f1d41d6daa62c6626`; продвижения нет |
| [PR #59](https://github.com/karateka-git/compadre-assistant/pull/59) | Open, draft, mergeable; заголовок/описание ещё описывают преимущественно этапы 1–2 и требуют актуализации |
| [PR fast 41434197082](https://github.com/karateka-git/compadre-assistant/actions/runs/41434197082), attempt 1 | Head `fd075914…`; `pr-fast` success, `ci-metrics` success, `hotfix-deps` skipped (цель main) |
| Полный CI | Для текущего head найден только PR run; допуска будущего merge SHA нет |
| Rulesets | Main/release требуют PR, merge commit и `pr-fast`; 0 обязательных Approve. Защита `v*` от update/delete активна; Immutable Releases enabled |
| Последняя версия | `release/3.0` и `v3.0.1` → `4d58c9c09259f8f9b1578a467b20c5e6055a6bc2`; Release опубликован |
| Новые имена | `release/3.1` и `v3.1.0` отсутствуют среди веток, тегов и Releases |

Предлагается **новая совместимая линия `release/3.1`, версия `v3.1.0`,
`--change minor`**. Добавлены ограниченный runtime-допуск и безопасный UNKNOWN;
публичная схема `0001`–`0004` не меняется. Это новая возможность из main,
а не PATCH поддерживаемой линии. Перед публикацией повторно проверить
свободные имена и состав изменений; номер сейчас является предложением.

## Сверить production без переключения

Read-only SSH-наблюдение начато **02.11.2026 11:11:09 UTC**; SQLite и
controller hashes перепроверены в той же сессии. Содержимое пользовательских
таблиц, конфигурация и секреты не выводились.

| Область | Наблюдение |
| --- | --- |
| Checkout | Чистый detached HEAD `4d58c9c09259f8f9b1578a467b20c5e6055a6bc2` |
| State | `stage=success`, marker отсутствует, `retry_after=null`; исторический `failed_version=v3.0.0` сохраняется |
| Last good | `v3.0.1`, SHA совпадает с checkout; CI run `41000609812`, attempt 1 |
| Два канала | Healthy, restarts 0, policy `unless-stopped` |
| Общий image ID | `sha256:5c2467f184e1dcb51dee7ade66932701cf89408d5e0e8ff39e22c5b7e31f75a3` |
| Controller | Все 35 обычных файлов `scripts/` и `deploy/` совпали по SHA256 с Git-байтами `38bc8411e17d717e954e554f890d599eddd96b96`; лишних файлов вне `__pycache__` нет |
| Wrapper | `/usr/local/sbin/deploy-prod` запускает `/usr/bin/python3 -m scripts.deploy_prod` с PYTHONPATH отдельного snapshot |
| Systemd | Service и timer active/enabled; `NeedDaemonReload=no`; поздний `zz-deploy-pinned.conf`, обе Compose-команды имеют `no-setuid`, User/Group=user |
| Рабочая БД | Read-only `integrity_check=ok`, foreign key violations 0, журнал `0001`–`0004` |
| Daily backup | `/var/backups/obs-chat-bot/app-20261102T031502Z.db`: integrity ok, FK 0, журнал `0001`–`0004` |
| Окно backup | Service inactive/dead, Result=success; следующий timer 03.11.2026 03:15 UTC. Перед действием проверить снова |

Признаков текущего `degraded`, активного deploy или выполняющегося backup нет.
Это не согласование эксплуатационного окна и не подтверждение живых ответов:
polling logs и ответы пользователей в этой проверке не исследовались.

## Получить точный допуск выпуска

Следующие команды — **план**, они не выполнялись. Все Git-действия начинать
с `git status --short --branch`; изменения делать только в worktree этого чата.

1. Актуализировать описание PR и release notes: runtime-допуск закрыт без
   evidence/context; UNKNOWN отказывает безопасно; схема не меняется; новый
   recovery ещё не включён. Зафиксировать нужные документы, согласовать push
   по [AGENTS.md](../../AGENTS.md), дождаться свежего `pr-fast` после нового SHA.
2. Перевести PR из draft и слить **merge commit** после повторного чтения
   head/base/checks/rulesets. Для защиты от смены head использовать
   `gh pr merge 59 --merge --match-head-commit <проверенный-head-SHA>`.
   Сохранить фактический merge SHA как `$releaseSha`: PR head и тестовый
   merge ref не являются его заменой.
3. Получить полный `main-full` именно `$releaseSha`. Если push-run пропустил
   `full-suite`, выполнить `gh workflow run main-full.yml --ref main` после
   сверки tip main. Проверить run/attempt, workflow/ref/SHA и все обязательные
   jobs/steps. Если main сдвинулся, заново выбрать и проверить его tip.
4. Получить commit локально через `git fetch origin main`; подготовить чистый
   checkout этого SHA в worktree задачи, сохранив ветку задачи. Не менять
   основную папку и чужие worktree. До Python-команд проверить project venv:
   локальной `.venv` сейчас нет, доступен интерпретатор ниже; импорт `openai`
   проверен. Тесты runtime повторно не запускались в read-only подготовке.

```powershell
$projectPython = 'C:\Users\compadre\Documents\obsChatBot\.venv\Scripts\python.exe'
# releaseSha — фактический текущий main с успешным полным CI, пока не определён.
$env:GITHUB_TOKEN = gh auth token
& $projectPython -m scripts.create_release_branch release/3.1 $releaseSha --check-only
& $projectPython -m scripts.create_release_branch release/3.1 $releaseSha
# Отдельная разрешённая запись remote ref:
& $projectPython -m scripts.create_release_branch release/3.1 $releaseSha --publish-remote
& $projectPython -m scripts.publish_release v3.1.0 $releaseSha --change minor --notes-file $notesPath --check
& $projectPython -m scripts.publish_release v3.1.0 $releaseSha --change minor --notes-file $notesPath --dry-run
# Только после просмотра notes/check/dry-run и разрешения публикации:
& $projectPython -m scripts.publish_release v3.1.0 $releaseSha --change minor --notes-file $notesPath --publish
```

`$notesPath` должен указывать на [подготовленные notes](RELEASE_V310.md).
Секрет токена не печатать. После публикации повторно сверить
прямой tag, Release target, immutable, ветку и полный CI точного SHA.
Любая частичная запись останавливает процедуру: тег не переносить и не удалять.

## Подготовить отдельное действие на VPS

Старый controller не содержит метрик этапа 2. Установка runtime этапа 4 сама
по себе не требует нового recovery-контракта: штатный запуск остаётся строгим,
новых SQL нет. Но для проверки метрик на следующем выпуске нужен отдельный
controller snapshot из полного проверенного merge SHA. Поэтому предлагаемый
маршрут включает его обновление **до deploy**, по
[порядку snapshot](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot).
Не считать совпадение runtime SHA обновлением controller.

1. После полного CI создать локально `git archive` точного SHA для `scripts/`
   и `deploy/`, manifest SHA256 каждого Git-файла и digest архива. Проверить
   LF, отсутствие symlink/hardlink, синтаксис и imports. Подготовить точные
   staging/backup пути и план атомарной замены для отдельного согласования.
2. После согласования переноса/замены snapshot снова проверить state/marker,
   оба канала, systemd и timer. Сохранить старый snapshot, проверить staging
   и переключить по OPERATIONS; не менять pin, checkout или БД этим действием.
3. После отдельного допуска к установке и согласования окна заново проверить
   Release/CI, snapshot, отсутствие active backup/deploy/degraded и cooldown.
   Выполнить `sudo deploy-prod v3.1.0` только после этих условий.
4. Проверить `success`, отсутствие marker, checkout SHA, OCI revision и image
   ID обоих каналов, `last_good`, backup, SQLite integrity/FK/manifest,
   polling, restart policy, службу и timer; сохранить короткие метрики.
   Получить пользовательское подтверждение команд Telegram/VK отдельно.

Итог 4.5 фиксирует **фактически установленный** SHA/image ID и `last_good`,
который следующая установка выберет прежней целью. Пока установлена v3.0.1,
этап 4 не закрыт. Даже после v3.1.0 production-пары для возврата на будущую
схему остаются неподтверждёнными до harness этапа 5 и контроллера этапа 6.

## Соблюсти границы согласования

В этой подготовке выполнены только чтение GitHub/VPS и локальная запись
этого документа; commit, push, merge, запуск CI, публикация и изменения VPS
не выполнялись. Для Git push AGENTS требует явного согласия. Создание remote
линии/tag/Release и merge входят в предлагаемый внешний маршрут, но здесь
не разрешены поручением на read-only подготовку. OPERATIONS явно требует
отдельного согласования переноса/изменения controller snapshot и отдельного
эксплуатационного допуска/окна перед deploy. Ни зелёный PR, ни публикация
не заменяют эти условия. При отказе сохранить state/marker и следовать
[возврату](../OPERATIONS.md#отказ-выпуска-и-возврат), не стирать cooldown.
