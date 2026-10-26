"""Чтение буквального содержимого нового контракта в проверках presentation."""
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse
from compadre_assistant.core.presentation.message_content import MessageContent
from compadre_assistant.presentation.message_rendering import content_groups


def content_text(content: MessageContent) -> str:
    """Возвращает видимый текст блоков без зависимости от временного API ответа."""
    return "".join(span.text for group in content_groups(content, ()) for span in group)


def response_text(response: IncomingResponse) -> str:
    """Читает содержание ответа, оставляя действия для отдельных проверок."""
    return content_text(response.content)
