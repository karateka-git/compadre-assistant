# Production VPS

Здесь находятся доступ, ограничения и **последний документированный снимок** VPS.
Он зафиксирован 14.11.2026. Перед
любой работой повторно проверьте факты на VPS. Установка, диагностика, backup и
восстановление описаны в [OPERATIONS.md](OPERATIONS.md), допуск версии — в
[RELEASES.md](RELEASES.md).

## Доступ

- VPS: `user@91.188.213.142`; приложение находится в `/opt/obs-chat-bot`.
- Закрытый SSH-ключ на текущем рабочем компьютере:
  `C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka`.
  Публичная часть ключа — тот же путь с суффиксом `.pub`.
- Ключ не копировать в репозиторий, чат или логи. Для другого компьютера
  создать или безопасно получить отдельный ключ и добавить его в H3LLO.
- Для чтения кода на VPS используется отдельный GitHub deploy key
  `/home/user/.ssh/obs-chat-bot_deploy` с доступом только на чтение к
  `karateka-git/compadre-assistant`.

```powershell
ssh -i "C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka" user@91.188.213.142
```

Для ручного выпуска из GitHub Actions нужен выделенный SSH-вход runner с
отдельным ключом и заранее сверенным ключом VPS. Ограничьте этот вход
штатным root-wrapper `/usr/local/sbin/deploy-prod`. Через `sudo -n` разрешите
только обычную установку по версии, `--verify-installed SHA` и
`--cleanup-old SHA RUNTIME_ID` с точной версией. Произвольный shell, `env`,
Python и другие режимы контроллера этому входу не предоставляйте. Перед
первым запуском сверяйте права и установленный controller snapshot с
[инструкцией GitHub Actions](OPERATIONS.md#выпустить-обычную-версию-из-github-actions).

## Последний проверенный снимок 14.11.2026

| Область | Наблюдение 14.11.2026 |
| --- | --- |
| Установленный выпуск | `v4.0.0`, SHA `119e36d2bdde0162b970c81cdc980d61dec0cc9c`. Immutable Release `445221675`, опубликован в 10:47:06 UTC. Свежий `verify-installed` подтвердил тот же выпуск. [Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v4.0.0) |
| База CI | Deployment `7786489171`, текущий статус `22189527386` (`success`), полный CI `main` `43586230283`, попытка 1. При создании релизной ветки контроллер `43586749622` подтвердил допуск по полному CI того же SHA в `main`. Задание полного CI в релизной ветке пропущено. [Deployment и его статус](https://api.github.com/repos/karateka-git/compadre-assistant/deployments/7786489171/statuses) · [CI main](https://github.com/karateka-git/compadre-assistant/actions/runs/43586230283/attempts/1) · [Контроллер релизной ветки](https://github.com/karateka-git/compadre-assistant/actions/runs/43586749622/job/132422802704) |
| SQLite и backup | Поколение `1ff4727c3f834b98a058065b107c7bb3`, последняя миграция `0005_ask_usage.sql`. Активная SQLite, selector и финальный snapshot сохранились. Контроллер записал очистку прежней SQLite и её сопутствующих файлов. Штатный backup выбранного поколения завершился успешно в 10:53:15 UTC. |
| Каналы и systemd | Основной unit и backup timer `active`. Оба контейнера `healthy`, перезапусков ноль, polling подтверждён. Docker restart policy `no`, запуском управляет systemd с selector. |
| Пользовательская приёмка | Ask включён, проверка Ask и OCR в обоих личных чатах запрошена. Admin выключен. Штатные настройки Timeweb и список доверенных ещё не заданы. |

Штатный workflow подтвердил Deployment в 10:52:02.6534274 UTC.
Перед следующим эксплуатационным действием повторите проверку VPS.
[Порядок проверки](OPERATIONS.md#подготовка-и-запуск-версии) ·
[Метрики выкладки](DEPLOY_METRICS.md#разбивка-по-этапам).

## Исторический снимок 13.11.2026

При разовом переходе на policy v2 контроллер VPS подтвердил evidence полного
CI на актуальной принятой базе `v3.4.0`. Обновили snapshot контроллера и
локальный policy pin. Checkout приложения сохранили, а state и selector
SQLite сверили побайтно. Установку приложения не повторяли.

| Область | Наблюдение 13.11.2026 |
| --- | --- |
| Установленный выпуск | `stage=success`, `last_good=v3.4.0`, SHA `22c01936c58d0cd7c99df334465f4562f9e3ca4e`. Checkout чистый, detached HEAD, marker отсутствует. [Immutable Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v3.4.0) |
| База CI | Deployment `7736620585` и его текущий доверенный статус `22029591001` (`success`) совпали с `last_good` и Release `442821807`. [Статусы Deployment](https://api.github.com/repos/karateka-git/compadre-assistant/deployments/7736620585/statuses) |
| Контроллер и policy | Snapshot `792ae2fd81ce20a7e61ce9d27dc30451c6d19e0e`. GitHub variable `COMPATIBILITY_POLICY_SHA` и локальный `compatibility-policy.sha` закреплены на `9845ca2a106494c660df943c8d803da6e421ca03`. Проверены байты 71 файла snapshot, изолированный импорт и systemd guards. |
| Полный CI | `main` на SHA `792ae2fd81ce20a7e61ce9d27dc30451c6d19e0e` и `release/3.4` на SHA `efe22a4f019e68a0436ee3d5e1825ab9248934e0` подтвердили базу `v3.4.0`. VPS проверил артефакт `14014894610` для точной попытки `main`. [CI main](https://github.com/karateka-git/compadre-assistant/actions/runs/43378302919/attempts/1) · [CI release/3.4](https://github.com/karateka-git/compadre-assistant/actions/runs/43378457266/attempts/1) |
| Каналы и backup | Оба контейнера Telegram и VK `healthy`, перезапусков 0. Основной unit и backup timer `active`, backup service `inactive` с результатом `success`. |

Перед следующим эксплуатационным действием повторите
[проверку состояния VPS](OPERATIONS.md#подготовка-и-запуск-версии).

## Исторический снимок — 06.11.2026, после 20:17:13 UTC

Штатный `deploy-prod v3.2.0` завершился `degraded` при возврате основного
systemd unit: Python загрузил `scripts` из release checkout вместо
установленного controller snapshot. После исправления unit отдельная root-only
команда forward recovery завершила тот же выпуск на выбранной новой SQLite.
[Итог проверки](RELEASE_REVIEW.md) отделяет отказ исходной команды от
успешного восстановления.

| Область | Последнее наблюдение |
| --- | --- |
| Durable state | `success`, `last_good=v3.2.0` на SHA `cabc0e24d28947123868db8a60f6b55095feb63e`; marker отсутствует. |
| SQLite | Selector указывает на новое поколение. Прежняя и новая БД имеют `integrity_check=ok`, FK 0, совпадающие fingerprint и logical digest до и после recovery; строковые данные не выводились. |
| Каналы | Те же candidate TG/VK ID `running` и `healthy`, restarts 0, restart policy `no`; polling signals обоих каналов подтверждены в ограниченном окне. Пользователь отдельно подтвердил ответы `/help` в Telegram и VK. Старые боты не запущены. |
| systemd и backup | Основной service и backup timer `active/enabled`; backup service `inactive`. Final snapshot backup цел, root/0400. |
| Контроллер | Установлен snapshot `aeeb73c557face466ce411b8c22a4aad04750861`; exact main/backup selector units с `python3 -P`. `require_infrastructure` прошёл. |

Дополнительное чтение VPS 07.11.2026 в 06:07:35 UTC подтвердило checkout
`cabc0e24d28947123868db8a60f6b55095feb63e`, active main service/timer
и оба тех же TG/VK ID `healthy` с restarts 0. Оно не повторяло проверку
SQLite, selector и durable state из снимка 06.11.2026.

Повторный read-only допуск к recovery выявил лимит анонимного GitHub API в
Release selector. Исправленный controller использует приватный read-only token;
оно прошло отдельный CI и Linux-проверку до единственного запуска recovery.
Результат восстановления проверен независимо; пользователь подтвердил
живые ответы `/help` в обоих каналах без записи содержимого ответов и ID чатов.

Ротация базы полного CI после этого выпуска была отменена: repository variable
`COMPATIBILITY_POLICY_SHA` возвращена к `09f15a5b3c01ec73d02379b918104e8cf697b4b2`,
хотя на VPS уже работала v3.2.0. Успешный [main-full 42344876362/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42344876362)
проверил SHA `a44cd3c73afefc30b832051fc95fff0e5555142a` после возврата policy.
Для следующего deploy от установленной v3.2.0 требовался новый точный CI.
Действующий порядок описан в
[допуске полного CI](RELEASES.md#2-получить-полный-ci-для-точного-коммита) и
[инструкции выпуска](OPERATIONS.md#выпустить-обычную-версию-из-github-actions).
Reboot и потеря питания VPS не проверялись.

Перед дальнейшими действиями повторно прочитать фактический state, selector,
marker, контейнеры и timer на VPS: эта запись не является новым допуском.

## Исторический снимок — 02.11.2026, 11:28:58 UTC

| Область | Результат проверки |
| --- | --- |
| Версия и допуск | `v3.1.0`, SHA `dd71c2ba42a0744b5b654cedcf73009ce8879058`; [GitHub Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v3.1.0), immutable. Полный `main-full` точного SHA: [41436078724/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41436078724/attempts/1), success. |
| Установка | Обычный `deploy-prod v3.1.0` завершился success в 11:28:22 UTC. Marker отсутствует, checkout чистый detached HEAD; `last_good` содержит v3.1.0 и точные SHA/image ID/CI. |
| Контроллер | Отдельный snapshot обновлён до `dd71c2ba42a0744b5b654cedcf73009ce8879058`: 38 файлов сверены по Git-байтам/SHA256 до и после замены. Прежний сохранён в `/usr/local/lib/compadre-assistant-deploy.saved-38bc841-20261102`. |
| Каналы и образы | Оба healthy, restarts 0, policy unless-stopped; image ID `sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8`, OCI revision совпадает с SHA. Polling signals обоих каналов подтверждены без traceback/conflict в проверенном окне; дублей нет. |
| Проверки запуска | Обе `verify_both` success: 27.780 с и 27.742 с. Затем независимая проверка pin/systemd/health/duplicates прошла. |
| Рабочая БД | Integrity ok, FK violations 0, журнал `0001`–`0004`; новых SQL не было. |
| Backup установки | `/var/lib/compadre-assistant-deploy/backups/v3.1.0-dd71c2ba42a0744b5b654cedcf73009ce8879058.db`, mode 0600, integrity ok/FK 0, схема `0001`–`0004`. Старые backup не удалялись. |
| Systemd | Service и backup timer active/enabled; backup service inactive, Result=success. |
| Метрики | Полная команда 129.977 с, простой по контроллеру 89.938 с, downtime_open=false; приватный файл `metrics-ec26b90e5eff4ef38b424578051ac3ee.json` в каталоге состояния. |
| Пользовательская приёмка | Живые ответы Telegram/VK после v3.1.0 ещё не подтверждены пользователем. Healthy и polling не заменяют эту проверку. |

Независимый осмотр начат 02.11.2026 в 11:28:58 UTC и завершён около 11:29 UTC.
На тот момент базовая версия стала установленной целью следующего обновления;
совместимость с будущей схемой и новый recovery ещё не были доказаны.

## Исторический снимок — 30.10.2026, 04:33:27 UTC

| Область | Результат проверки |
| --- | --- |
| Версия и допуск | `v3.0.1`, SHA `4d58c9c09259f8f9b1578a467b20c5e6055a6bc2`; [GitHub Release](https://github.com/karateka-git/compadre-assistant/releases/tag/v3.0.1). `release-full` завершился успешно для точного SHA, [run 41000609812](https://github.com/karateka-git/compadre-assistant/actions/runs/41000609812). |
| Установка и контроллер | `stage=success`, marker отсутствует, checkout — чистый detached HEAD `4d58c9c09259f8f9b1578a467b20c5e6055a6bc2`. Установленный controller snapshot `38bc8411e17d717e954e554f890d599eddd96b96` не менялся; 35 файлов совпали с candidate. |
| Каналы и образы | `tg_assistant` и `vk_assistant` — `healthy`, restart count 0, policy `unless-stopped`, image ID `sha256:5c2467f184e1dcb51dee7ade66932701cf89408d5e0e8ff39e22c5b7e31f75a3`; polling обоих каналов работает. |
| Инцидент и восстановление | `v3.0.0` перешёл в `degraded` из-за таймаута Docker healthcheck в 5 секунд. Выполнено ручное восстановление вперёд на `v3.0.1`; это recovery-процедура, а не обычный `deploy-prod`. Холодный Telegram healthcheck завершился с exit code 0 за 7.407 секунды, что подтверждает, что лимита 5 секунд недостаточно для этого сценария запуска. |
| Рабочая БД | `integrity_check=ok`, `foreign_key_check` пуст; применены `0001_initial_schema.sql`–`0004_private_chat_address_lookup.sql`. |
| Backup до миграции | `/var/lib/compadre-assistant-deploy/backups/v3.0.0-523ce4c595e7fe87771a10752944358a19c3bed8.db`; mode `0600`, `integrity_check=ok`, `foreign_key_check` пуст, схема содержит `0001`–`0002`. |
| Backup recovery | `/var/lib/compadre-assistant-deploy/backups/recovery-v301-20261030T043039155240Z.db`; mode `0600`, `integrity_check=ok`, `foreign_key_check` пуст, схема содержит `0001`–`0004`. Обе копии сохранены. |
| systemd | `obs-chat-bot.service` — `active/enabled`; `obs-chat-bot-backup.timer` — `active/enabled`. |
| Пользовательская приёмка | Отдельно от серверных наблюдений пользователь подтвердил `/help` и меню в личных диалогах Telegram и VK. Содержимое ответов и временные метки сообщений не записывались. |

Серверная часть снимка наблюдалась 30.10.2026 в 04:33:27 UTC после ручного
восстановления. Инцидент `v3.0.0` завершился продвижением вперёд на `v3.0.1`,
без возврата к старой схеме; backup до миграции и recovery backup сохранены.
Пользовательская приёмка выше приведена отдельно и не расширяет серверное
наблюдение. Технические команды и условия повторной проверки — в
[OPERATIONS.md](OPERATIONS.md); обычный допуск и `deploy-prod` описаны в
[RELEASES.md](RELEASES.md).

## Исторический снимок — 21.10.2026

На тот момент работал `v2.0.0` (`0d21ef0172ecf8024bd6c1583a8046fda798aec3`),
SQLite содержала миграции `0001`–`0002`, а backup перед `0002` сохранял схему
`0001` и проходил `integrity_check`. Controller snapshot был собран из SHA
`38bc8411e17d717e954e554f890d599eddd96b96`; полный CI релизной линии прошёл
[run 39601345018](https://github.com/karateka-git/compadre-assistant/actions/runs/39601345018).
Пользователь подтвердил личный выбор, связанные каналы, второй профиль,
долгий Ask и позднее сохранение режима после повторного включения Obsidian.
Тексты и временные метки ответов независимо не наблюдались; доступность
прежней статьи также не проверялась. Этот исторический результат не описывает
текущее состояние VPS.

## Базовые команды

```bash
cd /opt/obs-chat-bot
sudo systemctl status obs-chat-bot.service
sudo systemctl is-active obs-chat-bot-backup.timer
docker compose ps
docker compose ps -q | xargs -r docker inspect --format '{{.Name}} {{.State.Status}} restarts={{.RestartCount}}'
```

Для healthcheck текущей сборки:

```bash
cd /opt/obs-chat-bot
docker compose run --rm -T --no-deps tg_assistant python -m compadre_assistant --healthcheck --channels telegram,vk </dev/null
```

Обычное обновление выполнять на конкретный опубликованный SHA с планом
сохранения данных; `git pull` на текущем detached HEAD не использовать.
Проверить оба канала, polling, журналы, `RestartPolicy.Name=unless-stopped`,
SQLite integrity и backup timer; для реальных ответов требуется
пользовательская проверка. Полные logs и inspect не публиковать.

## Ограничения для работы с VPS

- Перед изменением проверить `git status`, systemd, Docker Compose и
  состояние контроллера. Согласовать эксплуатационное действие с пользователем
  и проверить результат после выполнения. Согласование ручного запуска
  обычного выпуска покрывает предусмотренную этим запуском техническую
  очистку прежнего поколения после подтверждения перехода на новую SQLite.
- Не выводить и не передавать `.env`, PEM, SSH private key, API tokens или
  содержимое production SQLite.
- Не удалять активную production SQLite и не применять к ней правила очистки
  development-БД. Единственное узкое исключение — зарегистрированное прежнее
  поколение после завершённой установки, свежей технической приёмки того
  же runtime и проверки backup. Штатный workflow записывает Deployment
  после завершения очистки и повторной сверки приёмки.
  [Процедура очистки](OPERATIONS.md#удалить-прежнее-поколение-sqlite-после-приёмки)
  Вручную удалять файлы, менять selector или удалять backup нельзя. Изменение
  схемы требует новой миграции с сохранением данных.
- Перед `git push` требуется явное согласие пользователя.
