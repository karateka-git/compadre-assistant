from __future__ import annotations

from datetime import UTC

from compadre_assistant.core.presentation.message_content import MessageContent, compose_content, code_span, join_content

from compadre_assistant.capabilities.obsidian.presentation.vault_configuration import load_vault_configuration_example
from compadre_assistant.capabilities.obsidian.domain.articles.analyzing import AnalyzeArticleResult
from compadre_assistant.capabilities.obsidian.domain.articles.processing import ProcessArticleUrlResult
from compadre_assistant.capabilities.obsidian.domain.articles.stages import ProcessingStage
from compadre_assistant.capabilities.obsidian.presentation.processing import ObsidianMessageResult
from compadre_assistant.capabilities.obsidian.presentation.results import ObsidianResultType
from compadre_assistant.capabilities.obsidian.domain.search.models import EmbeddingIndexCoverage
from compadre_assistant.capabilities.obsidian.domain.vaults.github_models import GitHubConnectionCompletionStatus
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_configuration import (
    VAULT_CONFIGURATION_PATH,
    VaultConfigurationError,
    VaultConfigurationErrorCode,
)
from compadre_assistant.capabilities.obsidian.domain.vaults.vault_sync import VaultSyncStatus, VaultSyncWarningReason
from compadre_assistant.capabilities.obsidian.domain.articles.statuses import ArticleStatus
from compadre_assistant.capabilities.obsidian.domain.reviews.entities import ObsidianProposalAction


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


def format_article_processing_result(result: ProcessArticleUrlResult) -> MessageContent:
    """Формирует человекочитаемый ответ по результату article pipeline.

    Args:
        result: Результат обработки URL статьи.

    Returns:
        Содержимое ответа, которое адаптер оформит для внешнего канала.
    """
    article = result.article
    title = article.title or "без заголовка"
    text_length = len(article.cleaned_text or "")
    status = _ARTICLE_STATUS_LABELS.get(article.status, article.status.value)
    action = _article_action_text(result)
    article_id = article.id if article.id is not None else "не сохранен"

    return (
        compose_content(
            action,
            '\n\nНазвание: ',
            str(title),
            '\nСтатус: ',
            str(status),
            '\nID статьи: ',
            str(article_id),
            '\nТекст: ',
            str(text_length),
            ' символов',
        )
    )


def format_article_analysis_result(
    processing_result: ProcessArticleUrlResult,
    analysis_result: AnalyzeArticleResult,
) -> MessageContent:
    """Формирует ответ с полезной LLM-сводкой статьи.

    Args:
        processing_result: Результат обработки URL статьи.
        analysis_result: Результат LLM-анализа статьи.

    Returns:
        Нейтральное содержимое; текст анализа остаётся буквальными данными.
    """
    article = analysis_result.article
    title = article.title or "без заголовка"
    article_id = article.id if article.id is not None else "не сохранен"
    analysis_action = (
        "Анализ готов." if analysis_result.created else 'Использую сохранённый анализ.'
    )

    return (
        compose_content(
            _article_action_text(processing_result),
            '\n',
            str(analysis_action),
            '\n\nНазвание: ',
            str(title),
            '\nID статьи: ',
            str(article_id),
            '\n\n',
            str(analysis_result.analysis.result_text),
        )
    )


