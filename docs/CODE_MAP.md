# Карта кода для разработки

Эта карта помогает найти начальные файлы для типовой задачи. Пути приложения
начинаются от `compadre_assistant/`; пути тестов — от `tests/`, остальные — от
корня репозитория. Строка карты
не перечисляет все затрагиваемые файлы и не заменяет поиск по коду. Границы
модулей и зависимости описаны в [архитектуре](ARCHITECTURE.md).

| Задача | Где начать поиск | Связанные тесты | Подробности |
| --- | --- | --- | --- |
| Общие команды, identity и режим чата | `core/presentation/incoming/`, `core/domain/chat_modes.py`, `core/domain/users/` | `test_incoming_commands.py`, `test_chat_modes.py`, `test_user_identity_repository.py` | [Путь сообщения](ARCHITECTURE.md#как-проходит-сообщение) |
| Личный выбор возможностей и граница допуска | `core/domain/capability_preferences.py`, `core/data/sqlite/capability_preference_repository.py`, `core/presentation/incoming/dispatcher.py`, `core/presentation/incoming/registry.py` | `test_capability_preferences_domain.py`, `test_capability_preference_repository.py`, `test_personal_processor_admission.py`, `test_incoming_boundary.py` | [Профиль и включённые возможности](ARCHITECTURE.md#профиль-и-включённые-возможности), [команды](RUN.md#13-посмотреть-доступные-команды) |
| Сборка и регистрация возможности | `bootstrap.py`, `core/presentation/incoming/registry.py`, `capabilities/ask/bootstrap.py` | `test_capability_composition.py`, `test_presentation_boundary.py` | [Сборка зависимостей](ARCHITECTURE.md#сборка-и-время-жизни-зависимостей) |
| Telegram, VK и доставка ответа | `presentation/telegram/bot.py`, `presentation/vk/bot.py`, `presentation/shared/safe_send.py` | `test_telegram_bot.py`, `test_vk_bot.py`, `test_shared_responses.py` | [Ответ и фоновая доставка](ARCHITECTURE.md#ответ-ошибка-и-фоновая-доставка) |
| Разовый вопрос Ask | `capabilities/ask/presentation/handler.py`, `capabilities/ask/domain/use_cases/ask_question.py` | `test_ask_question.py`, `test_ask_data.py`, `test_ask_channels.py` | [Одноразовый Ask](ARCHITECTURE.md#одноразовый-ask) |
| Статья, анализ и предложение | `capabilities/obsidian/presentation/processing.py`, `capabilities/obsidian/domain/articles/`, `capabilities/obsidian/domain/reviews/` | `test_process_article_url.py`, `test_prepare_obsidian_review.py`, `test_obsidian_proposal_confirmation.py` | [Pipeline статьи](ARCHITECTURE.md#pipeline-статьи-и-proposal) |
| GitHub App и подключение vault | `capabilities/obsidian/domain/vaults/`, `capabilities/obsidian/data/github/`, `capabilities/obsidian/presentation/vault_configuration.py` | `test_github_connection.py`, `test_vault_selection.py`, `test_github_app_client.py` | [Подключение vault](ARCHITECTURE.md#github-app-и-подключение-vault), [настройка App](GITHUB_APP.md) |
| Синхронизация vault и поиск | `capabilities/obsidian/domain/vaults/vault_sync.py`, `capabilities/obsidian/domain/search/`, `capabilities/obsidian/data/sqlite/github_vault_sync_manager.py` | `test_vault_sync.py`, `test_hybrid_search.py`, `test_embedding_checkpoints.py` | [Индекс и поиск](ARCHITECTURE.md#локальная-копия-vault-и-поиск), [диагностика](OPERATIONS.md#диагностика-embeddings) |
| Разбиение Markdown | `document_chunker/`, `capabilities/obsidian/data/chunking/document_note_chunker.py` | `test_document_chunker.py`, `test_document_note_chunker.py` | [Document chunker](DOCUMENT_CHUNKER.md) |
| SQLite и миграции | `core/data/sqlite/connection.py`, `core/data/sqlite/migration_runner.py`, `core/data/sqlite/migrations/0002_user_capability_preferences.sql`, repositories модуля-владельца данных | `test_sqlite_connection.py`, `test_migration_runner.py`, предметные `test_*repository.py` | [Правила схемы](../AGENTS.md#development-бд), [локальная БД](RUN.md#пересоздание-базы-после-изменения-схемы) |
| CI, публикация и установка версии | `.github/workflows/`, `scripts/`, `deploy/` | `test_publish_release.py`, `test_full_ci_admission.py`, `test_deploy_prod.py` | [Допуск релиза](RELEASES.md), [эксплуатация](OPERATIONS.md); перед VPS — [production](PRODUCTION.md) |

Для выбора команды проверки смотрите [RUN.md](RUN.md#проверка-изменения-кода).
Проверки CI и выпуск имеют отдельные условия допуска в [RELEASES.md](RELEASES.md).
