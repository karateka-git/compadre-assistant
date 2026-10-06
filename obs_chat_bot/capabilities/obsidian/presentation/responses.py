from __future__ import annotations

from datetime import UTC

from obs_chat_bot.capabilities.obsidian.presentation.vault_configuration import load_vault_configuration_example
from obs_chat_bot.capabilities.obsidian.domain.articles.analyzing import AnalyzeArticleResult
from obs_chat_bot.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlResult
from obs_chat_bot.capabilities.obsidian.domain.articles.stages import ProcessingStage
from obs_chat_bot.capabilities.obsidian.presentation.processing import ObsidianMessageResult
from obs_chat_bot.capabilities.obsidian.presentation.results import ObsidianResultType
from obs_chat_bot.capabilities.obsidian.domain.search.models import EmbeddingIndexCoverage
from obs_chat_bot.capabilities.obsidian.domain.vaults.github_models import GitHubConnectionCompletionStatus
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_configuration import (
    VAULT_CONFIGURATION_PATH,
    VaultConfigurationError,
    VaultConfigurationErrorCode,
)
from obs_chat_bot.capabilities.obsidian.domain.vaults.vault_sync import VaultSyncStatus, VaultSyncWarningReason
from obs_chat_bot.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from obs_chat_bot.capabilities.obsidian.domain.reviews.entities import ObsidianProposalAction


_ARTICLE_STATUS_LABELS: dict[ArticleStatus, str] = {
    ArticleStatus.NEW: "новая",
    ArticleStatus.FETCHING: "загружается",
    ArticleStatus.EXTRACTED: "текст извлечен",
    ArticleStatus.ANALYZING: "анализируется",
    ArticleStatus.ANALYZED: "проанализирована",
    ArticleStatus.NEEDS_OBSIDIAN_REVIEW: "ждет разбора для Obsidian",
    ArticleStatus.REVIEWED: "разобрана",
    ArticleStatus.FAILED: "ошибка обработки",
}


def format_article_processing_result(result: ProcessArticleUrlResult) -> str:
    """Формирует человекочитаемый ответ по результату article pipeline.

    Args:
        result: Результат обработки URL статьи.

    Returns:
        Короткий текст, который можно отправить пользователю во внешний канал.
    """
    article = result.article
    title = article.title or "без заголовка"
    text_length = len(article.cleaned_text or "")
    status = _ARTICLE_STATUS_LABELS.get(article.status, article.status.value)
    action = _article_action_text(result)
    article_id = article.id if article.id is not None else "не сохранен"

    return (
        f"{action}\n\n"
        f"Название: {title}\n"
        f"Статус: {status}\n"
        f"ID статьи: {article_id}\n"
        f"Текст: {text_length} символов"
    )


def format_article_analysis_result(
    processing_result: ProcessArticleUrlResult,
    analysis_result: AnalyzeArticleResult,
) -> str:
    """Формирует ответ с полезной LLM-сводкой статьи.

    Args:
        processing_result: Результат обработки URL статьи.
        analysis_result: Результат LLM-анализа статьи.

    Returns:
        Markdown-текст анализа с коротким техническим контекстом.
    """
    article = analysis_result.article
    title = article.title or "без заголовка"
    article_id = article.id if article.id is not None else "не сохранен"
    analysis_action = (
        "Анализ готов." if analysis_result.created else "Использую сохраненный анализ."
    )

    return (
        f"{_article_action_text(processing_result)}\n"
        f"{analysis_action}\n\n"
        f"Название: {title}\n"
        f"ID статьи: {article_id}\n\n"
        f"{analysis_result.analysis.result_text}"
    )


