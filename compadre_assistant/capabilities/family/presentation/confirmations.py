"""Хранит одноразовые подтверждения семейных действий в исходном чате."""

from dataclasses import dataclass
from secrets import randbits
from threading import Lock
from time import monotonic

from compadre_assistant.capabilities.family.domain.membership import FamilyMember
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import ConfirmationAction, HandlerContext, PendingConfirmation


_TTL_SECONDS = 600  # Время на выбор в исходном чате.
_VERSION = 1  # Замена запроса всегда создаёт новый ID, повторных версий одного запроса нет.


@dataclass(frozen=True, slots=True)
class MemberConfirmation:
    """Связывает адресное решение с ревизией режима и проверенным членством."""

    mode_revision: int
    family_id: int
    actor: FamilyMember
    target: FamilyMember | None
    expires_at: float
    confirmation: PendingConfirmation


class MemberConfirmations:
    """Выдаёт и принимает подтверждения в пределах текущего процесса."""

    def __init__(self) -> None:
        """Создаёт пустое хранилище ожиданий."""
        self._pending: dict[tuple[int, ReplyAddress], MemberConfirmation] = {}
        self._lock = Lock()

    def begin(
        self, context: HandlerContext, address: ReplyAddress, operation: str, family_id: int,
        actor: FamilyMember, target: FamilyMember | None, label: str,
    ) -> PendingConfirmation:
        """Заменяет запрос этого профиля в исходном чате новым подтверждением.

        Args:
            context: Проверенный профиль и ревизия активного режима.
            address: Исходный чат запроса.
            operation: Вид подтверждаемого семейного действия.
            family_id: Семья, к которой относится действие.
            actor: Проверенное членство исполнителя.
            target: Проверенное членство цели, когда она требуется.
            label: Подпись кнопки согласия.

        Returns:
            Адресное подтверждение для общего меню и кнопок.

        Raises:
            ValueError: Контекст передан без ревизии активного режима.
        """
        if context.mode_revision is None:
            raise ValueError("Для подтверждения нужен активный семейный режим")
        confirmation_id = randbits(62) + 1
        actions = tuple(ConfirmationAction(
            f"family:{operation}:{confirmation_id}:{_VERSION}:{decision}", button,
            f"/confirm family {operation} {confirmation_id} {_VERSION} {decision}",
        ) for decision, button in (("yes", label), ("no", "Отменить")))
        confirmation = PendingConfirmation("family", operation, confirmation_id, _VERSION, address, actions)
        with self._lock:
            now = monotonic()
            self._pending = {key: value for key, value in self._pending.items() if now < value.expires_at}
            self._pending[context.app_user.id, address] = MemberConfirmation(
                context.mode_revision, family_id, actor, target, now + _TTL_SECONDS, confirmation,
            )
        return confirmation

    def current(self, context: HandlerContext, address: ReplyAddress) -> MemberConfirmation | None:
        """Читает актуальный запрос профиля, не принимая решение."""
        with self._lock:
            return self._current(context, address)

    def take(
        self, context: HandlerContext, address: ReplyAddress, command: str,
    ) -> tuple[MemberConfirmation, bool] | None:
        """Одноразово принимает точную команду из исходного чата.

        Args:
            context: Допущенный профиль и текущая ревизия режима.
            address: Чат входящего события.
            command: Полная адресная команда, включая решение.

        Returns:
            Исходный запрос и признак согласия либо None без совпадения.
        """
        with self._lock:
            pending = self._current(context, address)
            if pending is None:
                return None
            approve, cancel = pending.confirmation.actions
            if command not in (approve.command, cancel.command):
                return None
            del self._pending[context.app_user.id, address]
            return pending, command == approve.command

    def _current(self, context: HandlerContext, address: ReplyAddress) -> MemberConfirmation | None:
        pending = self._pending.get((context.app_user.id, address))
        if pending is not None and pending.mode_revision == context.mode_revision and monotonic() < pending.expires_at:
            return pending
        return None
