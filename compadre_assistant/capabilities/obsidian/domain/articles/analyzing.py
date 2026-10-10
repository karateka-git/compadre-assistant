from __future__ import annotations

from dataclasses import dataclass, replace

from compadre_assistant.capabilities.obsidian.domain.articles.errors import ArticleAnalysisError
from compadre_assistant.capabilities.obsidian.domain.articles.ports import (
    ArticleAnalysisResultRepository,
    ArticleAnalyzer,
    ArticleRepository,
    ProcessingErrorRecorder,
)
from compadre_assistant.capabilities.obsidian.domain.articles.stages import ProcessingStage
from compadre_assistant.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from compadre_assistant.capabilities.obsidian.domain.articles.entities import Article
from compadre_assistant.capabilities.obsidian.domain.articles.statuses import ArticleStatus


@dataclass(frozen=True, slots=True)
class AnalyzeArticleCommand:
    """Команда LLM-анализа уже сохранённой статьи."""

    article_id: int
    app_user_id: int = 1
    incoming_message_id: int | None = None
    force: bool = False


@dataclass(frozen=True, slots=True)
class AnalyzeArticleResult:
    """Результат LLM-анализа статьи."""

    article: Article
    analysis: ArticleAnalysisResult
    created: bool


class AnalyzeArticleError(RuntimeError):
    """Ошибка полного pipeline LLM-анализа статьи."""


class AnalyzeArticleUseCase:
    """Анализирует очищенный текст статьи и сохраняет LLM-результат.

    Args:
        article_repository: Port хранения статей.
        analyzer: Port LLM-анализа очищенного текста статьи.
        analysis_result_repository: Port хранения результатов анализа.
        error_recorder: Optional port записи диагностических ошибок.
    """

    def __init__(
        self,
        *,
        article_repository: ArticleRepository,
        analyzer: ArticleAnalyzer,
        analysis_result_repository: ArticleAnalysisResultRepository,
        error_recorder: ProcessingErrorRecorder | None = None,
    ) -> None:
        self._article_repository = article_repository
        self._analyzer = analyzer
        self._analysis_result_repository = analysis_result_repository
        self._error_recorder = error_recorder

    def execute(self, command: AnalyzeArticleCommand) -> AnalyzeArticleResult:
        """Выполняет LLM-анализ статьи.

        Args:
            command: Команда с ID сохранённой статьи.

        Returns:
            Результат анализа и актуальная статья.

        Raises:
            AnalyzeArticleError: Если статья не найдена, не готова к анализу,
                LLM-анализ или сохранение результата завершились ошибкой.
        """
        article = self._get_article(command.article_id, command.incoming_message_id)
        if article.app_user_id != command.app_user_id:
            error = AnalyzeArticleError(f"Article not found: {command.article_id}")
            self._record_error(
                article_id=None,
                app_user_id=command.app_user_id,
                incoming_message_id=command.incoming_message_id,
                stage=ProcessingStage.STORAGE,
                error=error,
            )
            raise error
        existing = self._analysis_result_repository.get_latest_for_article(article.id)
        if existing is not None and not command.force:
            return AnalyzeArticleResult(
                article=article,
                analysis=existing,
                created=False,
            )

        if not article.cleaned_text:
            error = AnalyzeArticleError(
                f"Article does not contain cleaned text: {article.id}"
            )
            self._record_error(
                article_id=article.id,
                app_user_id=article.app_user_id,
                incoming_message_id=command.incoming_message_id,
                stage=ProcessingStage.ANALYSIS,
                error=error,
            )
            raise error

        self._article_repository.update_status(article.id, ArticleStatus.ANALYZING)

        try:
            analysis = self._analyzer.analyze(article)
        except (ArticleAnalysisError, ValueError) as error:
            self._mark_failed_and_record(
                article.id,
                command.incoming_message_id,
                article.app_user_id,
                error,
            )
            raise AnalyzeArticleError(f"Could not analyze article: {error}") from error

        if analysis.article_id != article.id:
            error = AnalyzeArticleError(
                "Analyzer returned result for another article: "
                f"{analysis.article_id} instead of {article.id}"
            )
            self._mark_failed_and_record(
                article.id,
                command.incoming_message_id,
                article.app_user_id,
                error,
            )
            raise error
        analysis = replace(analysis, app_user_id=article.app_user_id)

        try:
            saved_analysis = self._analysis_result_repository.save(analysis)
            updated_article = self._article_repository.update_status(
                article.id,
                ArticleStatus.ANALYZED,
            )
        except Exception as error:
            self._record_error(
                article_id=article.id,
                app_user_id=article.app_user_id,
                incoming_message_id=command.incoming_message_id,
                stage=ProcessingStage.STORAGE,
                error=error,
            )
            raise AnalyzeArticleError(
                f"Could not save article analysis: {error}"
            ) from error

        if updated_article is None:
            error = AnalyzeArticleError(
                f"Article disappeared before analysis status update: {article.id}"
            )
            self._record_error(
                article_id=article.id,
                app_user_id=article.app_user_id,
                incoming_message_id=command.incoming_message_id,
                stage=ProcessingStage.STORAGE,
                error=error,
            )
            raise error

        return AnalyzeArticleResult(
            article=updated_article,
            analysis=saved_analysis,
            created=True,
        )

    def _get_article(
        self,
        article_id: int,
        incoming_message_id: int | None,
    ) -> Article:
        """Возвращает статью или записывает диагностическую ошибку."""
        article = self._article_repository.get_by_id(article_id)
        if article is not None and article.id is not None:
            return article

        error = AnalyzeArticleError(f"Article not found: {article_id}")
        self._record_error(
            article_id=None,
            app_user_id=None,
            incoming_message_id=incoming_message_id,
            stage=ProcessingStage.STORAGE,
            error=error,
        )
        raise error

    def _mark_failed_and_record(
        self,
        article_id: int,
        incoming_message_id: int | None,
        app_user_id: int,
        error: Exception,
    ) -> None:
        """Переводит статью в `failed` и сохраняет ошибку анализа."""
        self._article_repository.update_status(article_id, ArticleStatus.FAILED)
        self._record_error(
            article_id=article_id,
            app_user_id=app_user_id,
            incoming_message_id=incoming_message_id,
            stage=ProcessingStage.ANALYSIS,
            error=error,
        )

    def _record_error(
        self,
        *,
        article_id: int | None,
        app_user_id: int | None,
        incoming_message_id: int | None = None,
        stage: ProcessingStage,
        error: Exception,
    ) -> None:
        """Записывает ошибку, если для use case передан recorder."""
        if self._error_recorder is None:
            return

        self._error_recorder.record(
            article_id=article_id,
            app_user_id=app_user_id,
            incoming_message_id=incoming_message_id,
            stage=stage,
            error_type=type(error).__name__,
            error_message=str(error),
        )
