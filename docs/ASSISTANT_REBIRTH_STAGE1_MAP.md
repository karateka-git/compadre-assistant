# Этап 1: карта переноса Obsidian

Дата: 2026-10-04. Исходная ветка: `codex/assistant-rebirth`. Карта составлена по
текущим импортам, точкам сборки, SQL-схеме, входам Telegram/VK/CLI и тестам.
Это план переноса, исходный код и БД в этом шаге не менялись.

## Граница и временная структура

На этапе 1 создаётся `obs_chat_bot/capabilities/obsidian/` с `domain`,
`application`, `data`, `presentation` и `bootstrap.py`. Текущие use cases и
ports остаются внутри `application` возможности по временному правилу
`AGENTS.md`. Полный перенос их в `domain`, общий реестр возможностей и режимы
чата относятся к этапу 2. Публичный вход Obsidian должен принимать уже
идентифицированного пользователя и транспортно-независимое событие, а возвращать
результат, который общий вход может отдать каналу без импорта внутренних моделей
статьи, vault или proposal. Старые публичные пути после переноса удаляются;
callers и тесты обновляются вместе с ними.

## Файлы и ответственность

Все пути в первых двух столбцах ниже находятся внутри `obs_chat_bot/`.

| Сейчас | Куда | Ответственность |
| --- | --- | --- |
| `domain/articles/{entities,analysis,statuses}.py` | `capabilities/obsidian/domain/articles/` | Статья, результат анализа, статус. |
| `domain/vaults/entities.py` | `capabilities/obsidian/domain/vaults/` | GitHub account/installation, vault, заметка, инструкции, lease, подтверждение. |
| `domain/search/{entities,statuses}.py` | `capabilities/obsidian/domain/search/` | Chunks, embeddings, поисковые запросы/результаты и статусы fallback. |
| `domain/reviews/entities.py` | `capabilities/obsidian/domain/reviews/` | План, proposal и статусы write-back. |
| `application/articles/{analysis,processing,ports,html,extracted,errors,stages,url_extraction,url_utils}.py` | `capabilities/obsidian/application/articles/` | URL pipeline, извлечение, анализ, ошибки и ports. URL распознаётся внутри Obsidian. |
| `application/articles/incoming_messages.py` | Разделить: общее входное событие остаётся вне возможности; `SavedIncomingMessage` и модель журнала переходят в `capabilities/obsidian/application/articles/` | Текущий `IncomingMessage` одновременно служит событием канала и записью article flow; этот смешанный контракт нельзя переносить целиком в ядро или оставлять в адаптерах. |
| `application/vaults/{github_models,github_connection,ports,vault_configuration,vault_selection,vault_sync,markdown}.py` | `capabilities/obsidian/application/vaults/` | GitHub Device Flow, выбор и sync vault, Markdown и соответствующие ports. |
| `application/search/{article_query,errors,full_text,hybrid,indexing,models,ports,vector}.py` | `capabilities/obsidian/application/search/` | Поиск, индексирование и embeddings; `ArticleSearchQueryBuilder` остаётся предметным. |
| `application/reviews/{proposal,confirmation,ports}.py` | `capabilities/obsidian/application/reviews/` | Подготовка review, pending proposal, SHA preflight и write-back. |
| Предметные ветви `application/incoming/processing.py` и `application/incoming/commands.py` | `capabilities/obsidian/presentation/` с внутренним вызовом Obsidian use cases | Команды GitHub, vault, reanalyze, ссылки, `да`/`нет` для vault/proposal, сообщения и результаты Obsidian. |
| Предметные ветви `presentation/shared/responses.py` | `capabilities/obsidian/presentation/` | Форматирование статей, анализа, sync, GitHub, proposal и ошибок Obsidian. |
| `data/sqlite/{article_repository,article_dtos,article_mappers,analysis_result_repository,analysis_result_dtos,analysis_result_mappers}.py` | `capabilities/obsidian/data/sqlite/` | Статьи и результаты анализа. |
| `data/sqlite/{incoming_message_repository,incoming_message_dtos,incoming_message_mappers,processing_error_repository,processing_error_dtos}.py` | `capabilities/obsidian/data/sqlite/` | Журнал article flow и ошибки обработки; таблицы `incoming_messages` и `processing_errors` принадлежат Obsidian. Для `processing_errors` проверить явный mapper при переносе. |
| `data/sqlite/{github_installation_repository,github_installation_writer,github_connection_state_store,github_vault_selection_manager,github_vault_sync_manager}.py` | `capabilities/obsidian/data/sqlite/` | Доступ GitHub, межпроцессный claim и менеджеры с короткими SQLite-соединениями. |
| `data/sqlite/{obsidian_vault_repository,vault_note_repository,vault_instruction_repository,vault_confirmation_repository,vault_sync_lease_repository,vault_dtos,vault_mappers}.py` | `capabilities/obsidian/data/sqlite/` | Vault, заметки, инструкции, подтверждения и leases. |
| `data/sqlite/{vault_chunk_index_repository,vault_full_text_search_repository,vault_embedding_index_repository,chunk_dtos,chunk_mappers,embedding_dtos,embedding_mappers,obsidian_proposal_repository}.py` | `capabilities/obsidian/data/sqlite/` | FTS, chunks, embedding index и proposals. |
| `data/github/{github_app_client,jwt_signer}.py`; `data/http/{article_html_fetcher,url_safety}.py`; `data/extraction/trafilatura_article_extractor.py` | Соответствующие каталоги `capabilities/obsidian/data/` | Конкретные GitHub, HTTP и extraction adapters. |
| `data/llm/{openai_article_analyzer,openai_obsidian_proposal_generator}.py`; `data/embeddings/{openai_compatible,dtos,mappers}.py`; `data/chunking/document_note_chunker.py` | Соответствующие каталоги `capabilities/obsidian/data/` | LLM, embedding и адаптер независимого chunker. |
| Obsidian-фабрики из `bootstrap.py` | `capabilities/obsidian/bootstrap.py` | Сборка URL/analysis/review/search/GitHub/vault; общий root вызывает сборку возможности. |

