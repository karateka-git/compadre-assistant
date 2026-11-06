# Подготовка шага 7.7: production-выпуск

Этот документ фиксирует read-only осмотр кандидата и VPS перед настоящим
выпуском. Порядок действий задан [планом](PLAN.md), правила публикации —
в [RELEASES.md](../RELEASES.md), установки и возврата — в
[OPERATIONS.md](../OPERATIONS.md). Проверка на копии и восстановление старого
сервиса завершены в [7.6](STAGE7_6_RESTORE.md). Здесь **ничего не установлено**.

## Проверить кандидата и версию

Осмотр 06.11.2026 около 17:54 UTC:

| Предмет | Подтверждено |
| --- | --- |
| Удалённый `main` | `cabc0e24d28947123868db8a60f6b55095feb63e` через `git ls-remote` и `fetch` |
| Установленный previous | `v3.1.0`, `dd71c2ba42a0744b5b654cedcf73009ce8879058` |
| Полный CI | [main-full 42249037867/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42249037867), exact candidate SHA, `completed/success` |
| Compatibility artifact | `13519278785`, `repository-compatibility-42249037867-1`, не истёк, digest `sha256:87a5ab53bcd8b7f57a2012c279872e9252c74ebfc33ddc0f3fbd0099b2e77c6c` |
| Reviewed policy | `eb1dad4fee8a176ec4870db12da5e1051a3d1c6e` |
| Candidate image из 7.3 | `sha256:2025e11b35b9e8898ca41d740d4a10e318e048e91d0eeda04b1000c4db5bc093`; production admission должен повторно сверить image/OCI |
| Controller archive из 7.3 | exact candidate, 63 файла, SHA256 `578f10a40946ae5f55e0ebb1c3f6458a8b7e20e18b8bccbb09bb347bcdbb9aca`; файл на VPS `/var/lib/compadre-assistant-stage7-20261106/admission-cabc0e2-20261106T160953Z/candidate.tar` повторно захеширован |

Удалённой `release/3.2` и тега `v3.2.0` в `git ls-remote` нет. Из проектного
venv выполнен read-only `scripts.create_release_branch release/3.2 <candidate>
--check-only`: допуск прошёл, run/attempt совпали. Предлагается совместимая
новая линия `v3.2.0`, `--change minor`; черновик текста —
[RELEASE_V320.md](RELEASE_V320.md). Перед созданием ветки/публикацией
повторить GitHub ruleset, immutable Releases, вершину main и полный CI.

`publish_release.py` требует чистый checkout с `HEAD` ровно candidate и
удалённой release-веткой того же SHA. Рабочий checkout этой задачи находится
на документационной ветке и **не подходит** для команды публикации. Нужно
использовать отдельный временный чистый checkout exact candidate, не
переключать ветку закреплённого worktree. Файл notes можно передать из
каталога этой задачи. Новых SQL-миграций у пары нет: `0001`–`0004`.

## Сверить VPS перед записью

Read-only SSH 06.11.2026, 17:54–17:55 UTC: checkout чистый, detached previous
SHA. `obs-chat-bot.service` и backup timer active; backup service inactive,
следующий timer 07.11.2026 03:15 UTC. Оба прежних container ID из 7.6
работают healthy, restarts=0, `unless-stopped`, image ID
`sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8`.
State `success`, `last_good` previous SHA; marker, selector и
`recovery-blocked.json` отсутствуют. Production SQLite открыта только на
чтение: `integrity_check=ok`, FK violations=0, migrations 1–4, WAL bytes=0.
Пять исторических release backup имеются; последний v3.1.0 от 02.11,
**свежего backup для окна 7.7 ещё нет**. Свободно около 5.6 GB на `/`.
`NeedDaemonReload=no` для основного и backup service.

Новый `controller.sha`, `compatibility-policy.sha` и `github-read-token`
в приватном state dir отсутствуют. Backup service имеет только
`90-deploy-guard.conf`; новый `zz-deploy-selector.conf` не установлен.
Основной service использует старый `zz-deploy-pinned.conf`; его нужно заменить
проверенными байтами `obs-chat-bot-selector.conf`. Timer/backup guards и
effective D-Bus units следует повторно сверить непосредственно перед
установкой, включая `ExecStartEx`, `ExecStopEx`, Conditions и порядок drop-ins.
Наличие старых живых контейнеров не доказывает запуск новой версии.

