# Установить базовую версию: результат шага 4.5

02.11.2026 установлен `v3.1.0`: базовый runtime стал фактической прежней
версией для следующего обновления. Выпуск прошёл обычный полный допуск,
контроллер обновлён отдельно, данные сохранены. Подготовка — в
[STAGE4_RELEASE_PREP.md](STAGE4_RELEASE_PREP.md), правила — в
[RELEASES.md](../RELEASES.md), актуальный снимок — в
[PRODUCTION.md](../PRODUCTION.md). Живые ответы Telegram/VK ещё требуют
пользовательской проверки; этапы 5–6 не выполнялись.

## Подтвердить выпуск точного SHA

| Событие | Подтверждение |
| --- | --- |
| Финальный PR head | `14a25b9f0d59284178525b184212712dc14b38b7`; notes уточнены: pip cache не дал ускорения, установка зависимостей сохранена |
| Короткая проверка | [41435601416/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41435601416/attempts/1), success; `pr-fast` success |
| Merge | [PR #59](https://github.com/karateka-git/compadre-assistant/pull/59), merge commit `dd71c2ba42a0744b5b654cedcf73009ce8879058`, 11:19:57 UTC; expected head сверён, bypass не использовался |
| Push main-full | [41435979902/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41435979902/attempts/1), full-suite skipped; не использован как допуск |
| Полный допуск | Ручной [41436078724/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41436078724/attempts/1), exact merge SHA, completed success; все обязательные jobs/steps выполнены |
| Линия и публикация | `release/3.1` и прямой `v3.1.0` tag → тот же SHA; [immutable Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v3.1.0), ID `431378458` |
| Проверки скриптов | `create_release_branch --check-only`, `publish_release --check` и `--dry-run` прошли до записи; после публикации `select_deploy_release` повторно подтвердил Release/SHA/CI |

Main не сдвинулся между выбором кандидата и созданием линии. Версия — новая
совместимая MINOR-линия; миграции `0001`–`0004` не изменены, новых SQL нет.
Продолжение шага выпуска/установки и ранее разрешённый push подтверждены
контекстом поручения пользователя; отдельные действия публикации, обновления
snapshot и deploy выполнены последовательно после проверок.

## Подтвердить отдельный controller snapshot

Архив создан `git archive` из merge SHA, а не из Windows checkout.
Все 38 обычных файлов `scripts/` и `deploy/` сверены с точными Git-байтами;
SHA256 архива:
`a79bd1cbf358444713f815d2eeb4b465126ffb2ae0544f9a669b368e2d65068b`.
Проверены типы записей, пути, отсутствие CR в Python/shell, Python AST,
shell syntax, imports, wrapper и соответствие действующему pin/systemd.

Перед записью в 11:25:06 UTC повторно подтверждены чистая v3.0.1,
`success`, отсутствие marker/cooldown, два healthy-канала и неработающий
backup service. Следующий backup был 03.11 в 03:15 UTC. Переключение
выполнено под существующим `deploy.lock`, без Python optimization.
Прежние 35 файлов до этого совпали с `38bc8411e17d717e954e554f890d599eddd96b96`.
Старый каталог сохранён как
`/usr/local/lib/compadre-assistant-deploy.saved-38bc841-20261102`.

Новый snapshot: `dd71c2ba42a0744b5b654cedcf73009ce8879058`. После замены
повторно проверены все 38 хешей и imports/pin/systemd. `state.json`,
runtime pin, Compose overrides и приложение при этой операции не изменились.
Метрики этапа 2 появились именно в отдельном установленном контроллере.

## Подтвердить установку и цель следующего возврата

Свежий predeploy в 11:25:52 UTC вновь подтвердил исходное безопасное состояние
и свободное окно backup. Отдельная команда `sudo deploy-prod v3.1.0`
завершилась с exit code 0 и `success` в **11:28:22 UTC**.
Независимая read-only проверка начата в **11:28:58 UTC**, завершена около
11:29 UTC. Первый диагностический heredoc имел ошибку завершающей строки
после чтения результатов; повтор через прямой Python stdin завершился успешно.
Это ошибка диагностической оболочки, не контроллера и не приложения.

| Область | Результат |
| --- | --- |
| Checkout | Чистый detached HEAD `dd71c2ba42a0744b5b654cedcf73009ce8879058` |
| Last good | `version=v3.1.0`, тот же SHA, CI `41436078724/1`, kind ordinary |
| Image ID обоих каналов и last_good | `sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8` |
| OCI revision и pin | Revision совпал с SHA; `compadre-assistant:release-dd71c2ba42a0744b5b654cedcf73009ce8879058` указывает на тот же image ID |
| Каналы | Оба healthy, restarts 0, policy unless-stopped; polling signals есть, traceback/conflict в проверенном окне нет, дублей polling нет |
| Контроллер | `success`, marker отсутствует; независимые `installed`, `verify_installed`, duplicate-polling checks успешны |
| Systemd/backup | Service и timer active/enabled; backup service inactive, Result=success |
| Рабочая БД | Integrity ok, FK violations 0, журнал `0001`–`0004` |
| Backup установки | `/var/lib/compadre-assistant-deploy/backups/v3.1.0-dd71c2ba42a0744b5b654cedcf73009ce8879058.db`, mode 0600, integrity ok/FK 0, журнал `0001`–`0004` |

Критерий установленной цели доказан сочетанием `last_good` с действующими
checkout, pin и image ID двух контейнеров. При следующем deploy контроллер
прочитает текущие SHA/image как `old_sha`/`old_image_id`; сейчас в завершённой
записи эти поля закономерно ещё описывают исходную v3.0.1. Их не переписывали
ради отчёта. Это не испытание реального rollback и не допуск v3.1.0 к любой
будущей схеме: список подтверждённых production-пар остаётся пуст до этапов 5–6.

## Сохранить измерения выпуска

Источник deploy — приватный файл
`/var/lib/compadre-assistant-deploy/metrics-ec26b90e5eff4ef38b424578051ac3ee.json`:
`operation=deploy`, `result=success`, SHA и run/attempt совпадают с выпуском.

| Этап | Секунды |
| --- | ---: |
| Полная команда deploy, 11:26:12.467–11:28:22.444 UTC | 129.977 |
| Простой по контроллеру, downtime_open=false | 89.938 |
| Build | 11.405 |
| Остановка обоих каналов | 12.820 |
| Backup | 1.244 |
| Migrations, без новых SQL | 3.512 |
| verify_both 1 | 27.780 |
| verify_both 2 | 27.742 |

CI artifact `ci-metrics-41436078724-1` прочитан: проверочный путь full-main
153 с от начала run до конца full-suite; full-suite job 134 с, unittest
63 с, install 12 с, три smoke 4 с, image build/healthcheck 47 с.
Весь run по API занял 170 с (11:20:41–11:23:31), включая очередь и
отчётный job. Artifact сравнивает full-main с `39380218769/1`: +81 с;
это сравнение одного типа, но не доказательство причины изменения времени.
Новый collector реально выполнился и загрузил artifact. PR collector до merge
оставался недоступен в доверенном base; зелёный его job не означал сбор метрик.

Сравнение с v3.0.0 нельзя трактовать как ускорение одного изменения:
тогда были новые миграции и отказ healthcheck, сейчас штатный выпуск без
новых SQL. Расход Codex tokens недоступен и остаётся `unknown`.

## Завершить техническую часть этапа

Локальный и полный CI допуск 4.4 подтверждены; технический критерий 4.5
выполнен: базовая версия установлена и подтверждена текущей целью следующего
обновления. Пользовательская проверка живых ответов TG/VK ожидается отдельно.
Этап 5 не начат; новый автоматический recovery не заявляется. Временный
каталог всей функциональности и открытый пункт бэклога сохраняются.
