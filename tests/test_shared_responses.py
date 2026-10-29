from tests.response_assertions import content_text, response_text
"""Тесты форматирования общих ответов presentation-слоя."""
from compadre_assistant.capabilities.obsidian.domain.pending import PendingOrigin

from dataclasses import replace
from datetime import UTC, datetime
import unittest

from compadre_assistant.capabilities.obsidian.domain.articles.analyzing import AnalyzeArticleResult
from compadre_assistant.capabilities.obsidian.domain.articles.processing import (
    ProcessArticleUrlError,
    ProcessArticleUrlResult,
)
from compadre_assistant.capabilities.obsidian.domain.articles.stages import ProcessingStage
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageResult
from compadre_assistant.core.presentation.incoming.dispatcher import (
    IncomingMessageResultType,
    ProcessIncomingMessageResult,
)
from compadre_assistant.capabilities.obsidian.presentation.results import ObsidianResultType
from compadre_assistant.capabilities.obsidian.domain.search.models import EmbeddingIndexCoverage
from compadre_assistant.capabilities.obsidian.domain.reviews.confirmation import (
    ObsidianConfirmationResult,
    ObsidianConfirmationStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import (
    GitHubConnectionCompletion,
    GitHubConnectionCompletionStatus,
    GitHubConnectionStartResult,
    GitHubConnectionStartStatus,
    GitHubDeviceAuthorization,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_selection import (
    VaultDisconnectResult,
    VaultDisconnectStatus,
    VaultSelectionResult,
    VaultSelectionStatus,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_configuration import (
    VaultConfigurationError,
    VaultConfigurationErrorCode,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import (
    VaultStatus,
    VaultSyncResult,
    VaultSyncStatus,
    VaultSyncWarning,
    VaultSyncWarningReason,
)
from compadre_assistant.core.domain.users.identity import CreatedLinkCode
from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
from compadre_assistant.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import (
    ObsidianProposal,
    ObsidianProposalAction,
    ObsidianProposalStatus,
)
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.capabilities.obsidian.domain.vaults.entities import ObsidianVault
from compadre_assistant.capabilities.obsidian.presentation.responses import (
    format_article_analysis_result,
    format_article_processing_result,
    format_obsidian_message_result,
)
from compadre_assistant.core.presentation.responses import format_incoming_message_result


class TelegramResponsesTest(unittest.TestCase):
    """Проверяет пользовательский текст по результату обработки статьи."""

    def test_format_missing_vault_configuration_includes_ready_example(
        self,
    ) -> None:
        """Ошибка первой синхронизации объясняет ручное создание YAML."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_SYNC_FAILED,
                error=VaultConfigurationError(
                    VaultConfigurationErrorCode.MISSING
                ),
            )
        ))

        self.assertIn(".knowledge-catcher.yml", reply)
        self.assertIn("memory-bank/AGENTS.md.txt", reply)
        self.assertIn("Пример:\nversion: 1", reply)

    def test_format_missing_instruction_names_required_path(self) -> None:
        """Ошибка перечисленного файла показывает исправляемый path."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_SYNC_FAILED,
                error=VaultConfigurationError(
                    VaultConfigurationErrorCode.INSTRUCTION_MISSING,
                    path="memory-bank/docs/workflows.md.txt",
                ),
            )
        ))

        self.assertIn("memory-bank/docs/workflows.md.txt", reply)

    def test_format_sync_reports_optional_embedding_failure(self) -> None:
        """Частичный semantic index показывает точное покрытие и fallback."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_SYNC_COMPLETED,
                vault_sync_result=VaultSyncResult(
                    status=VaultSyncStatus.SYNCED,
                    total_notes=173,
                    embedding_update_failed=True,
                    embedding_coverage=EmbeddingIndexCoverage(
                        total_chunks=623,
                        embedded_chunks=576,
                    ),
                ),
            )
        ))

        self.assertIn("Локальная копия vault обновлена из GitHub", reply)
        self.assertIn("576 из 623", reply)
        self.assertIn("осталось chunks 47", reply)
        self.assertIn("FTS fallback", reply)

    def test_format_sync_reports_embedding_retry_counts(self) -> None:
        """Повторное построение отличает новые embeddings от переиспользованных."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_SYNC_COMPLETED,
                vault_sync_result=VaultSyncResult(
                    status=VaultSyncStatus.UNCHANGED,
                    total_notes=173,
                    embedded_chunks=47,
                    unchanged_embeddings=576,
                    embedding_coverage=EmbeddingIndexCoverage(
                        total_chunks=623,
                        embedded_chunks=623,
                    ),
                ),
            )
        ))

        self.assertIn("Семантический индекс готов: embeddings 623 из 623", reply)
        self.assertIn("добавлено 47, переиспользовано 576", reply)

    def test_format_github_status_reports_semantic_index_readiness(self) -> None:
        """Статус показывает готовый и частичный semantic index отдельно."""
        vault = ObsidianVault(
            app_user_id=1,
            installation_id=1,
            repository_id=1,
            owner="owner",
            repository="notes",
            branch="main",
            id=1,
        )

        ready = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_STATUS,
                vault_status=VaultStatus(
                    vault=vault,
                    embedding_index_current=True,
                    embedding_coverage=EmbeddingIndexCoverage(
                        total_chunks=623,
                        embedded_chunks=623,
                    ),
                ),
            )
        ))
        partial = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_STATUS,
                vault_status=VaultStatus(
                    vault=vault,
                    embedding_index_current=False,
                    embedding_coverage=EmbeddingIndexCoverage(
                        total_chunks=623,
                        embedded_chunks=576,
                    ),
                ),
            )
        ))

        self.assertIn("готов: embeddings 623 из 623", ready)
        self.assertIn("embeddings 576 из 623", partial)
        self.assertIn("осталось chunks 47", partial)

    def test_format_article_processing_result_reports_created_article(self) -> None:
        """Новая статья получает понятный текст с названием, статусом, ID и длиной."""
        reply = content_text(format_article_processing_result(
            ProcessArticleUrlResult(
                article=_article(),
                created=True,
                extracted=True,
            )
        ))

        self.assertIn("Готово: статья сохранена.", reply)
        self.assertIn("Название: Article title", reply)
        self.assertIn("Статус: текст извлечен", reply)
        self.assertIn("ID статьи: 1", reply)
        self.assertIn("Текст: 11 символов", reply)

    def test_format_article_processing_result_reports_existing_article(self) -> None:
        """Повторная ссылка получает отдельный текст без намека на новую запись."""
        reply = content_text(format_article_processing_result(
            ProcessArticleUrlResult(
                article=_article(),
                created=False,
                extracted=False,
            )
        ))

        self.assertIn("Эта статья уже была сохранена.", reply)

    def test_format_article_processing_result_reports_updated_article(self) -> None:
        """Повторно извлеченная статья получает текст обновления."""
        reply = content_text(format_article_processing_result(
            ProcessArticleUrlResult(
                article=_article(),
                created=False,
                extracted=True,
            )
        ))

        self.assertIn("Готово: статья обновлена.", reply)

    def test_format_article_processing_result_uses_fallbacks(self) -> None:
        """Ответ остается понятным, если у статьи нет заголовка или ID."""
        reply = content_text(format_article_processing_result(
            ProcessArticleUrlResult(
                article=replace(_article(), id=None, title=None, cleaned_text=None),
                created=True,
                extracted=False,
            )
        ))

        self.assertIn("Название: без заголовка", reply)
        self.assertIn("ID статьи: не сохранен", reply)
        self.assertIn("Текст: 0 символов", reply)

    def test_format_article_analysis_result_includes_markdown_analysis(self) -> None:
        """Ответ с анализом содержит служебный контекст и Markdown LLM."""
        processing_result = ProcessArticleUrlResult(
            article=_article(),
            created=True,
            extracted=True,
        )
        analysis_result = AnalyzeArticleResult(
            article=replace(_article(), status=ArticleStatus.ANALYZED),
            analysis=ArticleAnalysisResult(
                id=1,
                article_id=1,
                llm_model="fake-llm",
                prompt_version="article-summary-v1",
                result_text="## Кратко\nГотовый анализ.",
            ),
            created=True,
        )

        reply = content_text(format_article_analysis_result(processing_result, analysis_result))

        self.assertIn("Готово: статья сохранена.", reply)
        self.assertIn("Анализ готов.", reply)
        self.assertIn("## Кратко", reply)


    def test_format_incoming_message_result_reports_missing_url(self) -> None:
        """Общий результат без URL получает пользовательскую подсказку."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_URL_MISSING
            )
        ))

        self.assertIn("Пришли ссылку", reply)

    def test_format_incoming_message_result_reports_start_registered(self) -> None:
        """/start приветствует зарегистрированного пользователя по имени."""
        reply = content_text(format_incoming_message_result(
            ProcessIncomingMessageResult(
                type=IncomingMessageResultType.START_REGISTERED,
                app_user=AppUser(id=42, display_name="Влад"),
            )
        ))

        self.assertEqual(
            reply,
            "Compadre Assistant запущен.\n"
            "Рад тебя видеть, Влад.\n"
            "Справка: /help.",
        )

    def test_format_incoming_message_result_reports_start_unregistered(self) -> None:
        """/start для нового канала объясняет регистрацию и привязку."""
        reply = content_text(format_incoming_message_result(
            ProcessIncomingMessageResult(
                type=IncomingMessageResultType.START_UNREGISTERED,
            )
        ))

        self.assertIn("пока не зарегистрирован", reply)
        self.assertIn("/register", reply)

    def test_format_incoming_message_result_reports_already_registered(self) -> None:
        """Повторный /register получает честный ответ."""
        reply = content_text(format_incoming_message_result(
            ProcessIncomingMessageResult(
                type=IncomingMessageResultType.ALREADY_REGISTERED,
                app_user=AppUser(id=42, display_name="Влад"),
            )
        ))

        self.assertIn("уже зарегистрирован", reply)
        self.assertIn("Влад", reply)
        self.assertNotIn("vault", reply)
        self.assertNotIn("42", reply)

    def test_name_completion_reports_independent_profile(self) -> None:
        """Сохранение имени завершает профиль без предметного ответа о vault."""
        reply = content_text(format_incoming_message_result(
            ProcessIncomingMessageResult(
                type=IncomingMessageResultType.REGISTRATION_NAME_SAVED,
                app_user=AppUser(id=42, display_name="Влад"),
            )
        ))

        self.assertIn("Регистрация завершена", reply)
        self.assertNotIn("vault", reply)

    def test_format_incoming_message_result_reports_link_code(self) -> None:
        """Код привязки форматируется как команда для второго канала."""
        reply = content_text(format_incoming_message_result(
            ProcessIncomingMessageResult(
                type=IncomingMessageResultType.LINK_CODE_CREATED,
                link_code=CreatedLinkCode(
                    code="ABC123",
                    expires_at=datetime(2026, 7, 17, tzinfo=UTC),
                ),
            )
        ))

        self.assertIn("ABC123", reply)
        self.assertIn("/link ABC123", reply)

    def test_format_incoming_message_result_reports_rebind_success(self) -> None:
        """Подтвержденная перепривязка сообщает нового пользователя."""
        reply = content_text(format_incoming_message_result(
            ProcessIncomingMessageResult(
                type=IncomingMessageResultType.LINK_REBOUND,
                app_user=AppUser(id=99, display_name="Основной профиль"),
            )
        ))

        self.assertIn("перепривязал", reply)
        self.assertIn("Основной профиль", reply)
        self.assertNotIn("99", reply)

    def test_format_incoming_message_result_reports_processing_error(self) -> None:
        """Ошибка article pipeline получает понятный текст ответа."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_PROCESSING_FAILED,
                error=ProcessArticleUrlError(
                    "Could not fetch article HTML: failed",
                    stage=ProcessingStage.FETCHING,
                ),
            )
        ))

        self.assertIn("Не удалось загрузить страницу", reply)

    def test_applied_commit_reports_post_commit_index_warning(self) -> None:
        """Сбой embeddings не выдаётся за ошибку уже успешного GitHub commit."""
        proposal = ObsidianProposal(
            origin=PendingOrigin("telegram", "chat"),
            id=1,
            app_user_id=1,
            article_id=1,
            analysis_id=1,
            vault_id=1,
            action=ObsidianProposalAction.ADD,
            reasoning="Создать заметку.",
            base_commit_sha="base",
            base_tree_sha="tree",
            target_path="note.md",
            proposed_markdown="# Note",
            status=ObsidianProposalStatus.APPLIED,
            applied_commit_sha="commit",
            completed_at=datetime.now(UTC),
        )
        error = RuntimeError("embedding unavailable")

        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_REVIEW_APPLIED,
                obsidian_confirmation=ObsidianConfirmationResult(
                    status=ObsidianConfirmationStatus.APPLIED,
                    proposal=proposal,
                    error=error,
                ),
                error=error,
            )
        ))

        self.assertIn("Изменение записано в GitHub прямым commit", reply)
        self.assertIn("поисковый индекс ещё не обновлён", reply)
        self.assertIn("/vault_sync", reply)

    def test_format_profile_status_preserves_general_text(self) -> None:
        """Общий статус профиля не требует модели vault."""
        reply = content_text(format_incoming_message_result(ProcessIncomingMessageResult(
            type=IncomingMessageResultType.PROFILE_STATUS,
            capability_reply="Профиль: Влад.\nРежим этого чата: не выбран.",
        )))
        self.assertIn("Влад", reply)
        self.assertIn("не выбран", reply)

    def test_format_github_authorization_returns_device_url_and_code(self) -> None:
        """Оба channel adapters получают безопасную инструкцию Device Flow."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_CONNECT_STARTED,
                app_user=AppUser(id=42),
                github_connection=GitHubConnectionStartResult(
                    status=GitHubConnectionStartStatus.STARTED,
                    installation_url=(
                        "https://github.com/apps/compadre-assistant/installations/new"
                    ),
                    authorization=GitHubDeviceAuthorization(
                        device_code="device-secret",
                        user_code="ABCD-EFGH",
                        verification_uri="https://github.com/login/device",
                        expires_in=900,
                        interval=5,
                    ),
                ),
            )
        ))

        self.assertNotIn("installations/new", reply)
        self.assertIn("https://github.com/login/device", reply)
        self.assertIn("ABCD-EFGH", reply)
        self.assertNotIn("device-secret", reply)

    def test_format_github_failure_covers_final_error_statuses(self) -> None:
        """Ошибки Device Flow предлагают повторить отправку repository URL."""
        cases = {
            GitHubConnectionCompletionStatus.DENIED: "отклонена",
            GitHubConnectionCompletionStatus.EXPIRED: "истёк",
            GitHubConnectionCompletionStatus.FAILED: "Не удалось",
        }

        for status, expected_text in cases.items():
            with self.subTest(status=status):
                reply = content_text(format_obsidian_message_result(
                    ObsidianMessageResult(
                        type=ObsidianResultType.GITHUB_CONNECT_FAILED,
                        github_completion=GitHubConnectionCompletion(status),
                    )
                ))
                self.assertIn(expected_text, reply)
                self.assertIn("ссылку", reply)

    def test_format_github_app_required_returns_installation_url(self) -> None:
        """После OAuth без installation бот предлагает настроить App."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_APP_REQUIRED,
                installation_url=(
                    "https://github.com/apps/compadre-assistant/installations/new"
                ),
            )
        ))

        self.assertIn("installations/new", reply)
        self.assertIn("read and write", reply)

    def test_format_github_authorized_announces_first_sync(self) -> None:
        """После Device Flow пользователь сразу видит начало первой синхронизации."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_AUTHORIZED_SYNC_STARTED,
            )
        ))

        self.assertIn("GitHub авторизован", reply)
        self.assertIn("первую синхронизацию", reply)

    def test_format_vault_selection_shows_repository_branch_and_path(self) -> None:
        """Успешный выбор vault возвращает понятные проверенные параметры."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_SELECTED,
                app_user=AppUser(id=42),
                vault_selection=VaultSelectionResult(
                    VaultSelectionStatus.SELECTED,
                    ObsidianVault(
                        id=1,
                        app_user_id=42,
                        installation_id=101,
                        repository_id=501,
                        owner="octocat",
                        repository="notes",
                        branch="main",
                        root_path="Vault",
                    ),
                ),
            )
        ))

        self.assertIn("Хранилище подключено:", reply)
        self.assertIn("octocat/notes", reply)
        self.assertIn("main", reply)
        self.assertIn("Vault", reply)

    def test_format_unavailable_vault_explains_required_write_access(self) -> None:
        """Ошибка выбора объясняет требование чтения и записи repository."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.GITHUB_VAULT_REPOSITORY_UNAVAILABLE,
            )
        ))

        self.assertIn("читать и записывать", reply)
        self.assertIn("Contents: read and write", reply)

    def test_format_vault_replacement_requests_yes_or_no(self) -> None:
        """Предложение замены явно предупреждает об удалении локальных данных."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=(
                    ObsidianResultType.GITHUB_VAULT_REPLACEMENT_CONFIRMATION_REQUIRED
                ),
                vault_selection=VaultSelectionResult(
                    VaultSelectionStatus.REPLACEMENT_CONFIRMATION_REQUIRED,
                    ObsidianVault(
                        app_user_id=42,
                        installation_id=101,
                        repository_id=502,
                        owner="octocat",
                        repository="second",
                        branch="main",
                    ),
                ),
            )
        ))

        self.assertIn("текущее подключение и его локальные данные будут заменены", reply)
        self.assertIn("да", reply)
        self.assertIn("нет", reply)

    def test_format_vault_disconnect_explains_scope_and_confirmation(self) -> None:
        """Отключение предупреждает об очистке локальных, но не пользовательских данных."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=(
                    ObsidianResultType.GITHUB_DISCONNECT_CONFIRMATION_REQUIRED
                ),
                vault_disconnect=VaultDisconnectResult(
                    VaultDisconnectStatus.CONFIRMATION_REQUIRED,
                    ObsidianVault(
                        id=1,
                        app_user_id=42,
                        installation_id=101,
                        repository_id=501,
                        owner="octocat",
                        repository="notes",
                        branch="main",
                    ),
                ),
            )
        ))

        self.assertIn("octocat/notes", reply)
        self.assertIn("Repository на GitHub не изменится", reply)
        self.assertIn("статьи и анализы сохранятся", reply)
        self.assertIn("да", reply)
        self.assertIn("нет", reply)

    def test_format_sync_failure_keeps_article_result_and_warns_about_fallback(
        self,
    ) -> None:
        """Сбой GitHub сохраняет основной ответ и поясняет локальный fallback."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_PROCESSED,
                article_result=ProcessArticleUrlResult(
                    article=_article(),
                    created=True,
                    extracted=True,
                ),
                vault_sync_warning=VaultSyncWarning(
                    reason=VaultSyncWarningReason.UPDATE_FAILED,
                    note_count=17,
                    last_checked_at=datetime(
                        2026,
                        7,
                        30,
                        7,
                        15,
                        tzinfo=UTC,
                    ),
                ),
            )
        ))

        self.assertIn("статья сохранена.", reply)
        self.assertIn("последняя локальная копия", reply)
        self.assertIn("Локально заметок: 17.", reply)
        self.assertIn("2026-07-30 07:15 UTC", reply)

    def test_format_sync_in_progress_warns_about_current_local_copy(self) -> None:
        """Параллельная синхронизация не скрывает успешный результат статьи."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_PROCESSED,
                article_result=ProcessArticleUrlResult(
                    article=_article(),
                    created=True,
                    extracted=True,
                ),
                vault_sync_warning=VaultSyncWarning(
                    reason=VaultSyncWarningReason.IN_PROGRESS,
                    note_count=17,
                ),
            )
        ))

        self.assertIn("статья сохранена.", reply)
        self.assertIn("другом связанном канале", reply)
        self.assertIn("текущая локальная копия", reply)

    def test_format_incoming_message_result_reports_reanalysis(self) -> None:
        """Повторный анализ форматируется как полезный Markdown-ответ."""
        reply = content_text(format_obsidian_message_result(
            ObsidianMessageResult(
                type=ObsidianResultType.ARTICLE_REANALYZED,
                analysis_result=AnalyzeArticleResult(
                    article=replace(_article(), status=ArticleStatus.ANALYZED),
                    analysis=ArticleAnalysisResult(
                        id=2,
                        article_id=1,
                        llm_model="fake-llm",
                        prompt_version="article-summary-v1",
                        result_text="## Кратко\nОбновлено.",
                    ),
                    created=True,
                ),
            )
        ))

        self.assertIn("Анализ обновлён.", reply)
        self.assertIn("## Кратко", reply)


def _article() -> Article:
    """Создает минимальную статью для тестов пользовательских ответов."""
    return Article(
        id=1,
        source_url="https://example.com/article",
        normalized_url="https://example.com/article",
        title="Article title",
        cleaned_text="Clean text.",
        status=ArticleStatus.EXTRACTED,
    )


if __name__ == "__main__":
    unittest.main()