Локальный `gh` OAuth token имеет scopes `admin:public_key`, `gist`, `read:org`,
`repo`. Он годится для локальной публикации, но имеет широкое право записи
и **не должен автоматически становиться production `github-read-token`**.
Для VPS нужен отдельный действующий read-only credential к GitHub API,
переданный без вывода значения и сохранённый root:root `0600` в
`/var/lib/compadre-assistant-deploy/github-read-token`. Аналогично
`compatibility-policy.sha` должен содержать точный reviewed SHA и иметь
root:root `0600`; controller SHA записывается только после проверки
установленного snapshot. Доступность отдельного read-only token пока
**не подтверждена**: это блокер production admission, а не повод применять
широкий локальный token без решения владельца.

## Выполнить после review подготовки

1. Подтвердить независимый критерий 7.6 и остаточный риск: reboot/power loss
   не проверялись, ручные writers запрещены на всё окно. Повторить свежий
   VPS baseline: checkout/state/marker, сервисы, контейнеры, image/OCI,
   backup, Docker mounts, место и SQLite без вывода строк.
2. В чистом временном checkout candidate повторить `create_release_branch
   release/3.2 <candidate> --check-only`, создать локальную и удалённую
   ветку штатным скриптом, сверить SHA. Затем `publish_release.py v3.2.0
   <candidate> --change minor --notes-file <RELEASE_V320.md> --check`,
   `--dry-run`, review текста, `--publish`. Подтвердить immutable Release,
   прямой tag SHA, ветку и полный run. Если tag создан, а Release нет,
   не повторять вслепую и не двигать tag: разобрать частичный результат.
3. Под существующим `deploy.lock` и без активного deploy перенести/использовать
   exact архив, сверить digest и каждый файл с manifest/Git, распаковать
   `controller_snapshot unpack --sha <candidate> --archive <tar> --digest
   <digest> --directory <new-root-owned-staging> --require-root`. Сохранить
   старый controller/wrapper/pin, переключить два каталога и проверить новый
   `controller_snapshot verify`, isolated imports и owner/mode. Установить
   проверенный wrapper и атомарно записать `controller.sha` root:root `0600`
   с fsync. До проверки не трогать checkout/image/БД/state/marker.
4. Подготовить отдельный read-only GitHub token и reviewed policy pin как
   внешние root:root `0600` файлы без записи значения в протокол. Установить
   exact selector drop-ins основного и backup service, сохранив оба marker
   guards. Выполнить `daemon-reload`, `systemd-analyze verify` и D-Bus
   проверку effective commands, flags, Conditions, `NeedDaemonReload=no`.
   После смены unit до deploy не делать непроверенный restart старой службы.
5. Свежий read-only допуск опубликованного Release, exact CI/compatibility,
   previous/image/OCI и backup. Проверить отсутствие сторонних Docker/SQLite
   writers и активного backup; соблюдать окно без ручного CLI/compose run.
   Затем единожды вызвать штатное `sudo deploy-prod v3.2.0` без инъекций.
6. Если команда успешна, независимо проверить installed SHA, выбранную БД,
   integrity/FK/schema и логическую сохранность исходных данных; новый
   backup, state/marker/lock, оба exact image IDs/OCI, health/restarts,
   service/timer и ограниченные polling signals. Записать длительность и
   простой. Реальные ответы Telegram/VK подтверждает пользователь отдельно.
   При `failed_preflight` убедиться, что прежние каналы работают; при
   `degraded` перейти к [процедуре recovery](../OPERATIONS.md#отказ-выпуска-и-возврат),
   не объявлять успех и не возвращать legacy controller после v2 state.

Результат каждого действия и независимой приёмки сохранить в этом каталоге
без token, `.env`, строк БД, полных logs/inspect. Пока нет отдельного read-only
GitHub credential, этап 7.7 нельзя довести до production admission.
