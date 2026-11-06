# Шаг 6.3. Выбрать поколение и сохранить допуск polling

Локальная реализация от 04.11.2026 связывает [снимок шага 6.2](STAGE6_FINAL_SNAPSHOT.md)
с [протоколом 6.1](STAGE6_DB_SWITCH.md). Основной код —
[deploy_database_switch.py](../../scripts/deploy_database_switch.py) и методы
`Host` в [deploy_prod.py](../../scripts/deploy_prod.py). **Новый путь закрыт для
эксплуатации**: `Host.require_switch_rollout()` безусловно отказывает до
изменения состояния и остановки работающих каналов. Нет параметра CLI или
файла конфигурации, который снимает запрет. Действующий VPS не изменялся;
его инструкции остаются в [OPERATIONS.md](../OPERATIONS.md).

## Выполнить подготовленный переход

1. Под deploy lock работать только с явно версионированным state v2.
   Legacy state сохраняет прежний отдельный маршрут; он не получает ложную
   границу `false` автоматически. `register_initial` предназначен для явной
   будущей процедуры 6.5: после доказанной остановки записать state и selector
   исходного `app.db` на месте. Сам `register_initial` повторно отказывает;
   завершение частичной регистрации по durable intent добавлено в
   [recovery 6.4](STAGE6_RECOVERY.md).
2. Для следующего перехода сохранить новый UUID перехода, прежнее выбранное
   поколение, SHA controller из внешнего `controller.sha`, SHA/image ID/tag
   обоих runtime и проверенное predeploy evidence. Проверки предварительной
   копии разрешают путь через selector, а не через жёсткий `app.db`.
3. После `writers_stopped` подтвердить marker, остановку обоих контейнеров,
   их restart policy `no`, остановку основного systemd unit, timer и backup
   service. Сохранить selector old для нового transition ID. Получить
   окончательный snapshot и отдельную мигрированную копию через helper 6.2.
   Записать snapshot path/digest, затем обе идентичности: UUID, host/container
   path, device/inode, manifest миграций и fingerprint.
4. Сохранить `switch_intent`, атомарно опубликовать `active-db.json`, затем
   прочитать фактический selector и записать `database_selected`.
   Публикация использует flush/fsync файла, replace и POSIX fsync каталога.
   Обе SQLite и их WAL/SHM остаются на своих именах. Отсутствующий файл,
   symlink/hardlink, чужой transition, неверный intent или identity закрывают
   выбор. Selector new без точного old → new intent запрещён.
5. Сформировать производный runtime.env с image pin и DATABASE_PATH.
   [database-compose.yml](../../deploy/database-compose.yml) задаёт путь
   через `environment` обоим каналам, перекрывая legacy env_file. Bind
   `./data:/app/data` сохраняется. До start выполняется настоящий Compose
   config, затем `compose create --no-build --pull never --force-recreate`.
   Inspect проверяет точный набор обоих ID, image ID, состояние `created`,
   отсутствие рестартов, restart policy `no`, DATABASE_PATH и data bind.
6. Сохранить `runtime_prepared`, затем `admission_recorded` с `true` и
   привязкой к transition, поколению и candidate SHA/image ID. Перечитать
   state и selector. Только затем передать **проверенные ID** в `docker start`.
   Сам метод start повторно читает durable admission и проверяет inspect.
   Ошибка sync или проверок запрещает start даже после фактического replace.

Встроенная ветка deploy после этого пока останавливается с отказом вместо
штатной финализации: marker не снимается, timer не возобновляется. Этот
защитный отказ недостижим через обычный вызов из-за раннего rollout gate,
но предотвращает объявление успеха при преждевременном удалении только gate.
На момент шага 6.3 любое незавершённое состояние v2 передавалось в `degraded`.
В [шаге 6.4](STAGE6_RECOVERY.md) добавлен проверяемый recovery до/после границы;
legacy recovery с `data/app.db` для v2 по-прежнему запрещён. Успешный возврат
пока заканчивается `recovery_verified` с marker и остановленным timer.

## Снять блокировки в следующих шагах

Перед удалением rollout gate требуется завершить все следующие условия:

- Возврат и защита при повреждённом JSON локально реализованы в
  [6.4](STAGE6_RECOVERY.md); Linux runtime и power-loss доказательства
  по-прежнему требуются. Исходный JSON сохраняется, отдельный marker отказа
  закрывает дальнейший запуск без догадок о выбранной БД.
- В 6.5 реализовать проверенную регистрацию legacy state, установку
  controller snapshot и его SHA, проверку installed pin, штатный systemd
  runtime и backup по selector. Нынешний runtime.env v2 намеренно содержит
  critical override; его нельзя считать готовой конфигурацией штатного boot.
- **Исключить сторонние writers/CLI до окончательного снимка.** Текущая
  quiescence проверяет только известные каналы и systemd units. Нужна
  реализованная проверка остальных writers либо доказанный обязательный
  общий lock/операционный запрет запуска для всех участников. Этот пробел
  блокирует снятие gate наряду с recovery и snapshot, а не переносится в
  необязательное улучшение.
- Проверить Linux Docker inspect/bind, права root, systemd, POSIX fsync,
  reboot/power loss и фактические startup/health обоих образов на стенде.

## Повторить локальные проверки

В worktree `.venv` отсутствует. Проверены импорты `unittest`, `sqlite3`,
`yaml` проектного окружения основной папки. Команда из корня worktree:

```powershell
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -m unittest tests.test_deploy_database_switch tests.test_deploy_prod tests.test_deploy_final_snapshot tests.test_predeploy_compatibility tests.test_migration_runner tests.test_runtime_schema
```

Новые 14 tests используют отдельные синтетические SQLite, настоящий backup
и файловый state/selector. Проверяются последовательность стадий, сохранение
старого файла, копия для мигратора, обе стороны ошибок selector replace,
ошибка настоящего `fsync` state writer, отказ после публикации admission,
подмена intent/identity/границы, отсутствие либо лишний inspect канала,
ошибки env/mount/image/restart каждого канала и прямой start без допуска.
Отдельно проверены блокировка production entry points, интеграция через
`deploy()` при тестовом обходе gate и отсутствие legacy recovery у прерванного v2. Настоящий Docker Compose config подтверждает
общий путь обоих каналов и отказ без переменной; Docker/systemd транспорт
самих контейнеров заменён тестовым адаптером.

Новые 14 tests прошли; общий результат указан в плане после группового
прогона. Windows не исполняет POSIX directory fsync. Инъекции ошибок не
доказывают power-loss durability; процессовые аварии прототипа 6.1 не
подменяют будущую интеграционную проверку нового controller в 6.6/этапе 7.