Файлы `__init__.py` перемещаются вместе со своими предметными пакетами.
`document_chunker/` остаётся отдельной библиотекой; её импортирует только
Obsidian adapter и сборка этого adapter.

## Общая часть, которую нельзя затянуть в Obsidian

| Сейчас | Ответственность на этапе 1 |
| --- | --- |
| `domain/users/entities.py`, `application/users/{identity,ports}.py`, `data/sqlite/user_identity_repository.py` | Общая регистрация, имя, внешние identities, коды связывания и rebind. На этапе 1 допустим прежний общий `application`; в `core` переедет на этапе 2. |
| Общие ветви `application/incoming/processing.py` и `commands.py` | Вход, разрешение identity, `/start`, `/help`, `/register`, `/name`, `/link_code`, `/link` и ответы связывания. `/status` сейчас включает vault и требует разделения: профиль общий, состояние vault возвращает публичный вход Obsidian. Онбординг с требованием vault также не должен делать core зависимым от `ObsidianVault`. |
| Общие ветви `presentation/shared/responses.py` | Тексты регистрации, имени, связывания и общей справки; состав команд Obsidian можно добавлять из публичного описания возможности. |
| `presentation/telegram/bot.py`, `presentation/vk/bot.py` | Получение событий, адрес ответа, split/retry/safe-send, фоновая доставка. Они не должны импортировать URL extractor, модели article journal и Obsidian-result; правило раннего ack для долгой операции требует нейтрального сигнала от входного контракта. |
| `presentation/cli/main.py`, `__main__.py` | Запуск и диагностические команды. `--process-url`/smoke используют публичную сборку Obsidian; создание конкретных Obsidian-зависимостей уходит из CLI. Smoke-сценарии находятся в `capabilities/obsidian/smoke.py`. |
| `data/config.py` | Единое чтение env и `AppConfig` на этапе 1; `GitHubAppConfig`, `EmbeddingConfig`, `ChunkingConfig` и note tag передаются в Obsidian bootstrap. Независимые настройки возможностей можно отделить позднее. |
| `data/sqlite/{connection,migration_runner}.py`, `data/sqlite/migrations/0001_initial_schema.sql` | Общее открытие SQLite (WAL, busy timeout, FK) и порядок инициализации. SQL-файл остаётся единым, физическое расположение не меняется, путь в `AGENTS.md` остаётся верен. Таблицы `app_users`, `external_identities`, `identity_link_tokens`, `identity_rebind_confirmations` общие; остальные предметные таблицы, включая `github_*`, `incoming_messages`, `processing_errors`, `articles`, `analysis_results`, `obsidian_*`, принадлежат Obsidian. |
| `bootstrap.py` | Главный composition root: общие зависимости и явное подключение Obsidian. |

## Связи, требующие аккуратного разреза

1. `ProcessIncomingMessageUseCase` сейчас содержит оба flow в одном классе.
   Его `ProcessIncomingMessageResult` включает `SavedIncomingMessage`, результаты
   анализа, vault и proposal; вынос только файлов `articles` оставит зависимость
   общего входа от Obsidian. Сначала нужен узкий публичный контракт входа/ответа
   возможности, затем разделение класса и formatter.