def format_obsidian_message_result(result: ObsidianMessageResult) -> MessageContent:
    """Формирует предметный ответ Obsidian для любого канала."""
    match result.type:
        case ObsidianResultType.CONFIRMATION_MISSING:
            return (
                compose_content(
                    'Сейчас нет ожидающего действия для подтверждения.\nЧтобы подключить другой repository, пришли его ссылку; для привязки канала введи ',
                    code_span('/link <code>'),
                    '.',
                )
            )
        case ObsidianResultType.GITHUB_CONNECT_STARTED:
            return compose_content(_format_github_connect_response(result, repeated=False))
        case ObsidianResultType.GITHUB_CONNECT_ALREADY_PENDING:
            return compose_content(_format_github_connect_response(result, repeated=True))
        case ObsidianResultType.GITHUB_CONNECT_PREPARING:
            return compose_content(
                'Авторизация GitHub запускается. Подожди несколько секунд, затем повтори ссылку на repository, если подключение не продолжилось.',
            )
        case ObsidianResultType.GITHUB_CONNECT_IN_PROGRESS:
            return (
                compose_content(
                    'Авторизация GitHub уже выполняется в другом связанном канале. Заверши её в исходном канале либо дождись истечения одноразового кода и снова пришли ссылку на repository.',
                )
            )
        case ObsidianResultType.GITHUB_AUTHORIZED_SYNC_STARTED:
            return (
                compose_content(
                    'GitHub авторизован. Проверяю repository и начинаю первую синхронизацию vault. Дождись следующего сообщения с результатом.',
                )
            )
        case ObsidianResultType.GITHUB_CONNECT_UNAVAILABLE:
            return compose_content('Подключение GitHub сейчас недоступно: провайдер GitHub не настроен на сервере.')
        case ObsidianResultType.LLM_NOT_CONFIGURED:
            return compose_content(
                'Анализ статей сейчас недоступен: провайдер LLM не настроен на сервере. Подключение vault и команды его обслуживания доступны отдельно, если настроен GitHub.',
            )
        case ObsidianResultType.GITHUB_CONNECT_FAILED:
            return compose_content(_format_github_failure(result))
        case ObsidianResultType.GITHUB_APP_REQUIRED:
            suffix = (
                compose_content('\n\nОткрой настройки App:\n', str(result.installation_url))
                if result.installation_url
                else ""
            )
            return (
                compose_content(
                    'Аккаунт GitHub авторизован, но GitHub App не имеет доступа к repository.',
                    suffix,
                    '\n\nВыбери repository и выдай разрешение ',
                    code_span('Contents: read and write'),
                    ', затем снова пришли ссылку на repository.',
                )
            )
        case ObsidianResultType.VAULT_REQUIRED:
            return (
                compose_content(
                    str(_display_name(result)),
                    ', хранилище заметок (vault) не подключено. Пришли ссылку на GitHub repository, чтобы подключить его.',
                )
            )
        case ObsidianResultType.GITHUB_VAULT_COMMAND_INVALID:
            return (
                compose_content(
                    'Не удалось распознать ссылку или путь к vault. Пришли GitHub repository в одном из форматов:\nhttps://github.com/owner/repository\nhttps://github.com/owner/repository vault/path\nЕсли vault находится в корне repository, путь не нужен.',
                )
            )
        case ObsidianResultType.GITHUB_VAULT_SELECTED:
            return compose_content(_format_vault_selection(
                result,
                prefix="Хранилище подключено:",
            ))
        case ObsidianResultType.GITHUB_VAULT_ALREADY_SELECTED:
            return compose_content(_format_vault_selection(
                result,
                prefix="Это хранилище уже подключено:",
            ))
        case (
            ObsidianResultType
            .GITHUB_VAULT_REPLACEMENT_CONFIRMATION_REQUIRED
        ):
            vault_description = _format_vault_selection(
                result,
                prefix="Подключить",
            )
            return (
                compose_content(
                    vault_description,
                    ' вместо текущего vault? При подтверждении текущее подключение и его локальные данные будут заменены; repository на GitHub не изменится.\nЕсли это единственное ожидающее действие Obsidian в этом чате, ответь ',
                    '«да», чтобы заменить, или «нет», чтобы сохранить текущее подключение. Ожидание действует 10 минут. Используй адресные команды ниже для нужного действия.',
                )
            )
        case ObsidianResultType.GITHUB_VAULT_REPLACED:
            return compose_content(_format_vault_selection(
                result,
                prefix="Хранилище заменено на",
            ))
        case ObsidianResultType.GITHUB_VAULT_REPLACEMENT_CANCELLED:
            return compose_content(_format_vault_selection(
                result,
                prefix="Замена отменена. Текущее подключение и его локальные данные сохранены:",
            ))
        case ObsidianResultType.GITHUB_VAULT_CONFIRMATION_MISSING:
            return (
                compose_content(
                    'Нет ожидающей замены. Пришли ссылку на новый GitHub repository, чтобы запросить подключение или замену vault.',
                )
            )
        case ObsidianResultType.GITHUB_VAULT_GITHUB_REQUIRED:
            return (
                compose_content(
                    'Чтобы продолжить подключение vault, авторизуй GitHub по инструкции в следующем сообщении.',
                )
            )
        case ObsidianResultType.GITHUB_VAULT_REPOSITORY_UNAVAILABLE:
            suffix = (
                compose_content('\n\nОткрой настройки App:\n', str(result.installation_url))
                if result.installation_url
                else ""
            )
            return (
                compose_content(
                    'GitHub App не может читать и записывать этот repository. Проверь URL, добавь repository в настройках GitHub App и выдай разрешение ',
                    code_span('Contents: read and write'),
                    '.',
                    suffix,
                    '\n\nПосле настройки снова пришли ссылку на repository.',
                )
            )
        case ObsidianResultType.GITHUB_VAULT_PATH_NOT_FOUND:
            return (
                compose_content(
                    'Папка vault не найдена или указанный путь ведёт не к папке. Проверь путь относительно корня repository и снова пришли ссылку с правильным путём.',
                )
            )
        case ObsidianResultType.GITHUB_VAULT_FAILED:
            return compose_content('Не удалось проверить GitHub repository. Повтори ссылку на repository позже.')
        case ObsidianResultType.GITHUB_SYNC_COMPLETED:
            return compose_content(_format_vault_sync(result))
        case ObsidianResultType.GITHUB_SYNC_FAILED:
            if isinstance(result.error, VaultConfigurationError):
                return compose_content(_format_vault_configuration_error(result.error))
            return (
                compose_content(
                    'Не удалось завершить синхронизацию vault с GitHub. Локальная копия не помечена как актуальная; её содержимое могло не обновиться. Повтори /vault_sync позже.',
                )
            )
        case ObsidianResultType.GITHUB_VAULT_CONFIGURATION_REQUIRED:
            if isinstance(result.error, VaultConfigurationError):
                return compose_content(_format_vault_configuration_error(result.error))
            return (
                compose_content(
                    code_span(str(VAULT_CONFIGURATION_PATH)),
                    ' нужно исправить; синхронизация не завершена. Исправь файл и вызови /vault_sync.',
                )
            )
        case ObsidianResultType.GITHUB_STATUS:
            return compose_content(_format_github_status(result))
        case ObsidianResultType.GITHUB_DISCONNECT_NOT_CONNECTED:
            return compose_content(
                'Хранилище заметок (vault) не подключено. Чтобы подключить его, пришли ссылку на GitHub repository.',
            )
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
                compose_content(code_span(str(vault.owner) + '/' + str(vault.repository)))
                if vault is not None
                else "текущий vault"
            )
            return (
                compose_content(
                    'Отключить хранилище ',
                    repository,
                    '? При подтверждении локальные Markdown-файлы и связанные metadata будут удалены. Repository на GitHub не изменится; профиль, связанные каналы, статьи и анализы сохранятся.\nЕсли это единственное ожидающее действие Obsidian в этом чате, ответь «да», чтобы отключить, или «нет», чтобы оставить vault подключённым. Ожидание действует 10 минут. Используй адресные команды ниже для нужного действия.',
                )
            )
        case ObsidianResultType.GITHUB_DISCONNECTED:
            return (
                compose_content(
                    'Vault отключён. Локальные Markdown-файлы удалены; repository на GitHub не изменён. Профиль, связанные каналы, статьи и анализы сохранены.',
                )
            )
        case ObsidianResultType.GITHUB_DISCONNECT_CANCELLED:
            return compose_content('Отключение отменено. Текущий vault и его локальная копия сохранены.')
        case (
            ObsidianResultType
            .GITHUB_DISCONNECT_CONFIRMATION_MISSING
        ):
            return (
                compose_content(
                    'Нет запроса на отключение vault. Вызови /vault_disconnect, чтобы запросить отключение.',
                )
            )
        case ObsidianResultType.REANALYZE_COMMAND_INVALID:
            return compose_content(
                'Для повторного анализа нужен числовой ID статьи. Используй /reanalyze <article_id>.',
            )
        case ObsidianResultType.ARTICLE_REANALYZED:
            if result.review_result is not None:
                return compose_content(_format_obsidian_review(result))
            if result.analysis_result is None:
                return compose_content('Анализ обновлен, но результат не удалось подготовить.')
            return compose_content(format_reanalysis_result(result.analysis_result))
        case ObsidianResultType.ARTICLE_REANALYSIS_FAILED:
            return (
                compose_content('Не удалось обновить анализ статьи. Проверь ID статьи или попробуй позже.')
            )
        case ObsidianResultType.ARTICLE_URL_MISSING:
            return compose_content('Пришли ссылку на статью, чтобы я попробовал её сохранить.')
        case ObsidianResultType.ARTICLE_PROCESSED:
            if result.article_result is None:
                return compose_content('Статья обработана, но результат не удалось подготовить.')
            return compose_content(_with_vault_sync_warning(
                format_article_processing_result(result.article_result),
                result,
            ))
        case ObsidianResultType.ARTICLE_ANALYZED:
            if result.article_result is None or result.analysis_result is None:
                return compose_content('Анализ готов, но результат не удалось подготовить.')
            return compose_content(_with_vault_sync_warning(
                format_article_analysis_result(
                    result.article_result,
                    result.analysis_result,
                ),
                result,
            ))
        case ObsidianResultType.ARTICLE_REVIEW_PREPARED:
            return compose_content(_with_vault_sync_warning(
                _format_obsidian_review(result),
                result,
            ))
        case ObsidianResultType.ARTICLE_REVIEW_CONFIRMATION_PENDING:
            proposal = _confirmation_proposal(result)
            target = (
                compose_content(' для заметки ', code_span(str(proposal.target_path)))
                if proposal is not None and proposal.target_path is not None
                else ""
            )
            return (
                compose_content(
                    'Ожидаю решения по предложению',
                    target,
                    '. Если в этом чате ожидает только одно действие Obsidian, ответь «да», чтобы применить предложение, или «нет», чтобы отменить его.',
                )
            )
        case ObsidianResultType.ARTICLE_REVIEW_CANCELLED:
            return (
                compose_content(
                    'Предложение отменено. Хранилище не изменено; статью можно отправить на новое рассмотрение.',
                )
            )
        case ObsidianResultType.ARTICLE_REVIEW_APPLIED:
            proposal = _confirmation_proposal(result)
            if proposal is None:
                return compose_content('Предложение применено.')
            if proposal.action is ObsidianProposalAction.SKIP:
                return compose_content('Рассмотрение завершено. Хранилище не изменено.')
            response = (
                compose_content(
                    'Изменение записано в GitHub прямым commit.\nПуть: ',
                    code_span(str(proposal.target_path)),
                    '\nCommit: ',
                    code_span(str(proposal.applied_commit_sha)),
                )
            )
            if result.error is not None:
                response = compose_content(response, (
                    "\n\nЗаметка записана в GitHub, но поисковый индекс ещё не обновлён. Запусти /vault_sync, чтобы обновить его."
                ))
            return compose_content(response)
        case ObsidianResultType.ARTICLE_REVIEW_CONFLICT:
            return (
                compose_content(
                    'Хранилище изменилось после подготовки предложения, поэтому запись отменена и чужие изменения не перезаписаны. Отправь ссылку на статью ещё раз, чтобы подготовить предложение по актуальной версии.',
                )
            )
        case ObsidianResultType.ARTICLE_REVIEW_IN_PROGRESS:
            return (
                compose_content(
                    'Хранилище сейчас занято синхронизацией или записью из другого связанного канала. Предложение сохранено. После завершения операции, если в этом чате ожидает только одно действие Obsidian, ответь «да», чтобы повторить запись, или «нет», чтобы отменить предложение.',
                )
            )
        case ObsidianResultType.ARTICLE_REVIEW_APPLY_FAILED:
            return (
                compose_content(
                    'Не удалось записать изменение в GitHub. Предложение и текст статьи сохранены. После устранения ошибки, если в этом чате ожидает только одно действие Obsidian, ответь «да», чтобы повторить запись, или «нет», чтобы отменить предложение.',
                )
            )
        case ObsidianResultType.ARTICLE_REVIEWED_RESULT:
            proposal = _confirmation_proposal(result)
            if proposal is None:
                return compose_content('Эта статья уже рассмотрена.')
            analysis = (
                compose_content('Сохранённый анализ:\n', str(result.analysis_result.analysis.result_text), '\n\n')
                if result.analysis_result is not None
                else ""
            )
            if proposal.action is ObsidianProposalAction.SKIP:
                return (
                    compose_content(
                        analysis,
                        'Эта статья уже рассмотрена: было решено не менять хранилище.\nПричина: ',
                        str(proposal.reasoning),
                    )
                )
            action = (
                "создана заметка"
                if proposal.action is ObsidianProposalAction.ADD
                else "обновлена заметка"
            )
            return (
                compose_content(
                    analysis,
                    'Эта статья уже рассмотрена: ',
                    str(action),
                    ' ',
                    code_span(str(proposal.target_path)),
                    '.\nCommit: ',
                    code_span(str(proposal.applied_commit_sha)),
                    '\nСтраница повторно не загружалась, анализ заново не запускался.',
                )
            )
        case ObsidianResultType.ARTICLE_REVIEW_FAILED:
            return compose_content(_with_vault_sync_warning(
                (
                    'Анализ статьи сохранён, но подготовить предложение для Obsidian не удалось. Хранилище не изменено. Попробуй позже.'
                ),
                result,
            ))
        case ObsidianResultType.ARTICLE_PROCESSING_FAILED:
            return compose_content(_format_processing_error(result.error))
        case ObsidianResultType.ARTICLE_ANALYSIS_FAILED:
            return compose_content(_with_vault_sync_warning(
                (
                    "Статью удалось сохранить, но анализ пока не получился. "
                    "Попробуй отправить ссылку позже."
                ),
                result,
            ))


