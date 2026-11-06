# Шаг 7.2: проверить snapshot на копии состояния

Запись от 06.11.2026 подтверждает приватную Linux-проверку controller snapshot
и восстановления его каталогов после прерывания процесса. Следующий шаг —
[7.3 в плане](PLAN.md#шаги-этапа-7); исходные копии и ограничения transport
описаны в [7.1](STAGE7_1_PREPARATION.md). Установка рабочего controller
по [OPERATIONS.md](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot)
не выполнялась и остаётся частью 7.7.

## Закрепить проверенные байты

Перед сборкой рабочая копия была чистой, HEAD ветки
`codex/ci-release-review-plan` — `512cb57e8ac80460f71b793fb1a55819244365d9`.
Архивы собраны `scripts.controller_snapshot build` из Git blobs, вне checkout.
Они не включают последующую запись этого отчёта и проверочный сценарий.

| Архив | SHA исходного commit | SHA256 tar |
| --- | --- | --- |
| Candidate, 61 файл и manifest | `512cb57e8ac80460f71b793fb1a55819244365d9` | `977ebea27322293a0d2626ed9d3c233dabaf51b2a7b6e630474730f262214654` |
| Previous для private восстановления | `dd71c2ba42a0744b5b654cedcf73009ce8879058` | `c889525aeaa02edbb5efc4a1d72d0b0b30823a6643f60e57a0b64938a9e24872` |

Локальные файлы: `%LOCALAPPDATA%/Temp/stage7-2-512cb57.tar` и
`%LOCALAPPDATA%/Temp/stage7-2-dd71c2b.tar`. Previous архив собран текущим
сборщиком из прежнего commit; это воспроизводимый private baseline,
а не копирование установленного каталога с его историческими pycache.

Свежий read-only preflight VPS в 14:54:19 UTC подтвердил чистый checkout
previous SHA, прежний digest state, отсутствие marker, service/timer active
и те же два healthy container IDs из 7.1. Разрешённая запись ограничена новым
каталогом:

```text
/var/lib/compadre-assistant-stage7-20261106/snapshot-proof-512cb57-20261106T1500Z
```

Суффикс — идентификатор; фактическая подготовка завершилась в 14:57:18 UTC.
Каталог root:root 0700. В нём сохранены оба tar, автономный `control/` и
четыре независимых сценария `before/`, `gap/`, `after/`, `v2/`.
Каждый содержит собственные checkout/state и каталоги snapshot.
Неизменённый legacy state скопирован из `fixtures/state.legacy.json`;
SHA256 до копирования и после восстановлений:
`fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317`.
Рабочая SQLite и закрытая `source.db` в сценариях не открывались.

## Проверить imports и границы исполнения

[Воспроизводимый сценарий](stage7_2_probe.py) передаёт архивы и проверочный
код через SSH stdin. Каждый удалённый запуск — отдельный
`sudo /usr/bin/python3 -I -B -`; административный Linux Python здесь
проверяет автономный controller, а не заменяет проектное окружение тестов.
Bootstrap проверяет фиксированный путь, владельца и права родителя,
создаёт только новый private root, сверяет внешний SHA256 candidate tar
и загружает guard из его проверенных байтов. До распаковки или импорта
controller установлен `scripts.stage7_transport.install_guard(root)`.

Guard не изменялся в 7.2. Его границы из 7.1 сохраняются: subprocess/socket
и production paths закрыты; это защита доверенного Python-процесса от
случайного внешнего вызова, не OS sandbox против native-кода или root.
Проверочные внешние команды controller вообще не нужны: реальные Host.run,
Docker/systemd, polling и полный deploy CLI не вызываются. `check_imports`,
который создаёт subprocess, внутри guard не используется; вместо этого
каждый новый `-I -B` процесс импортирует проверенный private `control/`.

В 14:57:18 UTC `inspect_archive`/`unpack`/`verify_directory(require_root=True)`
подтвердили manifest, каждый файл, LF, отсутствие links/лишних файлов,
root:root и точные file modes: wrapper 0755, остальные payload files 0644;
каталоги не доступны для чужой записи. Успешно импортированы `deploy_prod`,
`deploy_backup`, `deploy_runtime`, `deploy_schema_admission` и их зависимости.
Пути всех импортированных `scripts.*`/`compadre_assistant.*` проверены внутри
`control/`; SOURCE_ROOT совпал с ним. Release checkout в imports не участвовал.

## Прервать замену и проверить повтор

`current/` играет роль имени установленного каталога **только в private
сценарии**. Замена повторяет два rename из OPERATIONS:
`current → saved`, затем `candidate → current`. Это не одна атомарная
транзакция. Внутри настоящего `deploy_lock` процесс прерывался `os._exit(73)`;
это аварийное завершение процесса без finally, не reboot или power loss.

| Окно | UTC прерывания | Состояние непосредственно после | Два повтора в новых процессах |
| --- | --- | --- | --- |
| До первого rename | 14:57:29 | current=previous, candidate сохранён | 14:57:31/32: previous проверен, повтор идемпотентен |
| Между rename | 14:57:33 | current отсутствует, saved=previous | 14:57:34/35: saved возвращён в current, повтор идемпотентен |
| После второго rename | 14:57:36 | current=candidate, saved=previous | 14:57:38/39: candidate сохранён отдельно, previous возвращён, повтор идемпотентен |
| После второго rename и записи признака v2 | 14:57:40 | current=candidate, saved=previous, protocol_version=2 | 14:57:41/42: оба повтора запретили legacy restore, candidate и saved сохранены |

На каждом повторе реальный flock вновь захватывался на том же inode:
before=508836, gap=508838, after=508840, v2=508842. Второе независимое
открытие lock внутри удерживаемой секции отвергнуто. Lock-файл не удалялся.
После восстановления проверены все байты и права current; legacy state
сохранился побайтно. Каждый повтор дополнительно дважды вызывает процедуру
восстановления, затем второй процесс повторяет ту же проверку.

Признак v2 записан настоящим `write_state` только в четвёртую private копию.
Это намеренно неполное v2-состояние, не evidence успешной миграции или admission.
Запрет восстановления реализован в проверочной процедуре `restore_legacy`
по действующему эксплуатационному правилу: при protocol_version или selector
legacy каталог не выбирается. **Это проверка политики установщика, а не
доказательство, что старый исполняемый controller сам распознаёт v2.** Старый
controller не запускался. Для реальной установки оператор обязан соблюдать
тот же запрет; запуск старого binary в обход процедуры здесь не защищён.

## Проверить marker, lock и настройки backup

На каждой legacy копии реальный `read_state` прочитал stage=success и
сохранил `timer_before`. Private marker заставил настоящий
`deploy_backup.selected_database` отказать до выбора БД и обращения к runtime;
после проверки удалён только этот private marker. На неполном v2-state
настоящий backup также отказал, не вернувшись к legacy пути.

В проверенных candidate bytes backup drop-in содержит `env -i` и
`python3 -B -m scripts.deploy_backup`; timer и backup-service guards содержат
`ConditionPathExists=!/var/lib/compadre-assistant-deploy/deploy-in-progress`.
Проверка относится к содержимому файлов snapshot и private state/lock.
Drop-ins не устанавливались, effective systemd конфигурация не изменялась;
таймер, создание nightly backup и запуск службы кандидата не испытывались.

## Подтвердить рабочий сервис и дальнейший допуск

Итоговый независимый read-only осмотр: 06.11.2026, 14:58:29 UTC.

| Объект | Фактический результат |
| --- | --- |
| Checkout | Чистый, `dd71c2ba42a0744b5b654cedcf73009ce8879058` |
| Installed controller | Те же 38 файлов; inventory digest `dc683481109242bc88eae2b3350c86829930e716b6a6b6b5783420b3f8d795bb`, как в 7.1 |
| Production state | Прежний digest `fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317` |
| Marker / selector / controller.sha | Отсутствуют |
| Service / timer | Оба active/enabled |
| Backup service | inactive/dead, Result=success, NeedDaemonReload=no |
| Telegram | ID `2798069b3f96d3a31c9d3a9d7b12e52c3a754fa4d5348798963209a38869fe42`, running/healthy, restarts=0, unless-stopped |
| VK | ID `31a165fee7911fc6f51690956954c4d6258e433c1f8a9c768295a3cdf6a15a00`, running/healthy, restarts=0, unless-stopped |
| Image обоих | `sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8` |
| Закрытая source.db | Прежний SHA256 `5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c` |

Контейнеров ровно два. Остановка/перезапуск каналов, установка controller,
wrapper/units, изменение production lock/state/marker/DB не выполнялись.
Живые ответы и новые polling logs в этом шаге не проверялись.

Локально `.venv` worktree отсутствует; явный Python основного проектного
окружения проверен импортом unittest/sqlite3. Команда:

```powershell
$env:PYTHONUTF8='1'
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -B -m unittest tests.test_controller_snapshot tests.test_stage7_transport
```

Результат: 8 tests, OK, 7.776 с. Linux-проверка: одна подготовка, четыре
ожидаемых rc=73 и восемь успешных отдельных процессов повтора.
`git diff --check` успешен. Commit/push не выполнялись.

Критерий 7.2 выполнен в описанном private контуре. Это не полный CI,
compatibility evidence или допуск к остановке production. После изменения
кода snapshot в 7.3 нужны архив точного итогового SHA и повторная проверка;
до полного допуска итоговой пары previous/candidate боты остаются включены.
Systemd, реальный runtime/start/health/polling и backup нового выпуска
остаются 7.7. Reboot и power loss не проверены.
