from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from obs_chat_bot.capabilities.obsidian.domain.articles.errors import (
    ArticleExtractionError,
    ArticleFetchError,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.ports import (
    ArticleHtmlFetcher,
    ArticleRepository,
    ArticleTextExtractor,
    ProcessingErrorRecorder,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.domain.articles.url_utils import normalize_article_url
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus


@dataclass(frozen=True, slots=True)
class ProcessArticleUrlCommand:
    """Команда обработки одной ссылки на статью."""

    source_url: str
    app_user_id: int = 1
    incoming_message_id: int | None = None
    force_reprocess: bool = False
    preserve_existing_on_failure: bool = False


@dataclass(frozen=True, slots=True)
class ProcessArticleUrlResult:
    """Результат обработки ссылки на статью."""

    article: Article
    created: bool
    extracted: bool


class ProcessArticleUrlError(RuntimeError):
    """Ошибка полного pipeline обработки URL статьи."""

    def __init__(
        self,
        message: str,
        *,
        stage: ProcessingStage | None = None,
    ) -> None:
        """Создаёт ошибку article pipeline с типизированным этапом.

        Args:
            message: Техническое описание ошибки.
            stage: Этап pipeline, на котором произошла ошибка.
        """
        self.stage = stage
        super().__init__(message)


class ProcessArticleUrlUseCase:
    """Обрабатывает URL статьи до сохранения очищенного текста.

    Use case координирует шаги приложения, но не знает о конкретных HTTP,
    extraction или SQLite-реализациях.

    Args:
        article_repository: Port хранения статей.
        html_fetcher: Port загрузки HTML.
        text_extractor: Port извлечения текста.
        error_recorder: Optional port записи диагностических ошибок.
    """

    def __init__(
        self,
        *,
        article_repository: ArticleRepository,
        html_fetcher: ArticleHtmlFetcher,
        text_extractor: ArticleTextExtractor,
        error_recorder: ProcessingErrorRecorder | None = None,
    ) -> None:
        self._article_repository = article_repository
        self._html_fetcher = html_fetcher
        self._text_extractor = text_extractor
        self._error_recorder = error_recorder

    def execute(self, command: ProcessArticleUrlCommand) -> ProcessArticleUrlResult:
        """Выполняет pipeline обработки URL.

        Args:
            command: Команда с исходным URL.

        Returns:
            Результат с сохранённой статьёй.

        Raises:
            ProcessArticleUrlError: Если URL невалиден, загрузка/извлечение
                или сохранение результата завершились ошибкой.
        """
        try:
            normalized_url = normalize_article_url(command.source_url)
        except ValueError as error:
            self._record_error(
                article_id=None,
                app_user_id=command.app_user_id,
                incoming_message_id=command.incoming_message_id,
                stage=ProcessingStage.NORMALIZATION,
                error=error,
            )
            raise ProcessArticleUrlError(
                f"Could not normalize article URL: {error}",
                stage=ProcessingStage.NORMALIZATION,
            ) from error

        existing = self._article_repository.find_by_normalized_url(
            normalized_url,
            command.app_user_id,
        )
        if (
            existing is not None
            and not command.force_reprocess
            and (
                existing.cleaned_text
                or existing.status is ArticleStatus.REVIEWED
            )
        ):
            return ProcessArticleUrlResult(
                article=existing,
                created=False,
                extracted=False,
            )

        article = existing or self._create_article(
            command.source_url,
            normalized_url,
            command.app_user_id,
            command.incoming_message_id,
        )
        created = existing is None
        article_id = _require_article_id(article)
        original_status = article.status

        try:
            self._article_repository.update_status(article_id, ArticleStatus.FETCHING)
            html = self._html_fetcher.fetch(article.source_url)
        except ArticleFetchError as error:
            self._mark_failed_and_record(
                article_id,
                command.incoming_message_id,
                command.app_user_id,
                ProcessingStage.FETCHING,
                error,
                preserved_status=(
                    original_status
                    if command.preserve_existing_on_failure
                    else None
                ),
            )
            raise ProcessArticleUrlError(
                f"Could not fetch article HTML: {error}",
                stage=ProcessingStage.FETCHING,
            ) from error

        try:
            extracted = self._text_extractor.extract(html)
        except ArticleExtractionError as error:
            self._mark_failed_and_record(
                article_id,
                command.incoming_message_id,
                command.app_user_id,
                ProcessingStage.EXTRACTION,
                error,
                preserved_status=(
                    original_status
                    if command.preserve_existing_on_failure
                    else None
                ),
            )
            raise ProcessArticleUrlError(
                f"Could not extract article text: {error}",
                stage=ProcessingStage.EXTRACTION,
            ) from error

        text_hash = _build_text_hash(extracted.cleaned_text)
        try:
            updated = self._article_repository.update_content(
                article_id,
                title=extracted.title,
                cleaned_text=extracted.cleaned_text,
                text_hash=text_hash,
                status=ArticleStatus.EXTRACTED,
            )
        except Exception as error:
            self._mark_failed_and_record(
                article_id,
                command.incoming_message_id,
                command.app_user_id,
                ProcessingStage.STORAGE,
                error,
                preserved_status=(
                    original_status
                    if command.preserve_existing_on_failure
                    else None
                ),
            )
            raise ProcessArticleUrlError(
                f"Could not store article content: {error}",
                stage=ProcessingStage.STORAGE,
            ) from error
        if updated is None:
            error = ProcessArticleUrlError(
                f"Article disappeared before content update: {article_id}",
                stage=ProcessingStage.STORAGE,
            )
            self._mark_failed_and_record(
                article_id,
                command.incoming_message_id,
                command.app_user_id,
                ProcessingStage.STORAGE,
                error,
                preserved_status=(
                    original_status
                    if command.preserve_existing_on_failure
                    else None
                ),
            )
            raise error

        return ProcessArticleUrlResult(
            article=updated,
            created=created,
            extracted=True,
        )

    def reprocess_existing(
        self,
        *,
        article_id: int,
        app_user_id: int,
        incoming_message_id: int | None = None,
    ) -> ProcessArticleUrlResult:
        """Повторно загружает сохранённый URL, сохраняя прежний result при сбое.

        Args:
            article_id: ID существующей статьи.
            app_user_id: Обязательный пользовательский scope.
            incoming_message_id: Сообщение команды для диагностики.

        Returns:
            Результат новой загрузки и extraction.

        Raises:
            ProcessArticleUrlError: Статья отсутствует, принадлежит другому
                пользователю или повторная загрузка не завершилась.
        """
        article = self._article_repository.get_by_id(article_id)
        if article is None or article.app_user_id != app_user_id:
            raise ProcessArticleUrlError(
                f"Article not found: {article_id}",
                stage=ProcessingStage.STORAGE,
            )
        return self.execute(
            ProcessArticleUrlCommand(
                source_url=article.source_url,
                app_user_id=app_user_id,
                incoming_message_id=incoming_message_id,
                force_reprocess=True,
                preserve_existing_on_failure=True,
            )
        )

    def _create_article(
        self,
        source_url: str,
        normalized_url: str,
        app_user_id: int,
        incoming_message_id: int | None,
    ) -> Article:
        """Создаёт новую статью в статусе `new`."""
        try:
            return self._article_repository.create(
                Article(
                    source_url=source_url,
                    normalized_url=normalized_url,
                    app_user_id=app_user_id,
                )
            )
        except Exception as error:
            self._record_error(
                article_id=None,
                app_user_id=app_user_id,
                incoming_message_id=incoming_message_id,
                stage=ProcessingStage.STORAGE,
                error=error,
            )
            raise ProcessArticleUrlError(
                f"Could not create article: {error}",
                stage=ProcessingStage.STORAGE,
            ) from error

    def _mark_failed_and_record(
        self,
        article_id: int,
        incoming_message_id: int | None,
        app_user_id: int,
        stage: ProcessingStage,
        error: Exception,
        preserved_status: ArticleStatus | None = None,
    ) -> None:
        """Сохраняет ошибку, восстанавливая прежний статус при safe reprocess."""
        self._article_repository.update_status(
            article_id,
            preserved_status or ArticleStatus.FAILED,
        )
        self._record_error(
            article_id=article_id,
            app_user_id=app_user_id,
            incoming_message_id=incoming_message_id,
            stage=stage,
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


def _require_article_id(article: Article) -> int:
    """Возвращает ID сохранённой статьи или выбрасывает ошибку."""
    if article.id is None:
        raise ProcessArticleUrlError(
            "Saved article must contain id",
            stage=ProcessingStage.STORAGE,
        )
    return article.id


def _build_text_hash(cleaned_text: str) -> str:
    """Строит SHA-256 хеш очищенного текста статьи."""
    return sha256(cleaned_text.encode("utf-8")).hexdigest()