2. `IncomingMessage` содержит `app_user_id=1` по умолчанию и одновременно
   нормализует событие TG/VK. При разделении нельзя потерять `channel`, `chat_id`,
   `message_id`, внешний user ID и обратный адрес фонового ответа. Journal
   Obsidian создаётся только для article pipeline; общая обработка не обязана
   писать каждое событие. `article_id` остаётся внутри его repository/DTO.
3. `да`/`нет` сейчас сначала подтверждают rebind, затем vault replacement,
   disconnect и proposal; pending proposal блокирует команды кроме `/status` и
   `/github_status`. После разделения сохранить приоритет rebind в общем входе,
   а порядок предметных подтверждений внутри Obsidian. Статус и onboarding
   требуют публичного ответа Obsidian вместо чтения vault из общего кода.
4. Device Flow хранит только claim в `github_connection_attempts`, а challenge
   и callback исходного чата — в памяти `GitHubConnectionCoordinator`.
   `SQLiteGitHubAccountAccessWriter`, vault selection/sync managers открывают
   отдельные соединения по пути БД. При переносе сохранить процессный
   coordinator для каждого канала, межпроцессный claim и доставку callback;
   после перезапуска проверить истечение claim и возможность начать новый flow.
5. CLI сейчас собирает весь процесс на одно входящее сообщение, открывая
   SQLite worker connection; менеджеры GitHub используют отдельные соединения.
   Перенос фабрик не должен передавать закрытое соединение в background thread.
6. `obsidian_proposals` и подтверждения vault привязаны к `app_user_id`, а
   несколько каналов могут быть связаны с одним пользователем. Сохранить
   owner checks, pending exclusivity, leases, актуальность SHA и атомарный
   write-back. SQL foreign keys на `app_users` допустимы: это ссылочная связь,
   не разрешение ядру читать внутренние таблицы Obsidian.
7. `presentation/shared/responses.py` использует `ChatCommand`, статусы статьи,
   search coverage, GitHub и proposal; `telegram/bot.py` и `vk/bot.py` также
   напрямую импортируют Obsidian URL extractor для ack. После разделения
   проверить, что адаптеры знают только общий контракт события/ответа.

## Порядок реализации и проверки

1. **Граница данных:** перенести Obsidian domain, SQLite DTO/mappers/repositories
   и остальные data adapters. Оставить общие user repositories и SQLite lifecycle.
   Схему и рабочую БД не трогать. Проверить repository, FTS, leases, изоляцию.
2. **Сценарии:** перенести `articles`, `vaults`, `search`, `reviews` с временным
   `application`, обновить импорты data/тестов и удалить старые модули. Проверить
   URL pipeline, анализ, vault sync, индексирование, search и proposals.
3. **Общий вход:** ввести независимое от article journal событие и публичный
   вход Obsidian, разделить смешанный use case и приоритеты команд/подтверждений.
   Проверить регистрацию и связывание TG/VK вместе с article flow.
4. **Presentation:** перенести Obsidian-команды и форматирование, обновить
   Telegram/VK и CLI на общий контракт, сохранить фоновый callback. Проверить
   ответы, длинные сообщения и раннее подтверждение обработки.
5. **Сборка:** перенести фабрики в `capabilities/obsidian/bootstrap.py`, оставить
   общий composition root и SQLite lifecycle; убрать старые импорты без wrappers.
6. **Сквозная регрессия:** пройти полный unittest, три автономных smoke и
   healthcheck на временной БД по baseline. Отдельно проверить TG/VK/CLI,
   Device Flow, межпроцессный claim и восстановление после перезапуска,
   дубликаты, FTS fallback, leases, preview, подтверждение, SHA и конфликты.
   Живые каналы и внешние API отмечать как непроверенные, если нет настроек.
7. **Документация:** сверить фактическую структуру с roadmap и `AGENTS.md`,
   отметить выполненными только проверенные пункты этапа 1.

Тесты, наиболее чувствительные к границе: `test_process_incoming_message.py`,
`test_incoming_commands.py`, `test_shared_responses.py`, `test_telegram_bot.py`,
`test_vk_bot.py`, `test_telegram_flow_smoke.py`, `test_cli_process_url.py`,
`test_incoming_message_repository.py`, `test_processing_error_recorder.py`,
`test_user_identity_repository.py`, а также `test_vault_*`, `test_github_*`,
`test_hybrid_search.py`, `test_prepare_obsidian_review.py` и
`test_obsidian_proposal_confirmation.py`. Перед изменением каждого API нужно
обновлять его callers и относящиеся к нему тесты в том же шаге.