def _with_vault_sync_warning(
    reply: str | MessageContent,
    result: ObsidianMessageResult,
) -> MessageContent:
    """Добавляет предупреждение об использовании локальной копии vault."""
    warning = result.vault_sync_warning
    if warning is None:
        return compose_content(reply)

    if warning.reason is VaultSyncWarningReason.IN_PROGRESS:
        reason = (
            'Хранилище сейчас синхронизируется в другом связанном канале. Для этой операции использована текущая локальная копия.'
        )
    elif warning.reason is VaultSyncWarningReason.EMBEDDING_UPDATE_FAILED:
        reason = (
            'Заметки и полнотекстовый индекс актуальны, но семантический поиск временно недоступен. Поиск выполнен через полнотекстовый поиск (FTS fallback).'
        )
    else:
        reason = (
            'Не удалось обновить хранилище из GitHub. Использована последняя локальная копия.'
        )

    details = [compose_content('Локально заметок: ', str(warning.note_count), '.')]
    if warning.last_checked_at is not None:
        checked_at = warning.last_checked_at.astimezone(UTC)
        details.append(
            compose_content('Последняя успешная проверка GitHub: ', format(checked_at, '%Y-%m-%d %H:%M'), ' UTC.')
        )
    return compose_content(reply, '\n\n⚠️ ', reason, '\n', join_content(' ', details))


