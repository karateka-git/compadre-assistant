"""Оформляет адресную сводку и связывает вход с текущим членством получателя."""

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from json import dumps

from compadre_assistant.capabilities.family.domain.shopping import ShoppingItem
from compadre_assistant.capabilities.family.domain.shopping_notifications import ShoppingBatch
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import BoundReplyAction, IncomingResponse
from compadre_assistant.core.presentation.message_content import text_content


def shopping_reference(family_id: int, recipient_id: int, joined_at: datetime, address: ReplyAddress) -> str:
    """Связывает переход с семьёй, поколением членства, профилем и личным чатом.

    Ядро самостоятельно проверяет личность и допуск. Отпечаток переживает
    перезапуск и позволяет обнаружить смену семьи или владельца адреса.
    """
    identity = (family_id, recipient_id, joined_at.isoformat(), address.channel, address.chat_id)
    return sha256(dumps(identity).encode()).hexdigest()[:32]


def shopping_notification(
    batch: ShoppingBatch, items: tuple[ShoppingItem, ...], names: Mapping[int, str | None], address: ReplyAddress,
) -> IncomingResponse:
    """Формирует один короткий анонс из актуальных позиций и имён авторов.

    Args:
        batch: Проверенная подборка, связанная с членством получателя.
        items: Непустая последовательность позиций в порядке добавления.
        names: Доступные имена авторов из текущей семьи.
        address: Проверенный адрес для отправки и последующего входа.
    """
    authors = tuple(dict.fromkeys(item.created_by_app_user_id for item in items))
    labels = [((names.get(author) if author is not None else None) or "Участник семьи")[:48] for author in authors[:3]]
    who = ", ".join(labels)
    if len(authors) > len(labels):
        who += f" и ещё {len(authors) - len(labels)}"
    titles = [item.name if len(item.name) <= 80 else item.name[:79] + "…" for item in items[:5]]
    preview = ", ".join(titles)
    if len(items) > len(titles):
        preview += f" и ещё {len(items) - len(titles)}"
    reference = shopping_reference(batch.family_id, batch.recipient_id, batch.joined_at, address)
    action = BoundReplyAction("Открыть покупки", f"notice.family.{reference}", str(batch.id))
    return IncomingResponse(text_content(f"🛒 Список пополнили: {who}. Новых позиций: {len(items)}.\n" +
                                         preview), bound_actions=(action,))
