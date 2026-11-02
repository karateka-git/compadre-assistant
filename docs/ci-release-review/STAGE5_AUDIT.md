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

## Зафиксировать проверенную пару SHA и источник baseline

Read-only проверка GitHub 02.11.2026: [Release v3.1.0](https://github.com/karateka-git/compadre-assistant/releases/tag/v3.1.0)
имеет ID `431378458`, `draft=false`, `prerelease=false`, `immutable=true`,
`published_at=2026-11-02T11:25:08Z`; `target_commitish` и прямой
`refs/tags/v3.1.0` (`object.type=commit`) совпадают:
`dd71c2ba42a0744b5b654cedcf73009ce8879058`. На момент запроса списка Releases
это последняя публикация. Локального тега `v3.1.0` нет; доверенный источник
этого соответствия — GitHub Releases API и Git refs API, а не локальный tag list.
Использованы read-only `gh api repos/karateka-git/compadre-assistant/releases/tags/v3.1.0`,
`git/ref/tags/v3.1.0` и постраничный `/releases` того же репозитория.

| Объект аудита | Точное значение |
| --- | --- |
| Previous | `v3.1.0`, `dd71c2ba42a0744b5b654cedcf73009ce8879058` |
| Candidate исходников при аудите | `e35be22e8b211e233647d34d974bd008b918f956`, ветка `codex/ci-release-review-plan`; это ещё не опубликованный Release |
| Git tree приложения, одинаковый у пары | `2dc64664d9ee94f32f409d2b4e3a461d4d848745` |
| Git tree tests, одинаковый у пары | `e4dd77ddd456473c0eafbcf51bf8ddf681b8cabf` |
| Git tree migrations, одинаковый у пары | `d5f3fb7ba4f534b467dbebf178c4126f25a0f747` |
| Git blob requirements.txt, одинаковый у пары | `9c2586c7bf97cbb2efa5ec121e61e2c931b87573` |

`git diff previous candidate -- compadre_assistant tests requirements.txt`
пуст. Поэтому прочитанные в этом worktree Python/tests являются кодом previous
по Git-содержимому; никаких новых repository API или SQL в candidate пока нет.
После следующих коммитов candidate SHA и digest надо получить заново.
Изменения этого документа также ещё не входят в указанный SHA.

У обоих источников четыре SQL-миграции; SHA-256 ниже рассчитан по байтам
`git show <точный-SHA>:<путь>`, а не по CRLF рабочей копии:

| SQL в `compadre_assistant/core/data/sqlite/migrations/` | SHA-256 previous = candidate |
| --- | --- |
| `0001_initial_schema.sql` | `2d0921c93034128fbb338c7658fe23b6c33c3e17f7d086e0333f5f621e3ce762` |
| `0002_user_capability_preferences.sql` | `cf7e73eebfd34f91aeec80b90f24fd127ed2f1c4fdb2524df0b9fac7386c39e4` |
| `0003_backfill_existing_user_names.sql` | `246f0cbaf85bb478df1f35c27c22dfe0b5938f934a4cc87ca7b0b0d9a68af761` |
| `0004_private_chat_address_lookup.sql` | `6bf16497a8b63828e0ccfbe12ea72c0984311f3286dc63b92bc3c5cd8d0de169` |

[Main-full 41436078724/1](https://github.com/karateka-git/compadre-assistant/actions/runs/41436078724/attempts/1)
повторно прочитан через API: `workflow_dispatch`, `main-full.yml`, `head_branch=main`,
точный previous SHA, `completed/success`. Это подтверждение метаданных прежнего
прогона; в 5.1 не перечитывались все jobs/steps/logs и не выдавался новый полный
допуск. Исторический VPS-снимок выше не обновлялся, текущее установленное
состояние неизвестно без нового разрешённого predeploy.

Действующий `full_ci_controller.previous_release_sha` выбирает для `main`
последний **по published_at** глобальный Release, для существующей линии —
последний выпуск линии; при отсутствии её выпусков использует глобальный,
до первой публикации — bootstrap с проверкой ancestry. Это не доказательство
installed SHA, и последняя публикация может быть backport. В 5.2–5.4 закрепить
эту начальную базу через доверенную policy controller, подтвердить immutable
Release/tag/SHA и не разрешать candidate input подменять baseline. Для другой
установленной базы требуется отдельный допуск, а не автоматическая смена базы.
Нынешние workflows вызывают controller из candidate checkout; одного
`previous-release-sha` input reusable workflow недостаточно как evidence доверия.

## Прочитать матрицу публичных операций

Инвентаризация точного previous включает **26 классов и 107 публичных методов**
SQLite adapters. Сканировались все Python-файлы `compadre_assistant`, включая
файлы вне `*repository.py`; constructors без операций с БД не считаются
отдельными операциями. Managers включены даже там, где SQL делегирован repositories:
они открывают собственные соединения и формируют транзакционные границы.
Инфраструктурные функции и исключения перечислены после матрицы.

Все непустые строки ниже используют источник **M**: `apply_migrations(connection)`
из прежнего `migration_runner.py`, default каталог `core/data/sqlite/migrations/`,
версии `0001`–`0004`, отдельные временные БД. В будущем old gate заменяет только
SQL-ресурсы на candidate bytes до import; здесь SQL совпадает и не подменялся.
Прямые INSERT helpers (`tests/sqlite_helpers.ensure_app_user`, `_prepare_vault`,
`_prepare_installations` и аналоги) заполняют данные, не создают альтернативную схему.

Статус **П** означает прямой вызов с проверкой результата/сохранённых данных
или отказа в указанном поведенческом тесте; он не означает полноту edge cases.
**К** означает наблюдавшийся косвенный вызов через service либо вызов при
подготовке fixture, без прямой проверки результата именно этого метода
в указанном тесте. Сценарий может проверять общее поведение сервиса.
**Пробел** означает, что SQLite-поведение метода не подтверждено данным аудитом:
его надо покрыть отдельным compatibility test, а не считать зелёным по имени
похожего domain-теста. Полнота допуска не выводится из количества вызовов.

Test ID получается как `<префикс>.<test_...>`; все префиксы относятся к
неизменным tests previous SHA. Их ссылки открывают текущие совпадающие файлы.

| Обозначение | Префикс unittest ID |
| --- | --- |
| T01 | [`tests.test_analysis_result_repository.SQLiteArticleAnalysisResultRepositoryTest`](../../tests/test_analysis_result_repository.py) |
| T02 | [`tests.test_article_user_isolation.ArticleUserIsolationTest`](../../tests/test_article_user_isolation.py) |
| T03 | [`tests.test_capability_composition.CapabilityCompositionTest`](../../tests/test_capability_composition.py) |
| T04 | [`tests.test_capability_preference_repository.CapabilityPreferenceRepositoryTest`](../../tests/test_capability_preference_repository.py) |
| T05 | [`tests.test_chat_modes.ChatModesTest`](../../tests/test_chat_modes.py) |
| T06 | [`tests.test_embedding_checkpoints.EmbeddingCheckpointsTest`](../../tests/test_embedding_checkpoints.py) |
| T07 | [`tests.test_github_connection_state_store.GitHubConnectionStateStoreTest`](../../tests/test_github_connection_state_store.py) |
| T08 | [`tests.test_github_installation_writer.GitHubInstallationWriterTest`](../../tests/test_github_installation_writer.py) |
| T09 | [`tests.test_incoming_message_repository.SQLiteIncomingMessageRepositoryTest`](../../tests/test_incoming_message_repository.py) |
| T10 | [`tests.test_linked_chat_menus.LinkedChatMenusTest`](../../tests/test_linked_chat_menus.py) |
| T11 | [`tests.test_menu_reconciliation.MenuReconciliationTest`](../../tests/test_menu_reconciliation.py) |
| T12 | [`tests.test_obsidian_proposal_repository.SQLiteObsidianProposalRepositoryTests`](../../tests/test_obsidian_proposal_repository.py) |
| T13 | [`tests.test_operation_scopes.OperationScopesTest`](../../tests/test_operation_scopes.py) |
| T14 | [`tests.test_processing_error_recorder.SQLiteProcessingErrorRecorderTest`](../../tests/test_processing_error_recorder.py) |
| T15 | [`tests.test_user_identity_repository.SQLiteUserIdentityRepositoryTest`](../../tests/test_user_identity_repository.py) |
| T16 | [`tests.test_vault_chunk_index_repository.VaultChunkIndexRepositoryTest`](../../tests/test_vault_chunk_index_repository.py) |
| T17 | [`tests.test_vault_embedding_index_repository.VaultEmbeddingIndexRepositoryTest`](../../tests/test_vault_embedding_index_repository.py) |
| T18 | [`tests.test_vault_full_text_search.VaultFullTextSearchTest`](../../tests/test_vault_full_text_search.py) |
| T19 | [`tests.test_vault_lease_fencing.VaultLeaseFencingTest`](../../tests/test_vault_lease_fencing.py) |
| T20 | [`tests.test_vault_repositories.VaultRepositoriesTest`](../../tests/test_vault_repositories.py) |
| T21 | [`tests.test_vault_selection.GitHubVaultSelectionServiceTest`](../../tests/test_vault_selection.py) |

### `SQLiteArticleAnalysisResultRepository`

Источник: [analysis_result_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/analysis_result_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `save` | `T01.test_save_creates_analysis_result` | M / П |
| `get_by_id` | `T01.test_get_by_id_returns_saved_result` | M / П |
| `get_latest_for_article` | `T01.test_get_latest_for_article_returns_newest_result` | M / П |

### `SQLiteArticleFlowLeaseRepository`

Источник: [article_flow_lease_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/article_flow_lease_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `acquire` | `T13.test_simultaneous_same_url_is_reserved_for_one_full_flow` | M / К |
| `release` | `T13.test_simultaneous_same_url_is_reserved_for_one_full_flow` | M / К |

### `SQLiteArticleRepository`

Источник: [article_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/article_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `create` | `T02.test_same_normalized_url_can_be_saved_for_different_users` | M / П |
| `get_by_id` | `T02.test_same_normalized_url_can_be_saved_for_different_users` | M / К |
| `find_by_normalized_url` | `T02.test_same_normalized_url_can_be_saved_for_different_users` | M / П |
| `find_by_text_hash` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `update_status` | `T13.test_concurrent_confirmations_keep_remote_write_sequential` | M / К |
| `update_content` | `T13.test_concurrent_confirmations_keep_remote_write_sequential` | M / К |

### `SQLiteGitHubConnectionStateStore`

Источник: [github_connection_state_store.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/github_connection_state_store.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `acquire_attempt` | `T07.test_connection_attempt_is_shared` | M / П |
| `has_active_attempt` | `T07.test_connection_attempt_is_shared` | M / П |
| `release_attempt` | `T07.test_connection_attempt_is_shared` | M / П |

### `SQLiteGitHubInstallationRepository`

Источник: [github_installation_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/github_installation_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `replace_for_user` | `T20.test_installation_diff_preserves_or_cascades_active_vault` | M / П |
| `list_for_user` | `T08.test_writer_opens_fresh_connection_and_persists_only_ids` | M / П |
| `contains` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `delete_for_user` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |

### `SQLiteGitHubAccountAccessWriter`

Источник: [github_installation_writer.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/github_installation_writer.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `replace_for_user` | `T08.test_writer_opens_fresh_connection_and_persists_only_ids` | M / П |

### `SQLiteGitHubVaultSelectionManager`

Источник: [github_vault_selection_manager.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/github_vault_selection_manager.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `get_selected` | `T21.test_sqlite_manager_selects_vault_after_request_connection_is_closed` | M / П |
| `select` | `T21.test_sqlite_manager_selects_vault_after_request_connection_is_closed` | M / П |
| `get_pending` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `request_disconnect` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `decide` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |

### `SQLiteGitHubVaultSyncManager`

Источник: [github_vault_sync_manager.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/github_vault_sync_manager.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `sync` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `sync_if_stale` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `get_status` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |

### `SQLiteIncomingMessageRepository`

Источник: [incoming_message_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/incoming_message_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `save` | `T09.test_save_returns_existing_message_for_duplicate_external_id` | M / П |
| `link_to_article` | `T09.test_link_to_article_updates_saved_message` | M / П |
| `get_by_id` | `T09.test_link_to_article_updates_saved_message` | M / К |
| `find_by_external_id` | `T09.test_save_returns_existing_message_for_duplicate_external_id` | M / К |

### `SQLiteObsidianProposalRepository`

Источник: [obsidian_proposal_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/obsidian_proposal_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `save_pending` | `T12.test_add_completion_updates_note_vault_article_and_history` | M / К |
| `list_pending` | `T13.test_configuration_decision_checks_ttl_version_and_shared_write_lease` | M / К |
| `get_by_id` | `T12.test_add_completion_updates_note_vault_article_and_history` | M / К |
| `get_active_for_article` | `T12.test_only_one_active_cycle_is_allowed_per_article` | M / К |
| `claim` | `T12.test_add_completion_updates_note_vault_article_and_history` | M / К |
| `release_claim` | `T13.test_expired_lease_taken_during_preflight_blocks_remote_commit` | M / К |
| `get_latest_applied_for_article` | `T12.test_applied_history_survives_vault_replacement` | M / П |
| `mark_cancelled` | `T13.test_two_local_proposals_require_id_and_do_not_consider_remote_pending` | M / К |
| `mark_conflict` | `T13.test_lease_busy_keeps_both_proposals_then_stale_sha_conflicts` | M / К |
| `complete_applied` | `T12.test_add_completion_updates_note_vault_article_and_history` | M / П |

### `SQLiteObsidianVaultRepository`

Источник: [obsidian_vault_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/obsidian_vault_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `create_if_absent` | `T21.test_select_same_vault_does_not_replace_saved_row` | M / К |
| `replace` | `T20.test_replacing_vault_removes_previous_notes` | M / П |
| `get_by_id` | `T20.test_sync_state_update_is_scoped_by_user` | M / К |
| `get_for_user` | `T20.test_sync_state_update_is_scoped_by_user` | M / П |
| `update_sync_state` | `T20.test_sync_state_update_is_scoped_by_user` | M / П |
| `delete_for_user` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |

### `SQLiteProcessingErrorRecorder`

Источник: [processing_error_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/processing_error_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `record` | `T14.test_record_saves_processing_error` | M / П |

### `SQLiteVaultChunkIndexRepository`

Источник: [vault_chunk_index_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/vault_chunk_index_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `get_state` | `T16.test_invalidation_hides_stale_generation` | M / П |
| `list_for_note` | `T16.test_note_replacement_preserves_unchanged_chunk_identity` | M / П |
| `list_for_vault` | `T16.test_full_rebuild_saves_chunks_and_current_signature` | M / П |
| `invalidate` | `T16.test_invalidation_hides_stale_generation` | M / К |
| `list_stale_note_ids` | `T16.test_missing_global_marker_reconciles_only_dirty_note` | M / П |
| `mark_note_current` | `T16.test_full_rebuild_saves_chunks_and_current_signature` | M / К |
| `replace_for_note` | `T16.test_full_rebuild_saves_chunks_and_current_signature` | M / К |
| `delete_for_notes` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `replace_for_vault` | `T19.test_takeover_fences_all_local_publications` | M / К |
| `mark_current` | `T16.test_full_rebuild_saves_chunks_and_current_signature` | M / К |

### `SQLiteVaultActionConfirmationRepository`

Источник: [vault_confirmation_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/vault_confirmation_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `save` | `T20.test_confirmation_persists_replacement_and_disconnect_with_ttl` | M / К |
| `find_active` | `T20.test_confirmation_persists_replacement_and_disconnect_with_ttl` | M / П |
| `get_by_id` | `T20.test_confirmation_persists_replacement_and_disconnect_with_ttl` | M / К |
| `decide` | `T13.test_configuration_decision_checks_ttl_version_and_shared_write_lease` | M / К |

### `SQLiteVaultEmbeddingIndexRepository`

Источник: [vault_embedding_index_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/vault_embedding_index_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `get_state` | `T17.test_initial_index_stores_float32_blob_and_profile` | M / П |
| `list_for_vault` | `T17.test_initial_index_stores_float32_blob_and_profile` | M / П |
| `list_metadata_for_vault` | `T17.test_second_index_does_not_request_unchanged_chunks` | M / К |
| `invalidate` | `T17.test_initial_index_stores_float32_blob_and_profile` | M / К |
| `save_batch` | `T06.test_batch_rejects_changed_source_and_rolls_back_all_items` | M / П |
| `complete_generation` | `T06.test_finalization_rejects_missing_stale_and_mixed_vectors` | M / П |

### `SQLiteVaultFullTextSearchRepository`

Источник: [vault_full_text_search_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/vault_full_text_search_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `search` | `T18.test_search_indexes_title_path_tags_headings_and_chunk_text` | M / К |

### `SQLiteVaultInstructionRepository`

Источник: [vault_instruction_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/vault_instruction_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `replace_for_vault` | `T20.test_instruction_replace_preserves_order_and_isolates_vault` | M / П |
| `list_for_vault` | `T20.test_instruction_replace_preserves_order_and_isolates_vault` | M / К |

### `SQLiteVaultNoteRepository`

Источник: [vault_note_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/vault_note_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `upsert` | `T20.test_note_upsert_replaces_markdown_and_ordered_metadata` | M / П |
| `get_by_path` | `T20.test_note_queries_and_foreign_keys_enforce_user_isolation` | M / П |
| `list_for_vault` | `T20.test_replacing_vault_removes_previous_notes` | M / П |
| `delete_paths` | `T18.test_metadata_and_chunk_changes_refresh_fts_without_stale_rows` | M / К |

### `SQLiteVaultSyncLeaseRepository`

Источник: [vault_sync_lease_repository.py](../../compadre_assistant/capabilities/obsidian/data/sqlite/vault_sync_lease_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `acquire` | `T20.test_lease_blocks_second_connection_until_expiration` | M / П |
| `get` | `T20.test_lease_blocks_second_connection_until_expiration` | M / К |
| `release` | `T20.test_lease_blocks_second_connection_until_expiration` | M / П |
| `run_exclusive` | `T13.test_concurrent_confirmations_keep_remote_write_sequential` | M / К |

### `SQLiteUserCapabilityPreferenceRepository`

Источник: [capability_preference_repository.py](../../compadre_assistant/core/data/sqlite/capability_preference_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `get_for_user` | `T04.test_concurrent_distinct_ids_and_new_connection` | M / П |
| `upsert` | `T04.test_concurrent_distinct_ids_and_new_connection` | M / К |

### `SQLiteChatModeRepository`

Источник: [chat_mode_repository.py](../../compadre_assistant/core/data/sqlite/chat_mode_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `get` | `T05.test_modes_are_isolated_by_user_channel_and_chat` | M / П |
| `get_state` | `T05.test_only_one_connection_can_replace_the_same_revision` | M / П |
| `replace_if_revision` | `T05.test_only_one_connection_can_replace_the_same_revision` | M / П |
| `save` | `T05.test_modes_are_isolated_by_user_channel_and_chat` | M / К |
| `activate_if_absent` | `T05.test_concurrent_first_selection_does_not_replace_winner` | M / П |
| `clear` | `T05.test_clear_leaves_profile_and_other_chat` | M / П |

### `SQLiteAppUserRepository`

Источник: [user_identity_repository.py](../../compadre_assistant/core/data/sqlite/user_identity_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `create` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `get_by_id` | `T15.test_confirm_rebind_moves_channel_and_deletes_empty_previous_user` | M / П |
| `update_display_name` | `T15.test_register_creates_user_and_resolves_same_identity` | M / К |
| `delete` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |

### `SQLiteExternalIdentityRepository`

Источник: [user_identity_repository.py](../../compadre_assistant/core/data/sqlite/user_identity_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `find_private_chat` | `T10.test_enumeration_rejects_blank_ambiguous_and_deleted_addresses` | M / К |
| `has_chat_address` | `T11.test_ambiguous_address_blocks_point_lookup_and_origin_fallback` | M / К |
| `list_all_private_chats` | `T11.test_periodic_pass_enumerates_globally_once` | M / К |
| `list_for_user` | `T10.test_enumeration_rejects_blank_ambiguous_and_deleted_addresses` | M / К |
| `find` | `T15.test_register_creates_user_and_resolves_same_identity` | M / К |
| `create` | `T10.test_enumeration_rejects_blank_ambiguous_and_deleted_addresses` | M / К |
| `reassign` | `T10.test_rebind_during_menu_read_discards_previous_owner_menu` | M / К |
| `count_for_user` | Не подтверждён; отдельный compatibility test в 5.3 | M требуется / **Пробел** |
| `touch` | `T10.test_delivery_and_enumeration_errors_preserve_text_and_other_updates` | M / К |

### `SQLiteIdentityLinkTokenRepository`

Источник: [user_identity_repository.py](../../compadre_assistant/core/data/sqlite/user_identity_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `create` | `T15.test_link_code_binds_second_channel_to_same_user` | M / К |
| `find_active` | `T15.test_confirm_rebind_moves_channel_and_deletes_empty_previous_user` | M / К |

### `SQLiteIdentityLifecycleRepository`

Источник: [user_identity_repository.py](../../compadre_assistant/core/data/sqlite/user_identity_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `register_with_name` | `T15.test_register_creates_user_and_resolves_same_identity` | M / К |
| `link` | `T15.test_link_code_binds_second_channel_to_same_user` | M / К |

### `SQLiteIdentityRebindConfirmationRepository`

Источник: [user_identity_repository.py](../../compadre_assistant/core/data/sqlite/user_identity_repository.py).

| Метод | Поведенческий тест previous | Схема / результат аудита |
| --- | --- | --- |
| `save` | `T15.test_confirm_rebind_moves_channel_and_deletes_empty_previous_user` | M / К |
| `find` | `T03.test_disabled_obsidian_preserves_article_and_mode_without_building_entry` | M / К |
| `decide` | `T15.test_confirm_rebind_moves_channel_and_deletes_empty_previous_user` | M / К |

## Учесть инфраструктуру и границы schema gate

Поиск прямого SQL не ограничен классами выше. Следующие публичные функции
имеют самостоятельную ответственность; их нельзя потерять при выборе tests.
Пути в первых пяти строках относятся к `compadre_assistant/`.

| Файл / публичная операция | Тест / источник схемы | Решение |
| --- | --- | --- |
| `core/data/sqlite/connection.py`: `connect_database` | M во всех repository fixtures; `test_sqlite_connection.SQLiteConnectionTest.test_enable_wal_retries_temporary_lock` использует fake | Настройку FK/WAL/busy timeout проверять на реальных БД; fake retry не доказывает candidate-schema behavior. |
| `core/data/sqlite/migration_runner.py`: `apply_migrations` | `test_migration_runner.MigrationRunnerTest.*`; реальные SQL и синтетические каталоги | Candidate migration suite; не включать весь модуль в old repository gate. |
| `core/data/sqlite/runtime_schema.py`: `schema_fingerprint`, `validate_compatible_schema`, `prepare_runtime_schema` | `test_runtime_schema.RuntimeSchemaTest.test_exact_admission_repeat_parallel_and_no_unknown_sql`, `test_same_journal_changed_schema_and_missing_journal_fail`, `test_strict_mode_and_migrate_ignore_runtime_admission`; M + synthetic migration/schema changes | Контракт admission/DDL, отдельный infrastructure suite. `validate_release_pair` и `migration_sql_fingerprint` не читают предметные SQLite-данные; они также остаются в infrastructure suite. |
| `capabilities/obsidian/data/sqlite/vault_lease_fence.py`: `fence_vault_write` | M; `T19.test_write_renews_lease_before_expiration`, `T19.test_takeover_fences_all_local_publications`, `T06.test_lost_or_expired_lease_blocks_checkpoint_and_marker` | Включить поведенческие сценарии в old gate: UPDATE lease и отказ должны быть атомарны с предметной записью. |
| `capabilities/obsidian/data/sqlite/vault_process_lock.py`: `vault_process_lock` | M; `T19.test_process_crash_releases_vault_lock_without_deleting_file`, `T13.test_concurrent_confirmations_keep_remote_write_sequential` | Прямой `PRAGMA database_list` и OS lock; проверка Windows не заменяет POSIX-ветку в Linux CI. |
| `scripts/deploy_prepare.py`: `backup_sqlite`, `validate_schema` | `test_deploy_prepare.DeployPrepareTests.test_backup_preserves_wal_data_and_validates_copy`, `test_schema_validation_requires_exact_migration_list`; custom `items` / `schema_migrations` | Controller infrastructure, не old repository behavior. Тесты остаются отдельно; production copy/preflight в 5.5 не реализованы. |
| `scripts/deploy_prod.py`: публичные `deploy`, `verify_pinned` через private `_applied_names` | `test_deploy_prod.DeployProdTests.*`; fake host и custom schema | Оркестрация выпуска и чтение журнала, не предметный repository. Recovery/startup относятся к этапу 6. |
| `scripts/check_full_migrations.py`: `assert_database`, `check_migrations`, `check_synthetic_pending_migration`; `scripts/probe_previous_code.py`: `main` | Настоящие SQL + synthetic pending SQL; probe читает app_users и вызывает только `get_by_id/update_display_name` | Действующая частичная проверка, не полная матрица; `previous_migrations/run_previous_code` извлекают источники/запускают subprocess. |

`bootstrap.py`, CLI и `capabilities/obsidian/smoke.py` собирают/вызывают эти
адаптеры; smoke проверяет ограниченные записи, не вводит отдельный repository.
DTO/mappers используют SQLite Row как вход преобразования, но сами не выполняют
SQL. `scripts/check_pr_fast.py` проверяет SQL-файлы/схему как CI-инструмент;
это не пользовательский data-adapter. `deploy/` содержит shell/systemd/Compose
обвязку, а не дополнительные предметные SQLite repositories.

### Исключить миграторные и custom-schema случаи по точному test ID

| Tests previous | Источник | Действие в 5.2–5.3 |
| --- | --- | --- |
| `test_migration_runner.MigrationRunnerTest.test_clean_install_applies_all_migrations_without_creating_profiles`, `test_two_adapters_can_initialize_one_empty_database` | M, жёсткое ожидание `[1, 2, 3, 4]` | Не запускать как старое поведение repositories после добавления SQL. Обновлять ожидания только в candidate migration suite. |
| Остальные `test_migration_runner.MigrationRunnerTest.*` | Synthetic последовательности/ошибки имён; реальные prefix `0001`, `0001`–`0003` и upgrade | Все 9 тестов относятся к migration suite, в том числе backfill имён, preferences и private-chat index; старые assertions не переписывать. |
| `test_unknown_status_boundaries.UnknownStatusBoundaryTests.test_unknown_proposal_action_cannot_complete_or_change_row` | `sqlite3.connect(':memory:')`, собственный минимальный `obsidian_proposals` без release CHECK/FK | Только synthetic unit. `complete_applied` отдельно покрывается M; UNKNOWN на настоящих SQL проверяется только если они действительно допускают raw значение. |
| `test_unknown_status_boundaries.UnknownStatusBoundaryTests.test_unknown_identity_status_does_not_consume_token_or_rebind` | Минимальные `identity_rebind_confirmations` и `external_identities`, без полного release contract | Только synthetic unit; `decide` требует отдельного M-сценария неизвестного статуса, если candidate разрешит его. Не ослаблять CHECK/FK. |
| Остальные 6 `UnknownStatusBoundaryTests` | DTO / Mock / domain fakes | Проверяют UNKNOWN boundaries, не совместимость SQL. Особенно `test_vault_mapper_and_selection_reject_unknown_before_decision` не является SQLite `decide` test, хотя переменная называется storage. |
| `test_vault_sync.VaultSyncTest.*` | `MemoryVaultRepository`, `MemoryNoteRepository`, `MemoryLeaseRepository` и другие fakes | Не закрывают ни один метод `SQLiteGitHubVaultSyncManager`; отдельные SQLite compatibility tests обязательны. |
| `test_process_article_url.ProcessArticleUrlUseCaseTest.test_execute_records_normalization_error` | In-memory repository (строка 335 файла) | Не закрывает `SQLiteArticleRepository.find_by_text_hash`. |
| `test_cli_migrate`, `test_runtime_schema`, `test_deploy_prepare`, `test_deploy_prod`, `test_full_ci_controller` | Смесь реальных и custom схем/миграций, temp Git fixtures | Отдельные infrastructure suites, не добавлять целыми модулями в old repository manifest. |

Три identity-теста с добавленным trigger (`T15.test_failure_after_identity_insert_rolls_back_both_records`,
`test_register_rolls_back_profile_when_identity_insert_violates_unique`,
`test_link_rolls_back_code_when_identity_insert_violates_unique`) начинают с M,
затем намеренно меняют схему для fault injection. Они полезны для rollback,
но результат помечается **M + fault trigger**, а не неизменённая candidate-схема.
Они не заменяют нормальный register/link на M. DML-повреждения fixtures в
`test_embedding_checkpoints` проверяют отказы и также требуют явной отметки;
добавлять CHECK OFF/FK OFF ради их прохождения нельзя.

### Закрыть обнаруженные пробелы

В проверенном наборе не наблюдались 14 публичных операций, отмеченных в
матрице. Поиск по всему `tests/` не нашёл тестов `SQLiteGitHubVaultSyncManager`;
`contains/delete_for_user/count_for_user` также не получили прямых SQLite checks.
Отсутствие вызова в выбранном наборе не выдаётся за доказательство отсутствия
любого возможного косвенного пути во всех tests; до включения в обязательный
manifest каждую такую операцию надо подтвердить точным тестом на M либо
добавить отдельный compatibility test против previous API.

Особенно легко перепутать одноимённые пути: `IdentityLifecycle.register_with_name`
сам делает INSERT, `IdentityRebindConfirmation.decide` сам удаляет пустой
профиль, `VaultActionConfirmation.decide` сам удаляет/заменяет vault. Их успех
не доказывает `AppUser.create/delete` или `ObsidianVault.delete_for_user`.
Каскад удаления chunks при удалении notes не вызывает `delete_for_notes`.

Для строк К в 5.3 нужны явные postconditions, а не только вход в метод:
owner/foreign-ID isolation, not-found и duplicate; счётчики CAS/version и
неизменность loser; правильный lease owner/expiry/release; atomic rollback
частичной записи. Для batch/replace дополнительно проверять сохранность
старых данных при отказе; для cascades — дочерние строки; для FTS — отсутствие
stale результатов после update/delete. Уже существующие прямые проверки
также не исчерпывают эти варианты.

`T12` доказывает normal/skip completion и сохранение истории после замены
vault; ещё требуется fault injection между несколькими writes
`complete_applied`. `T08` проверяет сохранённые account/installations, но не откат всей
транзакции writer при ошибке второй записи. Для article
lease интеграционный сценарий одного URL не заменяет явную проверку wrong owner
и expiry. Двусторонние previous↔candidate записи/upgrade всех модулей пока не
проверены; одинаковые SQL в этой паре не отменяют эти требования.

## Воспроизвести наблюдение 5.1 и не принять его за допуск

Локальное окружение: `.venv` worktree отсутствует; использован явный
`C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe`.
`pip check` успешен; Python `3.12.10`, SQLite `3.49.1`, Windows AMD64.
Все 8 прямых requirements совпали с pins previous/candidate:
`aiogram 3.29.1`, `aiohttp-socks 0.12.0`, `httpx 0.28.1`, `openai 2.46.0`,
`PyJWT 2.13.0`, `PyYAML 6.0.3`, `python-dotenv 1.2.2`, `trafilatura 2.1.0`.
Это не версии прежнего production image и не будущего Linux CI; транзитивный
lock и раздельные previous/candidate environments ещё не подготовлены.

Выполнены **147 tests, 0 errors, 0 failures, 0 skips** из 22 модулей.
Одноразовый `sys.setprofile`/`threading.setprofile` фиксировал входы в публичные
SQLite методы и путь `apply_migrations`; схема во всех наблюдённых fixtures
бралась из M текущего worktree. Код/tests/assertions/SQL не менялись.
Первый технический запуск через `loadTestsFromNames` дал 5 ImportError
(`channel_helpers`, `sqlite_helpers`, `test_incoming_boundary`): старые tests
используют и `tests.*`, и импорты из корня `tests/`. После добавления **только
каталога tests этого worktree** в `sys.path`, как при штатном discover,
полный выбранный набор успешно повторён. В 5.2 нужны оба пути старого checkout,
проверенные `__file__` и отсутствие candidate package, а не изменение imports.

Состав прогона (имена без `tests/test_` и `.py`):

- `analysis_result_repository`
- `article_user_isolation`
- `incoming_message_repository`
- `processing_error_recorder`
- `user_identity_repository`
- `capability_preference_repository`
- `chat_modes`
- `vault_repositories`
- `obsidian_proposal_repository`
- `vault_chunk_index_repository`
- `vault_embedding_index_repository`
- `embedding_checkpoints`
- `vault_full_text_search`
- `vault_lease_fencing`
- `github_connection_state_store`
- `github_installation_writer`
- `vault_selection`
- `operation_scopes`
- `linked_chat_menus`
- `menu_reconciliation`
- `telegram_flow_smoke`
- `capability_composition`

Повтор поведенческих tests без одноразовой трассировки: из корня этого
worktree запускать указанным Python `-m unittest discover -s tests -p
"test_<имя>.py"` для каждого из 22 модулей. Матрица фиксирует конкретные test IDs,
а не автоматическое включение любых будущих tests с похожим именем.

Это **не CI evidence совместимости**: нет изолированных previous/candidate
checkout/environments, SQL-подстановки, обязательного manifest с fail-closed
проверкой полноты, двусторонних данных, негативных мутаций или predeploy.
Трассировка лишь подтверждает фактические call paths и fixture source
в совпадающем коде; временный файл трассы удалён. 5.1 завершает аудит,
5.2–5.6 остаются открытыми. Production и два отменённых untracked scripts
не использовались и не менялись.

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

Открытые работы: закрыть пробелы и косвенное покрытие матрицы точного previous
релиза; реализовать fixture adapters и изоляцию environments; закрепить
доверенную baseline policy и формат evidence; испытать preflight mismatch
на стенде. Локальные 147 tests подтверждают только описанный аудит; новой
реализации gate в этой правке нет и её проверки не выполнялись.

Этап 5 заканчивается допуском до остановки. Полный startup обоих образов,
доставка runtime admission, переключение БД без чужого WAL, граница приёма,
rollback/recovery/reboot и установка нового controller snapshot относятся
к этапам 6–7. Нельзя объявить новый возврат работающим по одному успешному
repository gate. Действующий `_recover` и отказ после попытки новой миграции
остаются прежними до отдельной реализации и проверки следующего этапа.