def format_obsidian_message_result(result: ObsidianMessageResult) -> str:
    """Формирует предметный ответ Obsidian для любого канала."""
    match result.type:
        case ObsidianResultType.CONFIRMATION_MISSING:
            return (
                "Сейчас нет действия, которое нужно подтвердить.\n"
                "Сначала пришли ссылку на другой repository или `/link <код>`."
            )
        case ObsidianResultType.GITHUB_CONNECT_STARTED:
            return _format_github_connect_response(result, repeated=False)
        case ObsidianResultType.GITHUB_CONNECT_ALREADY_PENDING:
            return _format_github_connect_response(result, repeated=True)
        case ObsidianResultType.GITHUB_CONNECT_PREPARING:
            return "Авторизация GitHub уже запускается. Подожди несколько секунд."
        case ObsidianResultType.GITHUB_CONNECT_IN_PROGRESS:
            return (
                "Подключение GitHub уже выполняется в другом привязанном канале. "
                "Заверши его там или дождись истечения одноразового кода."
            )
        case ObsidianResultType.GITHUB_AUTHORIZED_SYNC_STARTED:
            return (
                "GitHub авторизован. Начинаю проверку репозитория "
                "и первую синхронизацию."
            )
        case ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE:
            return "GitHub connector пока не настроен на сервере."
        case ObsidianResultType.GITHUB_CONNECT_FAILED:
            return _format_github_failure(result)
        case ObsidianResultType.GITHUB_APP_REQUIRED:
            suffix = (
                f"\n\nНастроить GitHub App:\n{result.installation_url}"
                if result.installation_url
                else ""
            )
            return (
                "GitHub-аккаунт авторизован, но GitHub App пока не имеет доступа "
                "к нужному repository."
                f"{suffix}\n\n"
                "Выбери repository и разреши `Contents: read and write`, "
                "затем снова пришли его ссылку."
            )
        case ObsidianResultType.REGISTRATION_VAULT_REQUIRED:
            return (
                f"{_display_name(result)}, осталось подключить Obsidian vault.\n"
                "Пришли ссылку на GitHub-репозиторий "
                "с Obsidian vault."
            )
        case ObsidianResultType.GITHUB_VAULT_COMMAND_INVALID:
            return (
                "Пришли обычную ссылку на GitHub repository:\n"
                "`https://github.com/owner/repository`\n"
                "или `https://github.com/owner/repository vault/path`.\n"
                "Если vault находится в корне repository, путь указывать не нужно."
            )
        case ObsidianResultType.GITHUB_VAULT_SELECTED:
            return _format_vault_selection(
                result,
                prefix="Obsidian vault подключён.",
            )
        case ObsidianResultType.GITHUB_VAULT_ALREADY_SELECTED:
            return _format_vault_selection(
                result,
                prefix="Этот Obsidian vault уже подключён.",
            )
        case (
            ObsidianResultType
            .GITHUB_VAULT_REPLACEMENT_CONFIRMATION_REQUIRED
        ):
            vault_description = _format_vault_selection(
                result,
                prefix="Предлагается новый vault.",
            )
            return (
                f"{vault_description}\n"
                "Он заменит текущее подключение и его локальные данные.\n"
                "Продолжить? Ответь `да` или `нет`."
            )
        case ObsidianResultType.GITHUB_VAULT_REPLACED:
            return _format_vault_selection(
                result,
                prefix="Obsidian vault заменён.",
            )
        case ObsidianResultType.GITHUB_VAULT_REPLACEMENT_CANCELLED:
            return _format_vault_selection(
                result,
                prefix="Замена отменена. Текущий vault сохранён.",
            )
        case ObsidianResultType.GITHUB_VAULT_CONFIRMATION_MISSING:
            return (
                "Нет ожидающей замены vault. "
                "Сначала пришли ссылку на новый GitHub repository."
            )
        case ObsidianResultType.GITHUB_VAULT_GITHUB_REQUIRED:
            return (
                "Для продолжения регистрации необходимо авторизовать GitHub."
            )
        case ObsidianResultType.GITHUB_VAULT_REPOSITORY_UNAVAILABLE:
            suffix = (
                f"\n\nНастроить GitHub App:\n{result.installation_url}"
                if result.installation_url
                else ""
            )
            return (
                "GitHub App не получила доступ на чтение и запись "
                "этого repository.\n"
                "Проверь URL, добавь repository в настройках GitHub App "
                "и разреши `Contents: read and write`."
                f"{suffix}\n\n"
                "После настройки снова пришли ссылку на repository."
            )
        case ObsidianResultType.GITHUB_VAULT_PATH_NOT_FOUND:
            return (
                "Указанный vault path не найден или ведёт не к каталогу.\n"
                "Проверь путь относительно корня repository."
            )
        case ObsidianResultType.GITHUB_VAULT_FAILED:
            return "Не удалось проверить GitHub repository. Попробуй позже."
        case ObsidianResultType.GITHUB_SYNC_COMPLETED:
            return _format_vault_sync(result)
        case ObsidianResultType.GITHUB_SYNC_FAILED:
            if isinstance(result.error, VaultConfigurationError):
                return _format_vault_configuration_error(result.error)
            return (
                "Не удалось синхронизировать Obsidian vault с GitHub. "
                "Локальная копия не была помечена как актуальная. Попробуй позже."
            )
        case ObsidianResultType.GITHUB_VAULT_CONFIGURATION_REQUIRED:
            if isinstance(result.error, VaultConfigurationError):
                return _format_vault_configuration_error(result.error)
            return (
                f"`{VAULT_CONFIGURATION_PATH}` необходимо исправить "
                "перед продолжением."
            )
        case ObsidianResultType.GITHUB_STATUS:
            return _format_github_status(result)
        case ObsidianResultType.GITHUB_DISCONNECT_NOT_CONNECTED:
            return "Obsidian vault ещё не подключён."
        case (
            ObsidianResultType
            .GITHUB_DISCONNECT_CONFIRMATION_REQUIRED
        ):
            vault = (
                result.vault_disconnect.vault
                if result.vault_disconnect is not None
                else None
            )
            repository = (
                f"`{vault.owner}/{vault.repository}`"
                if vault is not None
                else "текущий vault"
            )
            return (
                f"Отключить Obsidian vault {repository}?\n"
                "Локальный Markdown и связанные metadata будут удалены. "
                "Сам GitHub repository не изменится; профиль, связанные каналы, "
                "статьи и анализы сохранятся.\n"
                "Ответь `да` или `нет`."
            )
        case ObsidianResultType.GITHUB_DISCONNECTED:
            return (
                "Obsidian vault отключён, локальный Markdown удалён.\n"
                "GitHub repository не изменён. Профиль, связанные каналы, "
                "статьи и анализы сохранены."
            )
        case ObsidianResultType.GITHUB_DISCONNECT_CANCELLED:
            return "Отключение отменено. Текущий Obsidian vault сохранён."
        case (
            ObsidianResultType
            .GITHUB_DISCONNECT_CONFIRMATION_MISSING
        ):
            return (
                "Нет ожидающего подтверждения отключения. "
                "Сначала отправь `/github_disconnect`."
            )
        case ObsidianResultType.REANALYZE_COMMAND_INVALID:
            return "Пришли команду в формате `/reanalyze <ID статьи>`."
        case ObsidianResultType.ARTICLE_REANALYZED:
            if result.review_result is not None:
                return _format_obsidian_review(result)
            if result.analysis_result is None:
                return "Анализ обновлен, но результат не удалось подготовить."
            return format_reanalysis_result(result.analysis_result)
        case ObsidianResultType.ARTICLE_REANALYSIS_FAILED:
            return (
                "Не удалось обновить анализ статьи. Проверь ID статьи или попробуй позже."
            )
        case ObsidianResultType.ARTICLE_URL_MISSING:
            return "Пришли ссылку на статью, и я попробую ее сохранить."
        case ObsidianResultType.ARTICLE_PROCESSED:
            if result.article_result is None:
                return "Статья обработана, но результат не удалось подготовить."
            return _with_vault_sync_warning(
                format_article_processing_result(result.article_result),
                result,
            )
        case ObsidianResultType.ARTICLE_ANALYZED:
            if result.article_result is None or result.analysis_result is None:
                return "Анализ готов, но результат не удалось подготовить."
            return _with_vault_sync_warning(
                format_article_analysis_result(
                    result.article_result,
                    result.analysis_result,
                ),
                result,
            )
        case ObsidianResultType.ARTICLE_REVIEW_PREPARED:
            return _with_vault_sync_warning(
                _format_obsidian_review(result),
                result,
            )
        case ObsidianResultType.ARTICLE_REVIEW_CONFIRMATION_PENDING:
            proposal = _confirmation_proposal(result)
            target = (
                f" для `{proposal.target_path}`"
                if proposal is not None and proposal.target_path is not None
                else ""
            )
            return (
                f"Ожидаю подтверждения предложения{target}.\n"
                "Ответь строго «да», чтобы применить, или «нет», чтобы отменить."
            )
        case ObsidianResultType.ARTICLE_REVIEW_CANCELLED:
            return (
                "Предложение отменено. Vault не изменён, статья остаётся "
                "доступной для нового review."
            )
        case ObsidianResultType.ARTICLE_REVIEW_APPLIED:
            proposal = _confirmation_proposal(result)
            if proposal is None:
                return "Предложение применено."
            if proposal.action is ObsidianProposalAction.SKIP:
                return "Review завершён без изменений Obsidian vault."
            response = (
                f"Изменение применено к `{proposal.target_path}` прямым commit.\n"
                f"Commit: `{proposal.applied_commit_sha}`"
            )
            if result.error is not None:
                response += (
                    "\n\nЗаметка сохранена, но поисковый индекс пока не "
                    "обновлён. Повтори позже `/github_sync`."
                )
            return response
        case ObsidianResultType.ARTICLE_REVIEW_CONFLICT:
            return (
                "Vault изменился после подготовки предложения. Ничего не "
                "перезаписано. Отправь ссылку на статью ещё раз, чтобы "
                "подготовить новое предложение по актуальной версии."
            )
        case ObsidianResultType.ARTICLE_REVIEW_IN_PROGRESS:
            return (
                "Vault сейчас занят синхронизацией или записью из другого "
                "связанного канала. Предложение сохранено; повтори «да» позже."
            )
        case ObsidianResultType.ARTICLE_REVIEW_APPLY_FAILED:
            return (
                "Не удалось записать изменение в GitHub. Предложение и текст "
                "статьи сохранены; можно безопасно повторить «да» позже."
            )
        case ObsidianResultType.ARTICLE_REVIEWED_RESULT:
            proposal = _confirmation_proposal(result)
            if proposal is None:
                return "Эта статья уже прошла review."
            analysis = (
                f"Сохранённый анализ:\n{result.analysis_result.analysis.result_text}\n\n"
                if result.analysis_result is not None
                else ""
            )
            if proposal.action is ObsidianProposalAction.SKIP:
                return (
                    f"{analysis}Эта статья уже прошла review: было решено "
                    "не изменять vault.\n"
                    f"Причина: {proposal.reasoning}"
                )
            action = (
                "создана заметка"
                if proposal.action is ObsidianProposalAction.ADD
                else "обновлена заметка"
            )
            return (
                f"{analysis}Эта статья уже прошла review: {action} "
                f"`{proposal.target_path}`.\nCommit: "
                f"`{proposal.applied_commit_sha}`\n"
                "Повторная загрузка страницы и LLM-анализ не выполнялись."
            )
        case ObsidianResultType.ARTICLE_REVIEW_FAILED:
            return _with_vault_sync_warning(
                (
                    "Анализ статьи сохранён, но предложение для Obsidian "
                    "подготовить не удалось. Vault не изменён. Попробуй позже."
                ),
                result,
            )
        case ObsidianResultType.ARTICLE_PROCESSING_FAILED:
            return _format_processing_error(result.error)
        case ObsidianResultType.ARTICLE_ANALYSIS_FAILED:
            return _with_vault_sync_warning(
                (
                    "Статью удалось сохранить, но анализ пока не получился. "
                    "Попробуй отправить ссылку позже."
                ),
                result,
            )


