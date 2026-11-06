# Шаг 6.6: проверить связанную state machine

Запись от 06.11.2026 фиксирует локальную интеграционную проверку
switch/recovery/finalization/boot и отдельно согласованное удаление двух
исторических контейнеров VPS. Общий статус — в [плане](PLAN.md#шаги-этапа-6),
условия будущей установки — в [OPERATIONS.md](../OPERATIONS.md).
**Локальная матрица пройдена; эксплуатационный критерий этапа 6 полностью
не подтверждён, rollout gate остаётся закрыт.**

## Повторить локальную матрицу

Исходный checkout: `3bceb65426983ba1e8762fb3d0d135e7a8ad92b9`, ветка
`codex/ci-release-review-plan`; добавлены только тесты и эта документация.
Runtime/controller код и gate не изменялись. Новые
[integration tests](../../tests/test_stage6_integration.py) соединяют
настоящие функции переключения, recovery, финализации и boot с реальными
временными SQLite, WAL, backup, selector и state writer. Используются
синтетические таблица и SHA/image ID, без пользовательских данных.

| Сценарий | Подтверждённый результат |
| --- | --- |
| Успешное переключение и повтор boot | После `candidate_started` state перечитан новым Host; финализация приводит к `success`, selector new, точному candidate SHA/image ID и двум durable ID. Marker снят, исходные enabled/active timer восстановлены в модели. Два повтора boot не добавляют start/create. Исходная SQLite побайтно сохранена, строка присутствует в новой. |
| Отказ после selector, до admission | Recovery из `database_selected` возвращает old и прежний SHA/image; финализация доходит до `rolled_back`, граница остаётся false. Повтор boot не запускает дополнительную пару. |
| Отказ после durable true, до start | Recovery из `admission_recorded` сохраняет new, запускает прежний runtime на ней и завершает `rolled_back`; true не сбрасывается. |
| Отказ после start и записи | В новую SQLite записана committed WAL-строка. Recovery, финализация и повтор boot сохраняют new и обе строки; исходные байты old неизменны, integrity ok/FK пуст. Проверка exact Release/policy/previous SHA вызывается дважды в recovery и при каждом из двух повторов boot. |
| Прерывание финализации | Исключение после durable intent и снятия marker прерывает родительский вызов. Отдельный новый Python-процесс читает файлы и модель внешнего Docker, выполняет `_recover`, заканчивает `success` с тем же boot intent, без start/create. |

Три test methods включают пять сценариев: метод recovery содержит три
subtests. Последний сценарий действительно запускает отдельный процесс через
тот же `sys.executable`, но прерывание исходного вызова инъецировано;
это не kill/reboot/power loss. Новый процесс не получает объект controller
state или callback из памяти родителя; fake Docker inventory передаётся
отдельным JSON как модель внешнего daemon.

В fake transport `compose create` запрещён при живом writer, `docker start`
проверяет точные IDs, durable разрешение и отсутствие уже работающей пары.
Это доказывает последовательность команд в модели; реальных Telegram/VK
polling-процессов здесь нет. GitHub evidence response, POSIX permissions,
Linux lock и systemd транспорт подменены. Мигратор этой интеграционной
fixture сохраняет схему: реальные SQL/CLI миграции отдельно проверяются
существующими final-snapshot/predeploy tests в том же прогоне.

В worktree `.venv` отсутствует. Проверены зависимости проектного окружения
основной папки. Команда из корня worktree:

```powershell
$env:PYTHONUTF8='1'
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -B -m unittest tests.test_stage6_integration tests.test_controller_snapshot tests.test_controller_integration tests.test_deploy_backup tests.test_deploy_database_switch tests.test_deploy_database_recovery tests.test_deploy_prod tests.test_deploy_prepare tests.test_deploy_final_snapshot tests.test_predeploy_compatibility tests.test_deploy_runtime tests.test_migration_runner tests.test_runtime_schema tests.test_deploy_metrics
```

Групповой результат: 136 tests, `OK`, 2 Linux-only skips, 53.734 с.
После усиления restart-сценария отдельным Python-процессом повторены все
три новых tests: `OK`, 2.027 с. Полный набор приложения и GitHub CI не
запускались. Остальные проверки группы подтверждают отказ production entry
points при закрытом gate, ошибки selector/pin/evidence, реальные snapshot
и backup выбранной SQLite, а также recovery после прерываний.

Локальный `docker info` вновь отказал: отсутствует pipe
`dockerDesktopLinuxEngine`. Daemon не запускался. Реальный Linux Docker,
systemd/PartOf, права/lock, directory fsync, startup обоих образов на новой
схеме, reboot и power loss остаются приёмкой этапа 7. Установка snapshot
на VPS и полный CI точного merge SHA также ещё нужны. Эти ограничения
не позволяют считать этап 6 эксплуатационно принятым или снимать gate.

## Зафиксировать разрешённую очистку VPS

По явному разрешению пользователя удалены только два stopped legacy
контейнера. Перед изменением повторно прочитаны PRODUCTION/OPERATIONS,
выполнен свежий read-only preflight в 13:20:17 UTC и непосредственно перед
`docker rm` повторены assertions состояния. SSH Python исполнялся со stdin
с `-B`, без сохранения скрипта на VPS. Secret env, ключи, полные inspect/logs
и содержимое SQLite не выводились.

Оба объекта имели `status=exited`, image ref `obs-chat-bot:dev`, image ID
`sha256:c6809c7283abdb6978297335af2a2443c7bfa19dc219df7eeeba91808e9fd185`
и ровно один writable bind `/opt/obs-chat-bot/data` → `/app/data`;
других mounts и Docker volumes у них не было.

| Удалённое имя | Точный ID |
| --- | --- |
| `obs-chat-bot-tg_catcher-1` | `cb0a4c67604b4ebaad8ffa8572e1455eb11bd00cef127c88f3a022a9fea5671f` |
| `obs-chat-bot-vk_catcher-1` | `5cabbd3547470e007255017c64887573f29f61cbbe9bd0fcdca7475864069474` |

Вызван `docker rm` с этими двумя полными IDs **без `-v`**. Images, volumes,
bind data, активные контейнеры, checkout, controller, units и БД не изменялись
этим действием. Никакой deploy, установки snapshot или миграции не было.
Контейнеры как объекты, включая их writable layers, удалены; операция не
сохраняет возможность восстановить прежний container ID. Dev image сохранён,
bind data не удалён. При необходимости контейнер можно создать заново из
сохранённого image и отдельно проверенной конфигурации, но запуск старых
catcher рядом с активной парой не разрешён этой очисткой.

Независимый read-only postcheck завершён в 13:21:21 UTC:

| Объект | Результат |
| --- | --- |
| Полный Docker inventory | Ровно два активных assistant; обоих удалённых IDs нет. |
| Telegram | ID `2798069b3f96d3a31c9d3a9d7b12e52c3a754fa4d5348798963209a38869fe42`, running/healthy, restarts 0. |
| VK | ID `31a165fee7911fc6f51690956954c4d6258e433c1f8a9c768295a3cdf6a15a00`, running/healthy, restarts 0. |
| Активный image | У обоих прежний `sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8`. |
| Checkout | Чистый; preflight HEAD `dd71c2ba42a0744b5b654cedcf73009ce8879058`. |
| Controller state | `stage=success`, marker и selector отсутствуют; preflight legacy state без protocol/controller SHA. Установленный snapshot не переаттестован. |
| Systemd | Основной service и backup timer active/enabled; preflight backup service inactive/dead, Result=success, NeedDaemonReload=no. |
| Dev image | Точный прежний ID `c6809c…fd185` сохранён. |

Первый postcheck после успешного удаления ошибочно сравнил short IDs от
`docker ps -aq` с полными IDs и остановился на assertion. Никаких повторных
изменений не выполнялось: отдельный read-only запрос с `--no-trunc`
подтвердил полный ожидаемый набор и все результаты выше. Проверка здоровья
не означает пользовательскую приёмку ответов каналов или новый допуск v2.

Очистка устранила именно две обнаруженные legacy writable mounts. Она не
исключает ручные host CLI или будущий `docker run`: администратор сохраняет
доступ к Docker/data, а согласованное окно без ручных writers остаётся
обязательным. Исторический inventory 6.5 сохраняется как снимок до очистки;
для любого следующего действия нужен новый осмотр VPS.
