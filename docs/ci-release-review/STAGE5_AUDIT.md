# Проверить прежние repositories на схеме кандидата: аудит 5.1

Запись от 02.11.2026 фиксирует пересмотр [этапа 5](PLAN.md#5-автоматически-проверять-совместимость-бд):
CI получает код и тесты точного предыдущего релизного SHA и проверяет их
поведение на временных БД из настоящих миграций кандидата. Ни блокирующего
schema diff, ни запуска/переноса предыдущего Docker image в CI не требуется.
Действующий выпуск описан в [RELEASES.md](../RELEASES.md), эксплуатация —
в [OPERATIONS.md](../OPERATIONS.md). Это план реализации, а не готовый допуск.

## Зафиксировать смену решения и исходное состояние

Прежняя рекомендация `docker image save` → private storage → `load` → запуск
точного установленного image ID в CI **отменена решением пользователя**.
Локальный экспорт образа уже был выполнен в ходе прерванной реализации;
он не используется для допуска. В этой правке архив не удалялся и не
проверялся, round-trip и CI-хранилище не объявляются готовыми. Оставшиеся
`check_image_compatibility.py` и `compatibility_probe.py` — незавершённые
локальные материалы прежнего подхода, не действующий CI-контракт. Эта правка
не изменяет их, приложение, workflows, deploy или VPS.

Историческая read-only проверка VPS 02.11.2026 в 16:19:25–16:20 UTC установила:

| Поле | Наблюдение |
| --- | --- |
| Версия / SHA | `v3.1.0`, `dd71c2ba42a0744b5b654cedcf73009ce8879058` |
| Состояние | Чистый checkout, `stage=success`, та же версия в `last_good` |
| Контейнеры | Telegram и VK running/healthy с одинаковым `.Image` |
| Image ID | `sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8` |
| OCI revision | Тот же полный SHA |
| Платформа / Docker | `linux/amd64`, server `29.8.0` |

Источник — прежний аудит и [результат 4.5](STAGE4_RELEASE.md#подтвердить-установку-и-цель-следующего-возврата).
Новая редакция не повторяла подключение. Перед реальным deploy состояние
сверяется заново. Image ID остаётся локальным признаком закреплённого runtime;
он не является CI artifact и не доказывается исходным checkout.

[check_full_migrations.py](../../scripts/check_full_migrations.py) уже проверяет
fresh/upgrade, журнал, integrity/FK, идемпотентность и synthetic pending SQL.
[probe_previous_code.py](../../scripts/probe_previous_code.py) берёт старый
`SQLiteAppUserRepository`, читает одну строку и меняет display_name. Это
полезный частичный тест, но не полное покрытие repositories. Старые тесты
сейчас не извлекаются этим probe; используется текущий Python environment.

## Разделить два обязательных результата CI

| Набор | Источник и обязательный результат |
| --- | --- |
| Candidate migrations/data integrity | Код/тесты точного candidate SHA: свежая БД и upgrade БД previous SHA с данными всех модулей; неизменность выпущенных SQL, точный префикс журнала, новые миграции по порядку, integrity/FK, идемпотентность, сохранность данных и связей. |
| Old repository behavior | Код и поведенческие тесты точного previous release SHA: все публичные чтения/записи на настоящей candidate-схеме; дополнительно отдельные тесты против прежнего API для непокрытых методов и двусторонних данных. |

Оба результата обязательны. При отсутствии новых SQL второй набор всё равно
выполняется. Нельзя считать зелёный candidate suite проверкой старого API.
Равенство схем до/после не требуется: добавление колонки и удаление более
неиспользуемой колонки разрешены при сохранности данных и успешных операциях.
CHECK, trigger, NOT NULL без подходящего default или изменение смысла данных
могут сломать старые writes при сохранении всех колонок — это должно дать отказ.
Fingerprint обозначает конкретную проверенную схему и защищает от подмены;
это не блокирующий diff прежней и новой структуры.

## Запустить неизменные старые тесты с новым источником схемы

1. Доверенный controller выбирает предыдущий опубликованный релиз и его
   полный SHA для этой линии. Для первой базы это выпущенная `v3.1.0` выше.
   Не выбирать произвольный последний tag, moving branch или SHA из PR input.
   Получить отдельные checkout/archive previous и candidate из точных Git
   commits, включая старые tests/helpers и requirements. Зафиксировать hashes.
2. Создать раздельные Python environments для previous и candidate по их
   требованиям; записать Python/SQLite/dependency versions. Старый subprocess
   запускается с cwd/import path только старого checkout и внешним harness.
   Проверить реальные `__file__` imports, включая tests/helpers, исключить
   installed candidate package и candidate checkout из `sys.path`.
   CI использует временные данные и fakes внешних ports, без polling/секретов.
3. В одноразовой **копии** previous checkout заменить только SQL-ресурсы
   `compadre_assistant/core/data/sqlite/migrations/*.sql` точными Git bytes
   кандидата. Не заменять `migration_runner.py`, repositories, DTO, mappers,
   domain, tests или helpers. Сделать это **до импорта** старых tests.
   Проверить manifest: все Python/tests остались байт-в-байт previous SHA,
   SQL — candidate SHA, посторонних файлов в migration source нет.
4. Это конкретный адаптер существующих fixtures: старые repository tests,
   например [identity](../../tests/test_user_identity_repository.py) и
   [vault](../../tests/test_vault_repositories.py), вызывают `apply_migrations()`.
   Его default `migrations_dir=MIGRATIONS_DIR` вычисляется при импорте и указывает
   на каталог рядом со старым runner. Подстановка SQL в этом каталоге позволяет
   им создавать настоящую новую схему без правок старого Python и assertions.
   Одной поздней подмены переменной `MIGRATIONS_DIR` недостаточно из-за default
   argument. Для иных fixtures внешний адаптер меняет только источник/создание
   тестовой БД; он не подменяет SQL repositories, результаты или assertions.
5. Каждая fixture получает изолированную временную БД с включёнными FK/CHECK.
   Внешний runner подтверждает точный candidate journal и результат создания
   схемы, затем запускает прежние операции. Если старый runner не умеет новый
   формат миграции, candidate subprocess готовит БД отдельно; внешний fixture
   adapter передаёт её старым repositories. Это не обход ошибки старого API:
   изменения самого чтения/записи и assertions запрещены. Такой adapter должен
   иметь отдельную проверку, что тест реально работал на candidate-схеме.
6. Отдельные compatibility tests, которых нет в выпущенной базе, хранятся
   рядом с harness и используют прежние публичные API/модели. Они не выдают
   себя за tests старого release. Зафиксировать их digest и полный перечень
   методов/сценариев. Не импортировать candidate repositories в старый процесс.
7. Для upgrade подготовить fixtures старым API на прежней схеме, сохранить
   контрольные ожидания, мигрировать копию candidate runner и выполнить old
   read/update/delete. Отдельный candidate subprocess читает старые записи;
   создаёт свои записи, которые читает/обрабатывает прежний код. Сверять
   смысловые поля, owner, связи, revisions и неизвестные raw значения.
   Проверить integrity/FK и журнал после операций, а не только до них.

SQL-подстановка — только техника подготовки тестовой схемы. Она **не является
проверкой startup** установленного приложения и не разрешает старому runtime
в production выполнять незнакомые миграции. Admission/startup остаются под
[контрактом этапа 4](STAGE4_MIGRATION.md); доставка и возврат относятся к этапу 6.

### Отделить неподходящие старые тесты

[Тест мигратора](../../tests/test_migration_runner.py)
`test_clean_install_applies_all_migrations_without_creating_profiles`
(в исходной базе строка 239) ожидает `[1, 2, 3, 4]`. Новая миграция закономерно
меняет этот список. Это тест candidate migration suite, а не доказательство
несовместимости repository. Его ожидания обновляют только в candidate suite;
выпущенный old test не редактируют и не включают в old repository gate.
То же правило относится к тестам exact version/count и синтетической
последовательности мигратора. Они сохраняют собственную область проверки.

Тест с `CREATE TABLE` собственного минимального формата не доказывает работу
на candidate-схеме. В manifest он отмечается как custom-schema. Если внешний
адаптер может заменить только создание fixture на реальные candidate SQL,
старые операции/assertions запускаются без изменения; иначе исходный тест
остаётся отдельным unit-тестом, а его операции покрываются новым compatibility
тестом на настоящей схеме. Аналогично разбираются прямые SQL inserts/helpers:
сбой fixture не скрывается и не превращается в успешный skip. Нужно определить,
зависит ли от поля только helper или публичный old API, и доказать второй
отдельным тестом. Пока операция не покрыта, обязательный набор не завершён.

Синтетические UNKNOWN-тесты и DTO/API fakes отделены от реального schema gate.
В `0001` есть CHECK для proposal/confirmation; при невозможности вставить
неизвестное значение в настоящую candidate-схему не выключать ограничения
через `ignore_check_constraints`, FK OFF или правку `sqlite_schema`.
Если настоящая новая миграция разрешает значение, проверить его на ней;
иначе synthetic case подтверждает только mapper/use case. UNKNOWN не должен
вызывать внешние действия, расходовать lease/token/CAS или заменять raw
значение на известный статус. Malformed JSON остаётся ошибкой формата.

## Завершить инвентаризацию публичных операций

Ниже первичная инвентаризация исходников текущей базы; пути repository-файлов
относительны `compadre_assistant/`, имена тестов — `tests/`. Это перечень для
проверки, **не утверждение полного покрытия**. Перед 5.2 повторить поиск по
точному previous SHA, включая SQL-адаптеры вне файлов `*repository.py`.
Для каждого класса/метода получить строку method → old test ID → fixture source
→ отдельный compatibility test при пробеле → результат. Отсутствующий test,
пустой набор или skip обязательной операции закрывает допуск.

| Файл / класс или группа | Публичные операции, которые нужно покрыть | Где начать проверку существующих tests |
| --- | --- | --- |
| `core/data/sqlite/user_identity_repository.py`: AppUser | create, get_by_id, update_display_name, delete | `test_user_identity_repository.py` |
| Тот же файл: ExternalIdentity | find_private_chat, has_chat_address, list_all_private_chats, list_for_user, find, create, reassign, count_for_user, touch | `test_user_identity_repository.py`, поиск callers private chat |
| Тот же файл: IdentityLinkToken / IdentityLifecycle / IdentityRebindConfirmation | create/find_active; register_with_name/link; save/find/decide, expiry, token consumption и транзакционные отказы | `test_user_identity_repository.py`, `test_unknown_status_boundaries.py` |
| `core/data/sqlite/chat_mode_repository.py` | get/get_state, replace_if_revision, save, activate_if_absent, clear | `test_chat_modes.py` |
| `core/data/sqlite/capability_preference_repository.py` | get_for_user, upsert, owner isolation | `test_capability_preference_repository.py` |
| `capabilities/obsidian/data/sqlite/article_repository.py` | create, get_by_id, find_by_normalized_url, find_by_text_hash, update_status, update_content | `test_article_user_isolation.py`, `test_process_article_url.py` |
| `analysis_result_repository.py` | save, get_by_id, get_latest_for_article | `test_analysis_result_repository.py` |
| `incoming_message_repository.py` | save, link_to_article, get_by_id, find_by_external_id | `test_incoming_message_repository.py` |
| `processing_error_repository.py`: SQLiteProcessingErrorRecorder | record и проверка сохранённых полей | `test_processing_error_recorder.py` |
| `article_flow_lease_repository.py` | acquire/release, чужой owner и expiry | Поиск callers в тестах pipeline; отдельное покрытие при отсутствии |
| `github_installation_repository.py` | replace_for_user, list_for_user, contains, delete_for_user; cascades | `test_vault_repositories.py` |
| `obsidian_vault_repository.py` | create_if_absent, replace, get_by_id, get_for_user, update_sync_state, delete_for_user | `test_vault_repositories.py` |
| `obsidian_proposal_repository.py` | save_pending, list_pending, get_by_id, get_active_for_article, claim, release_claim, get_latest_applied_for_article, mark_cancelled, mark_conflict, complete_applied | `test_obsidian_proposal_repository.py`, UNKNOWN отдельно |
| `vault_confirmation_repository.py` | save, find_active, get_by_id, decide | `test_vault_repositories.py`, UNKNOWN отдельно |
| `vault_note_repository.py` | upsert, get_by_path, list_for_vault, delete_paths | `test_vault_repositories.py` |
| `vault_instruction_repository.py` | replace_for_vault, list_for_vault | `test_vault_repositories.py` |
| `vault_sync_lease_repository.py` | acquire, get, release, run_exclusive; fence/expiry/owner | `test_vault_repositories.py`, `test_vault_lease_fencing.py` |
| `vault_chunk_index_repository.py` | get_state, list_for_note/vault, invalidate, list_stale_note_ids, mark_note_current, replace_for_note/vault, delete_for_notes, mark_current | `test_vault_chunk_index_repository.py` |
| `vault_embedding_index_repository.py` | get_state, list_for_vault, list_metadata_for_vault, invalidate, save_batch, complete_generation | `test_vault_embedding_index_repository.py`, `test_embedding_checkpoints.py` |
| `vault_full_text_search_repository.py` | search; синхронность FTS после insert/update/delete chunks | `test_vault_full_text_search.py` |
| `github_connection_state_store.py` | acquire_attempt, has_active_attempt, release_attempt | `test_github_connection_state_store.py` |
| `github_installation_writer.py` | replace_for_user, account/installations в одной транзакции | `test_github_installation_writer.py` |
| `github_vault_selection_manager.py` | get_selected, select, get_pending, request_disconnect, decide | `test_vault_selection.py` |
| `github_vault_sync_manager.py` | sync, sync_if_stale, get_status; SQL transactions/lease/index, внешние ports — fakes | `test_vault_sync.py`, `test_vault_lease_fencing.py` |

Короткие имена Obsidian-файлов после первой строки относятся к тому же
`capabilities/obsidian/data/sqlite/`. Общие `connection`, `migration_runner`,
`runtime_schema`, `vault_lease_fence` и `vault_process_lock` — инфраструктура,
не дополнительные repositories; их транзакционные/lease ограничения должны
быть охвачены соответствующими сценариями. Мапперы/DTO включены через imports
старого кода и отдельные проверки UNKNOWN.

## Связать результат с CI и predeploy

Доверенный отчёт содержит previous release/tag и полный SHA, candidate SHA,
SQL digest обоих источников, точные журналы, fingerprint проверенной target
схемы, версии среды, digest runner/дополнительных tests, manifest test IDs и
результаты обоих наборов. Обязательны CI repository/workflow/ref/run/attempt
и проверка required jobs/steps точного candidate SHA. JSON `passed=true`
без доверенного происхождения не является допуском. Baseline policy и полноту
результатов проверяет controller; исполняемый candidate не выбирает себе базу
и не может исключить неудобные операции из обязательной матрицы.

Перед deploy под lock сравнить installed SHA (`last_good`, фактические
контейнеры/OCI revision и pin) с previous SHA успешного CI. При несовпадении,
отсутствии evidence или сбое API отказать до `writers_stopped`. Не заменять
базу ближайшим tag и не считать другой успешный SHA эквивалентным. Для другой
базы нужен новый полный допуск этого кандидата по доверенной политике.

Image ID сверяется только локально: оба контейнера и внешний pin должны
соответствовать сохранённому установленному образу. Потеря образа или mismatch
закрывают preflight; новый rebuild не восстанавливает доказанный pin.
Это не добавляет CI проверку старого Docker image.

На предварительной отдельной копии, полученной SQLite online backup API,
проверить candidate migrations, целостность, журнал и сохранность данных;
рабочую БД не монтировать в пробные процессы. Копия остаётся приватной на VPS,
не отправляется в CI; отчёт не содержит строк production. Непосредственно
перед остановкой повторно сверить установленный SHA/pin и действительность
результата. Окончательный снимок и переключение остаются задачей этапа 6.

## Проверить отказы и границу следующего этапа

Обязательны позитивные cases добавления совместимой колонки и удаления
неиспользуемой; негативные — удаление используемой колонки, CHECK/trigger,
ломающие old write, новая обязательная колонка без default, подмена baseline,
SQL или imports, неполная матрица и пропущенные tests. Синтетические мутации
проверяют чувствительность harness, но не выдают допуск реальному кандидату.
После них оба обязательных набора выполняются на неизменённых candidate SQL.

Открытые работы: подтвердить метод-за-методом покрытие точного предыдущего
релиза; реализовать fixture adapters и изоляцию environments; закрепить
доверенную baseline policy и формат evidence; испытать preflight mismatch
на стенде. Чтение кода не заменяет эти результаты, тесты новой реализации
в рамках этой правки документации не запускались.

Этап 5 заканчивается допуском до остановки. Полный startup обоих образов,
доставка runtime admission, переключение БД без чужого WAL, граница приёма,
rollback/recovery/reboot и установка нового controller snapshot относятся
к этапам 6–7. Нельзя объявить новый возврат работающим по одному успешному
repository gate. Действующий `_recover` и отказ после попытки новой миграции
остаются прежними до отдельной реализации и проверки следующего этапа.