def _with_vault_sync_warning(
    reply: str,
    result: ObsidianMessageResult,
) -> str:
    """Добавляет предупреждение об использовании локальной копии vault."""
    warning = result.vault_sync_warning
    if warning is None:
        return reply

    if warning.reason is VaultSyncWarningReason.IN_PROGRESS:
        reason = (
            "Vault сейчас синхронизируется в другом связанном канале, "
            "поэтому использована текущая локальная копия."
        )
    elif warning.reason is VaultSyncWarningReason.EMBEDDING_UPDATE_FAILED:
        reason = (
            "Заметки и полнотекстовый индекс актуальны, но embeddings временно "
            "недоступны. Поиск выполнен через FTS fallback."
        )
    else:
        reason = (
            "Не удалось обновить vault из GitHub, "
            "поэтому использована последняя локальная копия."
        )

    details = [f"Локально заметок: {warning.note_count}."]
    if warning.last_checked_at is not None:
        checked_at = warning.last_checked_at.astimezone(UTC)
        details.append(
            "Последняя успешная проверка GitHub: "
            f"{checked_at:%Y-%m-%d %H:%M} UTC."
        )
    return f"{reply}\n\n⚠️ {reason}\n{' '.join(details)}"


def _format_obsidian_review(result: ObsidianMessageResult) -> str:
    """Показывает безопасную сводку предложения без изменения GitHub."""
    review = result.review_result
    if review is None:
        return "Предложение для Obsidian подготовлено, но сводка недоступна."
    proposal = review.proposal
    if proposal.action is ObsidianProposalAction.SKIP:
        return (
            "Анализ готов. Предложение для Obsidian: ничего не менять.\n"
            f"Причина: {proposal.reasoning}\n\n"
            "Vault не изменён. Ответь «да», чтобы завершить review без "
            "изменений, или «нет», чтобы отменить предложение."
        )
    action = (
        "создать новую заметку"
        if proposal.action is ObsidianProposalAction.ADD
        else "обновить существующую заметку"
    )
    preview_source = (
        review.markdown_diff
        if proposal.action is ObsidianProposalAction.UPDATE
        and review.markdown_diff
        else proposal.proposed_markdown or ""
    )
    preview = preview_source[:2_000].rstrip()
    if len(preview_source) > len(preview):
        preview += "\n\n[Предпросмотр сокращён.]"
    preview_label = (
        "Предлагаемый diff"
        if proposal.action is ObsidianProposalAction.UPDATE
        else "Предлагаемый Markdown"
    )
    return (
        f"Анализ готов. Предложение для Obsidian: {action}.\n"
        f"Путь: `{proposal.target_path}`\n"
        f"Причина: {proposal.reasoning}\n\n"
        f"{preview_label}:\n{preview}\n\n"
        "Vault не изменён. Ответь «да», чтобы применить изменение, "
        "или «нет», чтобы отменить предложение."
    )