def _format_obsidian_review(result: ObsidianMessageResult) -> MessageContent:
    """Показывает безопасную сводку предложения без изменения GitHub."""
    review = result.review_result
    if review is None:
        return compose_content('Предложение для Obsidian подготовлено, но сводка недоступна.')
    proposal = review.proposal
    if proposal.action is ObsidianProposalAction.SKIP:
        return (
            compose_content(
                'Анализ готов. Предлагаю не менять хранилище.\nПричина: ',
                str(proposal.reasoning),
                '\n\nХранилище пока не изменено. Если в этом чате ожидает только одно действие Obsidian, ответь «да», чтобы завершить рассмотрение без изменений, или «нет», чтобы отменить предложение.',
            )
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
        else "Предлагаемый текст заметки"
    )
    return (
        compose_content(
            'Анализ готов. Предлагаю ',
            str(action),
            '.\nПуть: ',
            code_span(str(proposal.target_path)),
            '\nПричина: ',
            str(proposal.reasoning),
            '\n\n',
            str(preview_label),
            ':\n',
            str(preview),
            '\n\nХранилище пока не изменено. Если в этом чате ожидает только одно действие Obsidian, ответь «да», чтобы применить предложение, или «нет», чтобы отменить его.',
        )
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


def _article_action_text(result: ProcessArticleUrlResult) -> MessageContent:
    """Выбирает первую строку ответа по тому, что сделал pipeline."""
    if result.created:
        return compose_content('Готово: статья сохранена.')
    if result.extracted:
        return compose_content('Готово: статья обновлена.')
    return compose_content('Эта статья уже была сохранена.')


def _format_vault_selection(
    result: ObsidianMessageResult,
    *,
    prefix: str,
) -> MessageContent:
    """Формирует безопасное описание выбранного GitHub vault."""
    selection = result.vault_selection
    if selection is None:
        return compose_content(prefix)
    vault = selection.vault
    root_path = vault.root_path or "корень repository"
    description = (
        compose_content(
            str(prefix),
            ' ',
            code_span(str(vault.owner) + '/' + str(vault.repository)),
            ', ветка ',
            code_span(str(vault.branch)),
            ', папка ',
            code_span(str(root_path)),
        )
    )
    if result.type is not ObsidianResultType.GITHUB_VAULT_REPLACEMENT_CONFIRMATION_REQUIRED:
        description = compose_content(description, '.')
    if result.error is not None:
        if isinstance(result.error, VaultConfigurationError):
            return (
                compose_content(
                    description,
                    '\n\n',
                    _format_vault_configuration_error(result.error),
                    '\n\nПодключение сохранено. После исправления запусти ',
                    code_span('/vault_sync'),
                    '.',
                )
            )
        return (
            compose_content(
                description,
                '\n\nПодключение vault сохранено, но первая синхронизация не завершилась. Повтори ',
                code_span('/vault_sync'),
                '.',
            )
        )
    if result.vault_sync_result is not None:
        return compose_content(
            description, '\n\n',
            'Первая синхронизация с GitHub: ' if result.type is ObsidianResultType.GITHUB_VAULT_SELECTED else '',
            _format_vault_sync(result),
        )
    return compose_content(description)


def _format_vault_sync(result: ObsidianMessageResult) -> MessageContent:
    """Форматирует итог первой или ручной синхронизации vault."""
    sync = result.vault_sync_result
    if sync is None:
        return compose_content(
            'Синхронизация vault завершилась, но подробная сводка недоступна. Вызови /vault_status, чтобы проверить состояние локальной копии.',
        )
    if sync.status is VaultSyncStatus.NO_VAULT:
        return compose_content(
            'Хранилище заметок (vault) не подключено. Чтобы подключить его, пришли ссылку на GitHub repository.',
        )
    if sync.status is VaultSyncStatus.IN_PROGRESS:
        return compose_content(
            'Синхронизация vault с GitHub уже идёт. После завершения вызови /vault_status, чтобы проверить состояние локальной копии.',
        )
    if sync.status is VaultSyncStatus.FRESH:
        reply = (
            compose_content(
                'GitHub проверялся недавно, поэтому новый запрос не выполнялся. В локальной копии: заметок ',
                str(sync.total_notes),
                '. Файлов правил: ',
                str(sync.instruction_files),
                '.',
            )
        )
    elif sync.status is VaultSyncStatus.UNCHANGED:
        reply = (
            compose_content(
                'GitHub проверен: изменений для локальной копии нет. В локальной копии: заметок ',
                str(sync.total_notes),
                '. Файлов правил: ',
                str(sync.instruction_files),
                '.',
            )
        )
    else:
        reply = (
            compose_content(
                'Локальная копия vault обновлена из GitHub. Заметок: всего ',
                str(sync.total_notes),
                '; скачано: ',
                str(sync.downloaded_notes),
                '; добавлено: ',
                str(sync.added_notes),
                '; обновлено: ',
                str(sync.updated_notes),
                '; удалено: ',
                str(sync.deleted_notes),
                '.\nФайлов правил: ',
                str(sync.instruction_files),
                '.',
            )
        )
    if sync.embedding_update_failed:
        return compose_content(reply, '\n\n', _format_embedding_coverage(sync.embedding_coverage))
    if sync.embedding_coverage is not None:
        reply = compose_content(reply, (
            compose_content(
                '\nСемантический индекс готов: embeddings ',
                str(sync.embedding_coverage.embedded_chunks),
                ' из ',
                str(sync.embedding_coverage.total_chunks),
                '.',
            )
        ))
        if sync.embedded_chunks or sync.unchanged_embeddings:
            reply = compose_content(reply, (
                compose_content(
                    '\nEmbeddings: добавлено ',
                    str(sync.embedded_chunks),
                    ', переиспользовано ',
                    str(sync.unchanged_embeddings),
                    '.',
                )
            ))
    return compose_content(reply)


def _format_vault_configuration_error(error: VaultConfigurationError) -> MessageContent:
    """Форматирует безопасную ошибку обязательных правил vault."""
    if error.code is VaultConfigurationErrorCode.MISSING:
        reason = (
            compose_content(
                'Не найден обязательный файл конфигурации ',
                code_span(str(VAULT_CONFIGURATION_PATH)),
                '.',
            )
        )
    elif error.code is VaultConfigurationErrorCode.INSTRUCTION_MISSING:
        reason = compose_content('Не найден обязательный файл правил ', code_span(str(error.path)), '.')
    else:
        reason = compose_content(code_span(str(VAULT_CONFIGURATION_PATH)), ' заполнен некорректно.')
    return (
        compose_content(
            reason,
            '\nСинхронизация не завершена. Создай или исправь файл вручную по примеру ниже, затем вызови /vault_sync.\n\nПример:\n',
            str(load_vault_configuration_example()),
        )
    )


def _format_github_status(result: ObsidianMessageResult) -> MessageContent:
    """Форматирует состояние GitHub vault и локального каталога."""
    status = result.vault_status
    if status is None or status.vault is None:
        return compose_content(
            'Хранилище заметок (vault) не подключено. Чтобы подключить его, пришли ссылку на GitHub repository.',
        )
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
        compose_content(
            'Хранилище: ',
            code_span(str(vault.owner) + '/' + str(vault.repository)),
            ', ветка ',
            code_span(str(vault.branch)),
            ', папка ',
            code_span(str(root)),
            '\nЛокально заметок: ',
            str(status.note_count),
            '\nФайлов правил: ',
            str(status.instruction_count),
            '\nПоследняя проверка: ',
            str(checked),
            '\nПоследняя успешная синхронизация: ',
            str(synced),
        )
    )
    if status.embedding_coverage is None:
        return compose_content(reply, '\nСемантический индекс не настроен. Поиск использует текстовый индекс FTS.')
    if status.embedding_index_current and status.embedding_coverage.is_complete:
        return (
            compose_content(
                reply,
                '\nСемантический индекс готов: embeddings ',
                str(status.embedding_coverage.embedded_chunks),
                ' из ',
                str(status.embedding_coverage.total_chunks),
                '.',
            )
        )
    return compose_content(reply, '\n', _format_embedding_coverage(status.embedding_coverage))


