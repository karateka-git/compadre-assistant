"""Оформляет адресную сводку и связывает вход с текущим членством получателя."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from contextlib import AbstractContextManager
from collections.abc import Callable
import logging
from hashlib import sha256
from json import dumps

from compadre_assistant.capabilities.family.domain.shopping import ShoppingItem
from compadre_assistant.capabilities.family.domain.shopping_notifications import ShoppingBatch, ShoppingBatchRepository
from compadre_assistant.capabilities.family.presentation.members import MemberDisplayNames
from compadre_assistant.core.presentation.notifications import (
    NotificationDeclined, NotificationDelivery, NotificationRetry, NotificationRoute, NotificationSource,
)
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
    return IncomingResponse(text_content(f"🛒 Покупки · {who}\nНовых позиций: {len(items)}.\n" +
                                         preview), bound_actions=(action,))


class ShoppingNotifications(NotificationSource):
    """Проверяет актуальный состав созревшей подборки и фиксирует результат отправки."""

    def __init__(
        self, open_queue: Callable[[], AbstractContextManager[ShoppingBatchRepository]], names: MemberDisplayNames,
    ) -> None:
        """Принимает фабрику очереди семейного модуля и чтение отображаемых имён."""
        self._open_queue = open_queue
        self._names = names

    def dispatch(self, channel: str, now: datetime, route: NotificationRoute, send: NotificationDelivery) -> None:
        """Передаёт актуальные сводки в личные диалоги через интерфейс ядра.

        Args:
            channel: Канал работающего процесса.
            now: Время проверки сроков в UTC.
            route: Проверка доступа, выбора канала и актуальной личной привязки.
            send: Одна попытка отправки. NotificationRetry означает явный отказ
                канала; прочее исключение может означать потерю подтверждения.
        """
        with self._open_queue() as queue:
            for batch in queue.due_batches(now):
                try:
                    target = route(batch.recipient_id)
                    items = queue.current_items(batch)
                    if not items or not target.allowed:
                        if queue.claim(batch, now, expected_items=items):
                            queue.finish(batch)
                        continue
                    address = target.address
                    if address is None or address.channel != channel:
                        continue
                    authors = tuple(dict.fromkeys(i.created_by_app_user_id for i in items
                                                  if i.created_by_app_user_id is not None))
                    response = shopping_notification(batch, items, self._names(authors), address)
                    # Сначала полностью готовим сообщение. Начало передачи фиксируем непосредственно
                    # перед API, сверяя актуальные позиции и членство в той же транзакции.
                    if not queue.claim(batch, now, expected_items=items):
                        continue
                except Exception:
                    logging.getLogger(__name__).exception("Could not prepare shopping notification: batch=%s", batch.id)
                    continue
                try:
                    send(batch.recipient_id, address, response)
                except NotificationDeclined:
                    queue.finish(batch)
                except NotificationRetry as retry:
                    queue.finish(batch, now + timedelta(seconds=max(1, retry.seconds)))
                except Exception:
                    # API мог уже принять сообщение. Запись попытки переживёт рестарт.
                    logging.getLogger(__name__).exception("Shopping notification result unknown: batch=%s", batch.id)
                else:
                    queue.finish(batch)