def _confirmation_proposal(result: ObsidianMessageResult):
    """Возвращает proposal из результата подтверждения без раскрытия IDs."""
    confirmation = result.obsidian_confirmation
    return confirmation.proposal if confirmation is not None else None


def _display_name(result: ObsidianMessageResult) -> str:
    """Возвращает пользовательское имя без раскрытия внутреннего ID."""
    if result.app_user is None or result.app_user.display_name is None:
        return "пользователь"
    return result.app_user.display_name


def _article_action_text(result: ProcessArticleUrlResult) -> str:
    """Выбирает первую строку ответа по тому, что сделал pipeline."""
    if result.created:
        return "Готово: статья сохранена."
    if result.extracted:
        return "Готово: статья обновлена."
    return "Эта статья уже была сохранена."


def _format_vault_selection(
    result: ObsidianMessageResult,
    *,
    prefix: str,
) -> str:
    """Формирует безопасное описание выбранного GitHub vault."""
    selection = result.vault_selection
    if selection is None:
        return prefix
    vault = selection.vault
    root_path = vault.root_path or "корень repository"
    description = (
        f"{prefix}\n"
        f"Repository: `{vault.owner}/{vault.repository}`\n"
        f"Ветка: `{vault.branch}`\n"
        f"Vault path: `{root_path}`"
    )
    if result.error is not None:
        if isinstance(result.error, VaultConfigurationError):
            return (
                f"{description}\n\n"
                f"{_format_vault_configuration_error(result.error)}\n\n"
                "Подключение сохранено. После исправления запусти `/github_sync`."
            )
        return (
            f"{description}\n\n"
            "Первая синхронизация не завершилась. Подключение сохранено; "
            "повтори через `/github_sync`."
        )
    if result.vault_sync_result is not None:
        return f"{description}\n\n{_format_vault_sync(result)}"
    return description