def _format_embedding_coverage(coverage: EmbeddingIndexCoverage | None) -> MessageContent:
    """Форматирует частичное покрытие semantic index без ложного обобщения."""
    if coverage is None:
        return compose_content('Семантический индекс не настроен. Поиск использует текстовый индекс FTS.')
    return (
        compose_content(
            'Семантический индекс ещё обрабатывается: embeddings ',
            str(coverage.embedded_chunks),
            ' из ',
            str(coverage.total_chunks),
            ', осталось chunks ',
            str(coverage.missing_chunks),
            '. Пока индекс не готов, поиск работает через FTS fallback.',
        )
    )


def _format_github_connect_response(
    result: ObsidianMessageResult,
    *,
    repeated: bool,
) -> MessageContent:
    """Формирует безопасную инструкцию Device Flow внутри регистрации."""
    connection = result.github_connection
    if connection is None or connection.authorization is None:
        return compose_content(
            'Авторизация GitHub запускается. Подожди несколько секунд, затем повтори ссылку на repository, если подключение не продолжилось.',
        )
    prefix = (
        'Авторизация GitHub ждёт подтверждения.'
        if repeated
        else 'Чтобы проверить repository, авторизуй GitHub.'
    )
    authorization = connection.authorization
    continuation = (
        'После подтверждения бот продолжит проверку repository и подключение vault.'
        if repeated
        else 'После подтверждения бот проверит repository и продолжит подключение vault.'
    )
    return (
        compose_content(
            str(prefix),
            '\n\n1. Открой страницу:\n',
            str(authorization.verification_uri),
            '\n\n2. Введи одноразовый код: ',
            code_span(str(authorization.user_code)),
            '.\n\n',
            continuation,
            ' Временный GitHub token не сохраняется.',
        )
    )


