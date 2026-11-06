# Шаг 6.4. Восстановить runtime после прерывания

Локальная реализация, проверенная 06.11.2026, добавляет возврат и повтор команды к
[переключению 6.3](STAGE6_SWITCH_IMPLEMENTATION.md) по
[протоколу 6.1](STAGE6_DB_SWITCH.md). Код находится в
[deploy_database_switch.py](../../scripts/deploy_database_switch.py) и
[deploy_prod.py](../../scripts/deploy_prod.py). **Безусловный rollout gate
сохранён**: это не установленный controller snapshot и не эксплуатационная
приёмка. Действующий VPS не менялся; его команды остаются в
[OPERATIONS.md](../OPERATIONS.md).

## Определить допустимое поколение

Под внешним deploy lock сначала поставить marker, отключить backup timer,
остановить systemd service и запретить Docker restart. Для остановки не
требуются читаемые state, selector или runtime.env: контроллер перечисляет
ID по Compose labels рабочего каталога и двух каналов. Для каждого ID
независимо пытается выполнить update restart=no и stop, даже при ошибке
предыдущей защиты. Затем требуется подтверждённая quiescence.

| Durable состояние | Возврат |
| --- | --- |
| `false`, selector old | Проверить идентичность old и прежний локальный OCI pin; вернуть прежний runtime на old. |
| `false`, selector new и точный switch intent | Сохранить recovery intent new → old, атомарно вернуть selector old и запустить прежний runtime на old. |
| `true`, selector new | Сохранить new со всеми пользовательскими записями; повторно проверить точный Release/CI artifact, previous SHA и локальный OCI pin; запустить прежний runtime на new. |
| Повреждённая/неоднозначная граница, чужой selector, пропавшая БД, недоказанный image/path/ID, несовпадение evidence либо API outage | `degraded`, marker остаётся, запуска нет; обе БД и доказательства сохраняются для отдельного разбора. |

После `true` контроллер не восстанавливает snapshot поверх new, не выбирает
old как «более целую» БД и не выполняет down-миграции. Проверяются обе
зарегистрированные идентичности файлов: канонический путь, generation,
device/inode, manifest, fingerprint, integrity и foreign keys. Aliases
основного файла и существующих WAL/SHM/journal запрещены. Содержимое SQLite
и sidecars не переписывается и не удаляется recovery.

Наблюдаемый прежний runtime на old после durable `true` также запрещает
восстановление, даже если selector правильно указывает new. Достаточно одного
такого канала: оба writer останавливаются, selector и поколения сохраняются,
повтор остаётся в `degraded`. Неизвестная stage не разрешает recovery;
допускаются только явно перечисленные незавершённые стадии протокола.

## Проверить старый runtime на новой БД

Сохранённого `compatibility_evidence` недостаточно. Перед подготовкой
возврата и ещё раз непосредственно перед start контроллер заново выбирает
опубликованный candidate Release, сверяет все поля selected Release,
включая SHA, Release ID, run/attempt и kind, затем вызывает
`verify_evidence` с `previous_sha=state.old_sha` и внешним policy pin.
Artifact ID/digest/policy/fingerprint должны совпасть с durable записью и
fingerprint выбранной новой SQLite. Сбой API, истёкший или отозванный
artifact закрывают возврат. Token и содержимое evidence не выводятся.

Прежний tag должен быть каноническим `pinned_tag(old_sha)` и разрешаться
ровно в `old_image_id`; OCI revision проверяется и по tag, и по immutable ID.
После возврата checkout производный runtime.env закрепляет этот pin и
явный DATABASE_PATH. Compose создаёт оба контейнера без start; существующие
проверки сверяют IDs, image, env, mount, restart=no и отсутствие других
polling с теми же credentials. Start получает только проверенные IDs.

## Повторить прерванное восстановление

Recovery сохраняет `recovery_intent`, `recovery_selected`,
`recovery_prepared` с конкретными IDs и `recovery_verified`. Intent привязан
к transition, from/to generation и прежнему SHA/image. После обратного
selector replace повтор принимает уже выбранную old, сохраняя исходный
intent. Admission никогда не сбрасывается. Перед recovery читается именно
durable state, а не оставшаяся после ошибки записи память процесса.