def _format_vault_sync(result: ObsidianMessageResult) -> str:
    """Форматирует итог первой или ручной синхронизации vault."""
    sync = result.vault_sync_result
    if sync is None:
        return "Синхронизация vault завершилась без доступной сводки."
    if sync.status is VaultSyncStatus.NO_VAULT:
        return "Obsidian vault ещё не подключён."
    if sync.status is VaultSyncStatus.IN_PROGRESS:
        return "Этот vault уже синхронизируется. Дождись завершения."
    if sync.status is VaultSyncStatus.FRESH:
        reply = (
            "Vault недавно проверен, повторный запрос к GitHub не требуется.\n"
            f"Локально заметок: {sync.total_notes}. "
            f"Файлов правил: {sync.instruction_files}."
        )
    elif sync.status is VaultSyncStatus.UNCHANGED:
        reply = (
            "Vault проверен: изменений нет.\n"
            f"Локально заметок: {sync.total_notes}. "
            f"Файлов правил: {sync.instruction_files}."
        )
    else:
        reply = (
            "Vault синхронизирован.\n"
            f"Заметок: {sync.total_notes}; скачано: {sync.downloaded_notes}; "
            f"добавлено: {sync.added_notes}; обновлено: {sync.updated_notes}; "
            f"удалено: {sync.deleted_notes}.\n"
            f"Файлов правил: {sync.instruction_files}."
        )
    if sync.embedding_update_failed:
        return f"{reply}\n\n{_format_embedding_coverage(sync.embedding_coverage)}"
    if sync.embedding_coverage is not None:
        reply += (
            "\n"
            f"Semantic index: готов (embeddings: "
            f"{sync.embedding_coverage.embedded_chunks} из "
            f"{sync.embedding_coverage.total_chunks})."
        )
        if sync.embedded_chunks or sync.unchanged_embeddings:
            reply += (
                "\nEmbeddings: добавлено: "
                f"{sync.embedded_chunks}; переиспользовано: "
                f"{sync.unchanged_embeddings}."
            )
    return reply


