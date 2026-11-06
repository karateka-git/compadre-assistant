# Шаг 7.1: проверить копию и подготовить окно испытаний

Запись от 06.11.2026 фиксирует исходные данные для испытаний на закрытой
копии production-БД. Порядок следующих шагов находится в [плане](PLAN.md#шаги-этапа-7),
действующие ограничения VPS — в [PRODUCTION.md](../PRODUCTION.md), команды
установки и восстановления — в [OPERATIONS.md](../OPERATIONS.md).
**Подготовка 7.1 проверена: границы модели и private
FS/lock подтверждены; 7.2 не выполнялся. Остановка ботов и реальная миграция
image остаются заблокированы до допуска итогового SHA в 7.3.**

## Проверить исходный production

Свежий SSH-осмотр выполнен 06.11.2026, 14:36:03–14:37:53 UTC. На VPS
использован `/usr/bin/python3 -B -` только как административный инструмент
чтения metadata, хеширования и копирования state, а не как замена проектному
окружению для тестов. Содержимое `.env`, токены, пользовательские строки,
полные inspect и журналы не выводились. SQLite читалась с
`mode=ro&immutable=1`; нулевые WAL проверены отдельно. Live-БД не открывалась.

| Объект | Наблюдение |
| --- | --- |
| Checkout VPS | `/opt/obs-chat-bot`, чистый detached HEAD `dd71c2ba42a0744b5b654cedcf73009ce8879058` (previous, v3.1.0). |
| Основная служба | `obs-chat-bot.service`: active/exited, enabled, Result=success, NeedDaemonReload=no. |
| Backup | Timer active/waiting, enabled; service inactive/dead, Result=success, NeedDaemonReload=no. Последний timer trigger 06.11 03:15:33 UTC, следующий 07.11 03:15 UTC. |
| Диск | На общем `/dev/vda1` доступно 6 915 047 424 bytes (52% занято). Это снимок, перед несколькими copies/build повторить расчёт места. |
| Marker/selector | В production отсутствуют `deploy-in-progress`, `active-db.json`, `recovery-blocked` и `controller.sha`. |
| Legacy state | stage=success; нет protocol_version/controller_sha/retry_after; `failed_version=v3.1.0` сохранено, не очищалось. `timer_before` и остальное состояние сохраняются в приватной копии. |
| State digest | `/var/lib/compadre-assistant-deploy/state.json`: root:root 0600, 2082 bytes, SHA256 `fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317`. |
| Runtime pin | last_good: v3.1.0, previous SHA выше, tag `compadre-assistant:release-dd71c2ba42a0744b5b654cedcf73009ce8879058`, image ID ниже, CI run 41436078724/1. Содержимое runtime.env не публиковалось. |
| Docker inventory | Всего два контейнера, оба running/healthy, restart count 0, restart policy unless-stopped. Единственный mount каждого — writable bind `/opt/obs-chat-bot/data` → `/app/data`. Проверочных контейнеров нет. |

Точные исходные container IDs:

| Канал | ID |
| --- | --- |
| Telegram | `2798069b3f96d3a31c9d3a9d7b12e52c3a754fa4d5348798963209a38869fe42` |
| VK | `31a165fee7911fc6f51690956954c4d6258e433c1f8a9c768295a3cdf6a15a00` |

У обоих image ID
`sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8`.
Effective ExecStartEx — Docker Compose `up -d --no-build --pull never
vk_assistant tg_assistant`, ExecStopEx — `stop vk_assistant tg_assistant`;
у обеих flags=no-setuid. User/Group=user, WorkingDirectory=/opt/obs-chat-bot,
EnvironmentFiles указывает на приватный runtime.env. Drop-ins:
`channels.conf`, `zz-deploy-pinned.conf`. В этом осмотре не выполнялась полная
повторная аттестация D-Bus Conditions и `systemd-analyze verify`.

## Проверить закрытые копии

Пользователем заранее подготовлен каталог
`/var/lib/compadre-assistant-stage7-20261106`, root:root 0700. Его
`evidence/preparation.json` датирован 13:54:24 UTC и сообщает
containers_created=false, units_installed=false. Эти поля — прежняя запись;
свежий полный Docker inventory содержит только рабочую пару. Поиск
`compadre-stage7*` в `/etc/systemd/system`, `/run/systemd/system`,
`/usr/lib/systemd/system` вернул пустой список. `list-unit-files` с этим
шаблоном вернул rc=1 без строк и stderr; один этот rc не трактуется как успех.
Файлы будущих units в приватном `units/` не устанавливались и не запускались.

| Копия | Свежая проверка |
| --- | --- |
| `fixtures/source.db` | root:root 0400; 12 677 120 bytes; integrity=ok, FK violations=0. |
| `data/app.db` | root:root 0600; тот же размер и SHA256; integrity=ok, FK violations=0. |
| Обе SQLite | SHA256 `5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c`; WAL=0 bytes; при immutable-чтении journal_mode=delete. |
| Manifest обеих | `0001_initial_schema.sql`, `0002_user_capability_preferences.sql`, `0003_backfill_existing_user_names.sql`, `0004_private_chat_address_lookup.sql`. |
| Release backup | `/var/lib/compadre-assistant-deploy/backups/v3.1.0-dd71c2ba42a0744b5b654cedcf73009ce8879058.db`: root:root 0600, 12 677 120 bytes; integrity=ok/FK=0, WAL=0. |
| Ежедневные backup | Семь основных `.db` за 31.10–06.11, каждый 12 677 120 bytes и 0600; последний `app-20261106T031533Z.db`. Наличие/размер проверены, integrity каждого daily backup в 7.1 не проверялся. Старые sidecars не удалялись. |

Это проверка целостности предоставленного снимка, а не доказательство
равенства его содержимого live-БД на 14:37 UTC. Источник уже закрыт;
будущие сценарии получают отдельные копии, исходный `source.db` не меняют.

В 7.1 создан только новый приватный каталог
`/var/lib/compadre-assistant-stage7-20261106/legacy-state-inspection-20261106T1441Z`
(root:root 0700) с `state.json` (root:root 0600). Суффикс каталога —
идентификатор, фактическое создание наблюдалось около 14:37:05 UTC.
SHA256 state совпал с live digest выше. Предоставленный заранее
`fixtures/state.legacy.json` также совпал. Production state, marker,
lock, controller, checkout, units и рабочая БД не менялись.

## Закрепить SHA и проверить допуск

| Предмет | Доказательство и ограничение |
| --- | --- |
| Previous | Установлен точный `dd71c2ba42a0744b5b654cedcf73009ce8879058`, previous image ID и оба container IDs закреплены выше. |
| Previous CI | Свежий GitHub API подтвердил [Main full 41436078724/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41436078724/attempts/1): completed/success для точного previous SHA. Это не допуск будущему candidate. |
| Current candidate code | HEAD worktree `8f71d705145df4c61fe5aef0175d77b1cdf4ccee`; перед работой Git status пуст. После изменения кода в 7.3 SHA придётся закрепить заново. Документация этого шага также не становится release admission. |
| Current candidate CI | GitHub Actions API с `head_sha=8f71d705145df4c61fe5aef0175d77b1cdf4ccee`: total_count=0. Успешного полного CI точного SHA не предъявлено. |
| Current candidate image/evidence | В свежем локальном Docker image inventory VPS нет release tag этого candidate. Его точный image ID, OCI revision и compatibility artifact для пары previous/candidate не установлены; чужой dev image или прежний образ не подставлялись. |
| Installed controller | Все 38 обычных файлов (без pycache) совпали с Git blobs previous SHA. Digest inventory — `dc683481109242bc88eae2b3350c86829930e716b6a6b6b5783420b3f8d795bb`: SHA256 UTF-8 JSON массива `[relative_posix_path, file_sha256, numeric_mode]`, сортировка по пути, separators=(',', ':'). Это не digest tar. |
| Candidate snapshot | Точный архив HEAD в 7.1 не собирался и на VPS не переносился; SHA/digest текущего candidate snapshot не подтверждены. Подготовить и проверить в 7.2 после устранения блокера изоляции. |
| Исторический snapshot | [Локальный архив 6.5](STAGE6_CONTROLLER_SNAPSHOT.md#окончательный-локальный-snapshot-после-продолжения-65): source `27c1da95ad12acfa6b37847515dd1cdae0401f2b`, tar SHA256 `7d1e87d17a96dac0a88a8ec3683b6ef0c470a1260a80ef308f5a9de9d965164b`. Он не выдаётся за snapshot HEAD или CI admission. |

## Закрыть запуск до доказанной изоляции

Предоставленный `compose.json` использует для обоих каналов точный previous
image, entrypoint `/bin/false`, пустой command, network_mode=none,
restart=no, единственный bind приватного `data`. Environment пуст, env_file
не задан. Это безопасная заготовка без polling, но не готовый transport
controller: она сама не перехватывает его внешние операции.

Исследованы [Host](../../scripts/deploy_prod.py),
[SwitchHost](../../tests/test_deploy_database_switch.py),
[RecoveryHost](../../tests/test_deploy_database_recovery.py) и
[IntegrationHost](../../tests/test_stage6_integration.py). Тестовые наследники
моделируют Docker/systemd, однако не подтверждают безопасный полный CLI-запуск
controller на VPS. У Host есть прямые `subprocess.run` вне `run()`:
`logs` читает реальные Docker logs, `unit_enabled` вызывает systemctl,
`migrate_copy` вызывает Docker run и cleanup Docker rm с
`DOCKER_HOST=unix:///var/run/docker.sock`. Кроме того, module constants
содержат production service/unit paths. Простая подмена `Host.run` или
смена checkout/state_dir не закрывает все эти пути. Fixture подменяет
проверки evidence/POSIX и работает с синтетической схемой; её нельзя выдать
за Linux-приёмку candidate image и production-копии.

Добавлен [ограниченный guard](../../scripts/stage7_transport.py) и
[тест отдельного процесса](../../tests/test_stage7_transport.py). Import модуля
сам не устанавливает hooks и не выполняет операции. Явный `install_guard`
необратимо запрещает subprocess/socket и production paths; неизвестная
команда `ModelTransport` отвергается без fallback. Отдельный процесс
проверил 17 запретов, включая настоящие Host.logs/unit_enabled/migrate_copy,
унаследованный fd, dir_fd, symlink и link наружу; также чтение legacy state
и обычный файл/SQLite внутри private root.
Это защита от случайного внешнего вызова доверенного Python-кода, не OS
sandbox: native-код, заранее открытые handles, гонки путей и процесс с
возможностью изменить guard не объявляются изолированными.

После первого частичного прохода guard дополнен поддержкой private fd:
перехваченный `os.open` сохраняет канонический путь и device/inode,
`fdopen`/flock принимают только зарегистрированный fd с неизменным inode
и private путём. Закрытие удаляет регистрацию, повторно использованный
descriptor с другим inode отвергается. `dir_fd` и symlink не разрешены;
private link/unlink разрешён для публикации снимка, запись через alias с
nlink>1 закрыта. URI SQLite допускает только mode=ro/rw на private пути.
POSIX root должен принадлежать текущему uid и иметь приватные права.

Под guard локально прошли настоящие `write_state`, `_atomic_text`,
register_initial/selector, копирование SQLite и recovery из
database_selected/admission_recorded/candidate_started. Внешние операции
в этих сценариях остаются моделью RecoveryHost; проверка допуска GitHub
подменена, мигратор синтетический и схему не меняет. Дополнительно проверены
два private rename, прерывание между ними и возврат имён. Это поддержка
операций staging, не испытание controller snapshot по 7.2.

Для 7.2/7.4/7.5 сценарий обязан устанавливать guard в отдельном процессе,
использовать только новые private checkout/state/SQLite и явную модель
внешних операций. Любой забытый прямой subprocess либо неизвестная команда
закрывает процесс вместо fallback к реальным Docker/systemd/каналам.
Изоляция доверенного проверочного сценария доказана в этих границах;
запуск обычного production CLI под root этим документом не разрешается.

## Проверить Linux FS и lock без выпуска

После локального успешного прогона и свежего production preflight
14:47:04 и 14:48:43 UTC разрешён отдельный синтетический FS/lock proof.
Исходники Python переданы по SSH stdin и импортированы из памяти; файлов
controller или units на VPS не устанавливалось. Guard установлен до
проверочного сценария. Все записи — в новых root:root 0700 подкаталогах
`/var/lib/compadre-assistant-stage7-20261106`.

Первый `transport-proof-20261106T1448Z` в 14:48:12 UTC остановился с отказом:
Linux recovery потребовал `deploy/database-compose.yml` рядом с виртуальным
SOURCE_ROOT, которого при импорте из памяти нет. Только синтетический state
перешёл в degraded. Это не считается успешной проверкой Linux recovery;
каталог сохранён для разбора, к production пути не обращались.

Повторный `transport-fs-proof-20261106T1449Z` в 14:49:17–14:49:18 UTC
проверял только заявленную FS/lock границу и завершился rc=0:

- Настоящие atomic state writer и `_atomic_text` опубликовали state/selector;
  настоящий `read_selected` подтвердил old на синтетической SQLite.
- `deploy_lock` действительно удерживал Linux flock; второй open/flock
  отвергнут, повтор после release успешен на том же lock inode.
- Private link/unlink сохранил единственный inode без второго alias.
- Девять негативных операций отвергнуты: прямые Host.logs/unit_enabled/
  migrate_copy, socket, subprocess Docker, чтение live state, запись live
  DB, неизвестная команда модели и dir_fd.

SHA256 LF-исходника guard, исполненного в обоих Linux proof:
`dd40aedde9cf14938ccbe48951ff335011924f5698d777d6c2c28c897f48b01f`.
Это не установленный snapshot digest и не CI admission. Linux proof
использовал синтетические строки и SHA/image ID, не production данные.

## Отделить контракт image migration в 7.3

`migration_command` только строит argv и ничего не исполняет. Он принимает
immutable image ID и единственную writable SQLite в отдельном каталоге
внутри private `scenarios`; fixtures/source, live пути, alias, неожиданные
файлы в mount, имена с разделителями mount и mutable image tags отвергаются.
Команда задаёт network=none, read-only rootfs, cap-drop=ALL,
no-new-privileges, tmpfs и единственный private bind; entrypoint — Python
CLI `--migrate`, без env-file, credentials или polling. Guard запрещает
исполнение даже этой команды в процессе модели.

В 7.3 нужен отдельный внешний мигратор после полного допуска SHA/image/OCI,
с минимальным env и проверкой точного mount/argv, timeout и удаления только
его собственного контейнера. Он выполняется отдельно от модели и возвращает
только результат миграции и проверки копии. Проверка/исполнение реального
candidate image не выполнялись в 7.1; production `migrate_copy` не становится
разрешённым transport. Реальный `require_switch_rollout` остаётся закрыт.

## Подготовить одну остановку и восстановление

**Действующее указание пользователя от06.11.2026:** maintenance окно уже
открыто: stop attempt16:58:07.942252 UTC, подтверждение16:58:19.824951 UTC.
Оба прежних IDs exited, serviceinactive, timeractive. До завершения испытаний
ботов не запускать, после отдельных отказов case автоматическое восстановление
не выполнять (кроме угрозы данным/отдельной команды пользователя). Шаги1–3
ниже описывают подготовку уже выполненной остановки: повторный stop не нужен.
Восстановление выполняется после испытаний в7.6 с измерением всего этого окна.
Предыдущие два коротких окна завершились recovery (32.235с и30.997с) по
прежнему указанию; они не входят в текущее maintenance окно.

Следующий порядок — будущая операция после закрытия блокеров, **в 7.1 ни
одна команда stop/start/disable/enable не выполнялась**.

1. Повторить свежий осмотр и убедиться, что baseline не изменился: оба
   точных IDs/image, checkout/pin/state digest, service/timer, marker,
   backup и отсутствие постороннего writer. Подтвердить окно без ручных
   writers по [OPERATIONS.md](../OPERATIONS.md#подготовить-backup-и-штатный-запуск-для-sqlite-v2).
   Проверить свободное место для всех новых copies и image. Не начинать
   рядом с 03:15 UTC или при работающем backup/deploy.
2. До остановки получить полный CI итогового candidate SHA, совместимость
   точной пары с previous, точный image ID/OCI и проверенный snapshot
   итогового SHA после изменения gate в 7.3. Проверить transport и его
   private state/lock/selector; production marker/state/timer не менять.
   Отказ любого допуска оставляет рабочие каналы включёнными.
3. Зафиксировать UTC начала и один раз выполнить
   `sudo systemctl stop obs-chat-bot.service`. Effective ExecStop останавливает
   сразу VK и Telegram. Read-only осмотром потребовать service inactive,
   оба прежних IDs stopped, отсутствие иных writers. При неполной остановке
   испытания не продолжать до разбора; ботов оставить остановленными по
   действующему указанию пользователя. Досрочное восстановление — только при
   угрозе данным либо отдельной команде пользователя. Restart policy
   unless-stopped не менять; VPS не перезагружать. Production backup timer
   оставить в исходном состоянии: он обслуживает live-БД, испытания используют
   отдельную модель timer и другой lock. Возможное пересечение daily backup
   требует повторного осмотра и переноса окна до остановки.
4. Выполнить 7.3–7.5 только с копиями и изолированными операциями. Не
   возобновлять production между сценариями. При невозможности продолжить
   разобрать отказ в закрытом контуре; ботов оставить выключенными до
   завершения испытаний/7.6. Автоматический start после отказа запрещён новым
   явным указанием пользователя (кроме угрозы данным/отдельной команды).
5. После завершения испытаний в7.6 до start подтвердить неизменность production state/pin/checkout,
   отсутствие marker, исходные image/IDs и состояние live SQLite по
   отдельной read-only проверке integrity/FK/schema. Выполнить
   `sudo systemctl start obs-chat-bot.service`, затем проверить оба прежних
   IDs/image, healthy, restarts, отсутствие дублей, service/timer и polling.
   Compose up может пересоздать контейнер при расхождении конфигурации;
   потеря исходного ID является отклонением, требует разбора, а не
   автоматического признания восстановления. Записать конец и длительность
   единственного окна. При отказе следовать recovery без удаления state/marker.

## Проверить изменение и зафиксировать результат

В worktree `.venv` отсутствует; использован явный интерпретатор проектного
окружения основной папки, его unittest/sqlite3 imports проверены. Команда:

```powershell
$env:PYTHONUTF8='1'
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -B -m unittest tests.test_stage7_transport tests.test_deploy_database_switch tests.test_deploy_database_recovery tests.test_stage6_integration
```

Результат: 35 tests, OK, 17.883 с. После дополнения только теста private
rename повторены 4 tests transport: OK, 1.917 с. Проверены отсутствие
побочных эффектов import, негативные операции, настоящий private state/
selector/recovery и неисполняемый migration contract. Полный CI не запускался.
Родитель независимо повторил 4 tests transport из проектного окружения:
OK, 1.745 с.

Закрытая SQLite-копия и исходный manifest подтверждены, отдельная legacy
state copy сохранена, SHA/IDs и ограничения допуска зафиксированы. Повторные
хеши source/state в 14:49:31 UTC совпали с исходными; service и timer
остались active/enabled, marker отсутствовал; полный Docker inventory
подтвердил те же два healthy container IDs. Синтетический FS/lock proof
на VPS отделён выше. Production миграций, Docker build/run, deploy,
controller install, остановки ботов, commit или push в 7.1 не было.

Родитель проверил 7.1 перед передачей 7.2; шаг 7.2 ещё не выполнялся.
CI/snapshot/image/compatibility точного итогового SHA остаются обязательными
блокерами остановки в 7.3. Реальные systemd/start/health/polling нового
выпуска остаются в 7.7; reboot и power loss не проверяются на единственном VPS.