При повторе существующая пара сначала останавливается и только затем
создаётся одна новая проверенная пара. Поэтому повтор может менять IDs и
перезапускать каналы, но не запускает вторую параллельную пару. Даже после
`recovery_verified` marker, restart=no и остановленный timer сохраняются;
команда завершается закрытым gate 6.5. Штатный boot/finalization пока не
подготовлен, поэтому этот stage не называется `rolled_back`.

Прерванная первая регистрация завершается только при точном durable
`registration_intent`, false, отсутствии new и подтверждённых old/runtime.
Прерванная смена transition ID завершается только при совпадении фактического
selector с сохранённым `selector_rebind.from`, тем же поколении и точном
`to`. В остальных случаях отсутствующий selector не означает `app.db`.

Если `compose create --force-recreate` успел удалить один канал, raw inventory
может содержать неполную пару. Возврат допускается только при durable false,
стадии до возможного start и отсутствии прежней записи `recovery_containers`.
Каждый присутствующий container должен быть прежним runtime на old либо
точным candidate на new с нулевым `StartedAt` и без рестартов. После создания
восстановленной пары полные проверки и поиск двойного polling обязательны.
Пропавший канал после true или durable разрешения recovery start оставляет
неоднозначную историю и переводит восстановление в `degraded`.

## Разобрать повреждённый state

Обе entry points обрабатывают невалидный JSON, повторные ключи, неверный
корень и отсутствие state при следах перехода под lock. Они сохраняют
исходные байты `state.json`, пытаются закрыть каждый известный writer и
пишут отдельный `recovery-blocked.json` только со статическим кодом
`STATE_UNREADABLE`. Повтор снова ставит защиты и отказывает без выбора БД.
Не удалять этот файл или marker для обхода: нужен отдельный разбор фактов
и восстановление доверенного state. Если Docker/systemd недоступны,
остановка не объявляется подтверждённой; новый start всё равно запрещён.

## Воспроизвести проверки

Worktree `.venv` отсутствует; проверены импорты `unittest`, `sqlite3`, `yaml`
в проектном окружении основной папки. Из корня worktree:

```powershell
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -m unittest tests.test_deploy_database_recovery tests.test_deploy_database_switch tests.test_deploy_prod tests.test_deploy_final_snapshot tests.test_predeploy_compatibility tests.test_migration_runner tests.test_runtime_schema
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -m unittest tests.test_deploy_prepare tests.test_deploy_metrics
```

Новые tests используют временные SQLite и реальный committed WAL, файловые
state/selector и fake Docker/systemd транспорт. Проверены все pre-admission
стадии, обе стороны обратного selector replace, durable recovery стадии,
окна после create/start, повтор команды, частичный create, ошибки
pin/evidence/API/DB/selector и независимые попытки остановки каналов.
Отдельно проверены сохранение новых WAL-записей, повторная проверка artifact
перед start и сохранение повреждённого JSON без вывода его содержимого.
Регрессионные проверки отдельно подтверждают отказ old/old после `true`
для каждого канала, сохранение обоих файлов и selector при повторе, а также
отказ неизвестной стадии.

Результат 06.11.2026: первый набор — 98 tests, `OK` (1 Linux-only skip);
подготовка и метрики — 18 tests, `OK` (1 Linux-only skip). Всего 116 tests,
2 пропуска. `git diff --check` не обнаружил ошибок whitespace. Полный набор
приложения в этой проверке не запускался; проверены изменённые deployment
сценарии, общий state reader и связанные проверки SQLite.

POSIX owner/mode проверка чтения private config в этих тестах подменена
реальным чтением файлов: Windows не доказывает Linux права или directory
fsync. GitHub evidence transport подменён; полный валидатор artifact
проверяется отдельным существующим `test_predeploy_compatibility`.
Инъекции прерываний не доказывают power-loss durability. Реальные Linux
Docker inspect/bind, systemd, startup/health прежнего образа на новой схеме,
reboot, остановка сторонних CLI/writers и controller snapshot ещё требуют
6.5/6.6 и этапа 7. Эти ограничения блокируют снятие rollout gate.