def _format_vault_configuration_error(error: VaultConfigurationError) -> str:
    """Форматирует безопасную ошибку обязательных правил vault."""
    if error.code is VaultConfigurationErrorCode.MISSING:
        reason = (
            f"В корне vault не найден обязательный `{VAULT_CONFIGURATION_PATH}`."
        )
    elif error.code is VaultConfigurationErrorCode.INSTRUCTION_MISSING:
        reason = f"Не найден обязательный файл правил: `{error.path}`."
    else:
        reason = f"`{VAULT_CONFIGURATION_PATH}` заполнен некорректно."
    return (
        f"{reason}\n"
        "Создай или исправь файл вручную и повтори синхронизацию.\n\n"
        "Пример:\n"
        f"```yaml\n{load_vault_configuration_example()}\n```"
    )


def _format_github_status(result: ObsidianMessageResult) -> str:
    """Форматирует состояние GitHub vault и локального каталога."""
    status = result.vault_status
    if status is None or status.vault is None:
        return "Obsidian vault ещё не подключён."
    vault = status.vault
    root = vault.root_path or "корень repository"
    checked = (
        vault.last_checked_at.strftime("%Y-%m-%d %H:%M UTC")
        if vault.last_checked_at is not None
        else "ещё не проверялся"
    )
    synced = (
        vault.last_synced_at.strftime("%Y-%m-%d %H:%M UTC")
        if vault.last_synced_at is not None
        else "ещё не синхронизировался"
    )
    reply = (
        f"Obsidian vault: `{vault.owner}/{vault.repository}`\n"
        f"Ветка: `{vault.branch}`\n"
        f"Vault path: `{root}`\n"
        f"Локально заметок: {status.note_count}\n"
        f"Файлов правил: {status.instruction_count}\n"
        f"Последняя проверка: {checked}\n"
        f"Последняя успешная синхронизация: {synced}"
    )
    if status.embedding_coverage is None:
        return f"{reply}\nSemantic index: не настроен."
    if status.embedding_index_current and status.embedding_coverage.is_complete:
        return (
            f"{reply}\nSemantic index: готов (embeddings: "
            f"{status.embedding_coverage.embedded_chunks} из "
            f"{status.embedding_coverage.total_chunks})."
        )
    return f"{reply}\n{_format_embedding_coverage(status.embedding_coverage)}"


