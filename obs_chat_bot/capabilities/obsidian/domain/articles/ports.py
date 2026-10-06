from __future__ import annotations

from typing import Protocol

from obs_chat_bot.capabilities.obsidian.domain.articles.extracted import ExtractedArticle
from obs_chat_bot.capabilities.obsidian.domain.articles.html import ArticleHtml
from obs_chat_bot.capabilities.obsidian.domain.articles.incoming_messages import (
    ArticleIncomingMessage,
    SavedIncomingMessage,
)
from obs_chat_bot.capabilities.obsidian.domain.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.domain.articles.analysis import ArticleAnalysisResult
from obs_chat_bot.capabilities.obsidian.domain.articles.entities import Article
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus


class ArticleRepository(Protocol):
    """Описывает операции хранения статей, нужные application-слою."""

    def create(self, article: Article) -> Article:
        """Сохраняет новую статью.

        Args:
            article: Доменная модель статьи без ID.

        Returns:
            Сохранённая доменная модель статьи.
        """

    def get_by_id(self, article_id: int) -> Article | None:
        """Возвращает статью по ID или `None`, если записи нет."""

    def find_by_normalized_url(
        self,
        normalized_url: str,
        app_user_id: int = 1,
    ) -> Article | None:
        """Ищет статью по нормализованному URL."""

    def find_by_text_hash(self, text_hash: str, app_user_id: int = 1) -> list[Article]:
        """Возвращает статьи с одинаковым хешем очищенного текста."""

    def update_status(
        self,
        article_id: int,
        status: ArticleStatus,
    ) -> Article | None:
        """Обновляет статус статьи."""

    def update_content(
        self,
        article_id: int,
        *,
        title: str | None,
        cleaned_text: str,
        text_hash: str,
        status: ArticleStatus = ArticleStatus.EXTRACTED,
    ) -> Article | None:
        """Сохраняет извлечённый контент статьи."""


class ArticleHtmlFetcher(Protocol):
    """Описывает загрузчик HTML страницы статьи."""

    def fetch(self, url: str) -> ArticleHtml:
        """Загружает HTML по URL.

        Args:
            url: Поддерживаемый URL статьи.

        Returns:
            HTML страницы вместе с исходным и финальным URL.

        Raises:
            ArticleFetchError: Если страницу не удалось загрузить.
        """


class ArticleTextExtractor(Protocol):
    """Описывает извлечение чистого текста статьи из HTML."""

    def extract(self, html: ArticleHtml) -> ExtractedArticle:
        """Извлекает содержательный текст статьи.

        Args:
            html: HTML страницы статьи.

        Returns:
            Очищенный текст статьи и найденные метаданные.

        Raises:
            ArticleExtractionError: Если текст статьи извлечь не удалось.
        """


class ArticleAnalyzer(Protocol):
    """Описывает LLM-анализ очищенного текста статьи."""

    def analyze(self, article: Article) -> ArticleAnalysisResult:
        """Анализирует статью и возвращает готовый результат.

        Args:
            article: Сохранённая статья с ID и очищенным текстом.

        Returns:
            Результат анализа в доменном формате.

        Raises:
            ArticleAnalysisError: Если LLM-анализ выполнить не удалось.
            ValueError: Если статья не готова к анализу.
        """


class ArticleAnalysisResultRepository(Protocol):
    """Описывает хранение результатов LLM-анализа статей."""

    def save(self, result: ArticleAnalysisResult) -> ArticleAnalysisResult:
        """Сохраняет результат анализа статьи.

        Args:
            result: Доменная модель результата анализа без ID.

        Returns:
            Сохранённый результат анализа с ID и временем создания.
        """

    def get_by_id(self, result_id: int) -> ArticleAnalysisResult | None:
        """Возвращает результат анализа по ID или `None`, если записи нет."""

    def get_latest_for_article(self, article_id: int) -> ArticleAnalysisResult | None:
        """Возвращает последний результат анализа статьи или `None`.

        Args:
            article_id: ID статьи, для которой нужен результат анализа.
        """


class ProcessingErrorRecorder(Protocol):
    """Описывает запись диагностических ошибок обработки статьи."""

    def record(
        self,
        *,
        article_id: int | None,
        app_user_id: int | None = None,
        incoming_message_id: int | None = None,
        stage: ProcessingStage,
        error_type: str,
        error_message: str,
    ) -> None:
        """Сохраняет информацию об ошибке pipeline.

        Args:
            article_id: ID статьи, если она уже была создана.
            app_user_id: ID пользователя приложения, если он уже известен.
            incoming_message_id: ID входящего сообщения, если ошибка связана
                с конкретным сообщением пользователя.
            stage: Этап обработки, на котором произошла ошибка.
            error_type: Имя класса ошибки.
            error_message: Текст ошибки.
        """


class IncomingMessageRepository(Protocol):
    """Описывает журнал сообщений, запустивших article pipeline."""

    def save(self, message: ArticleIncomingMessage) -> SavedIncomingMessage:
        """Сохраняет входящее сообщение или возвращает уже существующую запись.

        Args:
            message: Сообщение article pipeline с разрешённым пользователем.

        Returns:
            Сохранённое сообщение с ID записи.
        """

    def link_to_article(
        self,
        *,
        incoming_message_id: int,
        article_id: int,
    ) -> SavedIncomingMessage | None:
        """Привязывает сохранённое входящее сообщение к статье.

        Args:
            incoming_message_id: ID сохранённого входящего сообщения.
            article_id: ID статьи, созданной или найденной после обработки URL.

        Returns:
            Обновлённое сообщение или `None`, если запись не найдена.
        """