def _format_github_failure(result: ObsidianMessageResult) -> MessageContent:
    """Объясняет сбой фоновой GitHub-авторизации без технических деталей."""
    completion = result.github_completion
    if completion is None:
        return compose_content(
            'Не удалось начать авторизацию GitHub. Снова пришли ссылку на repository, чтобы повторить запрос.',
        )
    if completion.status is GitHubConnectionCompletionStatus.DENIED:
        return (
            compose_content(
                'Авторизация GitHub отклонена; repository не подключён. Чтобы начать заново, снова пришли ссылку на repository.',
            )
        )
    if completion.status is GitHubConnectionCompletionStatus.EXPIRED:
        return (
            compose_content(
                'Срок одноразового кода GitHub истёк, поэтому авторизация не завершена. Снова пришли ссылку на repository, чтобы получить новый код.',
            )
        )
    return (
        compose_content(
            'Не удалось завершить авторизацию GitHub; подключение vault не продолжено. Снова пришли ссылку на repository, чтобы повторить авторизацию.',
        )
    )


def format_reanalysis_result(analysis_result: AnalyzeArticleResult) -> MessageContent:
    """Формирует ответ для принудительного повторного анализа."""
    article = analysis_result.article
    title = article.title or "без заголовка"
    article_id = article.id if article.id is not None else "не сохранен"
    return (
        compose_content(
            'Анализ обновлён.\nНазвание: ',
            str(title),
            '\nID статьи: ',
            str(article_id),
            '\n\n',
            str(analysis_result.analysis.result_text),
        )
    )


def _format_processing_error(error: Exception | None) -> MessageContent:
    """Формирует понятный ответ пользователю по ошибке article pipeline."""
    stage = getattr(error, "stage", None)
    if stage == ProcessingStage.NORMALIZATION:
        return compose_content('Не удалось разобрать ссылку. Проверь URL и пришли его еще раз.')
    if stage == ProcessingStage.FETCHING:
        return (
            compose_content(
                'Не удалось загрузить страницу по ссылке. Проверь, что она открывается в браузере, или попробуй позже.',
            )
        )
    if stage == ProcessingStage.EXTRACTION:
        return (
            compose_content(
                'Страница загрузилась, но текст статьи извлечь не получилось. Можно попробовать другую ссылку на эту же публикацию.',
            )
        )
    if stage == ProcessingStage.STORAGE:
        return compose_content('Не удалось сохранить статью. Попробуй позже.')
    return compose_content('Не удалось обработать ссылку. Отправь её ещё раз позже.')