def _format_embedding_coverage(coverage: EmbeddingIndexCoverage | None) -> str:
    """Форматирует частичное покрытие semantic index без ложного обобщения."""
    if coverage is None:
        return "Semantic index: не настроен."
    return (
        "⚠️ Semantic index пока не готов: сохранено embeddings "
        f"{coverage.embedded_chunks} из {coverage.total_chunks}.\n"
        f"Осталось обработать {coverage.missing_chunks} chunks. "
        "Пока поиск работает через FTS fallback."
    )


def _format_github_connect_response(
    result: ObsidianMessageResult,
    *,
    repeated: bool,
) -> str:
    """Формирует безопасную инструкцию Device Flow внутри регистрации."""
    connection = result.github_connection
    if connection is None or connection.authorization is None:
        return "Авторизация GitHub запускается. Подожди несколько секунд."
    prefix = (
        "Авторизация уже ожидает подтверждения."
        if repeated
        else "Для проверки repository необходимо авторизовать GitHub."
    )
    authorization = connection.authorization
    return (
        f"{prefix}\n\n"
        "1. Открой страницу авторизации:\n"
        f"{authorization.verification_uri}\n\n"
        f"2. Введи одноразовый код: `{authorization.user_code}`\n\n"
        "После авторизации бот сам продолжит подключение vault. "
        "Временный GitHub token не сохраняется."
    )


def _format_github_failure(result: ObsidianMessageResult) -> str:
    """Объясняет сбой фоновой GitHub-авторизации без технических деталей."""
    completion = result.github_completion
    if completion is None:
        return "Не удалось начать авторизацию GitHub. Попробуй позже."
    if completion.status is GitHubConnectionCompletionStatus.DENIED:
        return (
            "Авторизация GitHub отклонена. "
            "Чтобы повторить, снова пришли ссылку на repository."
        )
    if completion.status is GitHubConnectionCompletionStatus.EXPIRED:
        return (
            "Код авторизации GitHub истёк. "
            "Снова пришли ссылку на repository, чтобы получить новый."
        )
    return (
        "Не удалось завершить авторизацию GitHub. "
        "Снова пришли ссылку на repository или попробуй позже."
    )


def format_reanalysis_result(analysis_result: AnalyzeArticleResult) -> str:
    """Формирует ответ для принудительного повторного анализа."""
    article = analysis_result.article
    title = article.title or "без заголовка"
    article_id = article.id if article.id is not None else "не сохранен"
    return (
        "Анализ обновлен.\n\n"
        f"Название: {title}\n"
        f"ID статьи: {article_id}\n\n"
        f"{analysis_result.analysis.result_text}"
    )


def _format_processing_error(error: Exception | None) -> str:
    """Формирует понятный ответ пользователю по ошибке article pipeline."""
    stage = getattr(error, "stage", None)
    if stage == ProcessingStage.NORMALIZATION:
        return "Не удалось разобрать ссылку. Проверь URL и пришли его еще раз."
    if stage == ProcessingStage.FETCHING:
        return (
            "Не удалось загрузить страницу по ссылке. "
            "Проверь, что она открывается в браузере, или попробуй позже."
        )
    if stage == ProcessingStage.EXTRACTION:
        return (
            "Страница загрузилась, но текст статьи извлечь не получилось. "
            "Можно попробовать другую ссылку на эту же публикацию."
        )
    if stage == ProcessingStage.STORAGE:
        return "Не удалось сохранить статью. Попробуй позже."
    return "Не удалось обработать ссылку. Попробуй отправить ее позже."
