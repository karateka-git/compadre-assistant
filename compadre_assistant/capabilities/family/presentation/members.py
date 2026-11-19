"""Показывает текущий состав семьи и связывает выбор с исходным чатом."""

from base64 import urlsafe_b64encode
from collections.abc import Mapping
from hmac import compare_digest, digest
from json import dumps
from secrets import token_bytes
from typing import Protocol

from compadre_assistant.capabilities.family.domain.membership import Family, FamilyMember
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope, ScreenDestination
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import HandlerContext, IncomingResponse, ReplyAction, ResponseView
from compadre_assistant.core.presentation.message_content import code_span, compose_content, text_content


MEMBERS_COMMAND = "/family_members"
LEAVE_COMMAND = "/family_leave"
REMOVE_COMMAND = "/family_remove"
TRANSFER_COMMAND = "/family_transfer"
CLOSE_COMMAND = "/family_close"
_PAGE_SIZE = 10  # Верхний предел числа участников на странице.
_PREVIOUS = "Предыдущая"
_NEXT = "Следующая"
_BACK = "Назад"
_LEAVE = "Выйти из семьи"
_CLOSE = "Закрыть семью"
_REMOVE = "Исключить"
_TRANSFER = "Передать роль"
_UNAVAILABLE = compose_content("Открой актуальный состав через ", code_span(MEMBERS_COMMAND), ".")


def _members_view(*, update: bool = False) -> ResponseView:
    return ResponseView(
        "family-members", update=update, unavailable=_UNAVAILABLE, cleanup_previous=True,
        allow_multiple_messages=True,
        action_scope=ActionScope.SCREEN,
    )


class MemberDisplayNames(Protocol):
    """Читает только отображаемые имена уже проверенных участников."""

    def __call__(self, app_user_ids: tuple[int, ...]) -> Mapping[int, str | None]:
        """Возвращает имена по идентификаторам из проверенного состава семьи."""
        ...


class MembersView:
    """Показывает страницы состава и проверяет короткие ссылки на участников."""

    def __init__(self, display_names: MemberDisplayNames) -> None:
        """Получает имена через предоставленный при сборке порт."""
        self._display_names = display_names
        self._key = token_bytes(32)

    def show(
        self, context: HandlerContext, address: ReplyAddress, family: Family,
        members: tuple[FamilyMember, ...], page: int, *, show_commands: bool = False, update: bool = False,
    ) -> IncomingResponse:
        """Показывает состав семьи на запрошенной странице.

        Args:
            context: Допущенный профиль и ревизия семейного режима.
            address: Исходный чат Telegram или VK.
            family: Семья, членство в которой проверено.
            members: Актуальный состав из семейного сценария.
            page: Номер страницы, начиная с единицы.
            show_commands: Показать ручные команды вместо кнопок.
            update: Обновить доставленный список по кнопке перелистывания.

        Returns:
            Список участников с кнопками или точными командами выбора.
        """
        actor = next(member for member in members if member.app_user_id == context.app_user.id)
        operation = None
        if not actor.is_owner:
            operation = (_LEAVE, LEAVE_COMMAND)
        elif len(members) == 1:
            operation = (_CLOSE, CLOSE_COMMAND)

        layout = context.action_layout
        # Два перехода по страницам и действия внизу занимают отдельные группы.
        reserved_buttons = 4 if operation else 3
        capacity = layout.selection_capacity(
            reserved_rows=reserved_buttons if layout.max_columns == 1 else 2, reserved_buttons=reserved_buttons,
        )
        min_label_length = max(len(_PREVIOUS), len(operation[0]) if operation else len(_BACK))
        inline = capacity > 0 and layout.max_label_length >= min_label_length
        page_size = min(_PAGE_SIZE, capacity) if inline else _PAGE_SIZE
        show_commands = show_commands or not inline

        total_pages = max(1, (len(members) + page_size - 1) // page_size)
        page = min(page, total_pages)
        first = (page - 1) * page_size
        visible = members[first:first + page_size]
        names = self._display_names(tuple(member.app_user_id for member in visible))
        parts = [f"Участники семьи «{family.name}»"]
        if total_pages > 1:
            parts.append(f" · {page}/{total_pages}")
        actions = []
        for row, member in enumerate(visible):
            name = names.get(member.app_user_id) or "Участник"
            role = "владелец" if member.is_owner else "участник"
            own_label = ", ты" if member.app_user_id == context.app_user.id else ""
            reference = self._reference(context, address, family.id, actor, member)
            command = f"{MEMBERS_COMMAND} {reference}"
            index = first + row + 1
            parts.extend((f"\n{index}. ", name, f" ({role}{own_label})"))
            if show_commands:
                parts.extend((" — ", code_span(command)))
                continue
            limit = min(layout.max_label_length, 30)
            label = f"{index}. {name}"
            if len(label) > limit:
                label = label[:limit - 1] + "…"
            actions.append(ReplyAction(label, command, row=row, purpose=ActionPurpose.SELECTION))

        links = [(label, destination) for label, destination in ((_PREVIOUS, page - 1), (_NEXT, page + 1))
                 if 1 <= destination <= total_pages]
        if show_commands:
            for label, destination in links:
                parts.extend(("\n", label, " — ", code_span(f"{MEMBERS_COMMAND} commands {destination}")))
            if operation:
                label, command = operation
                parts.extend(("\n", label, " — ", code_span(command)))
            parts.extend(("\nВ меню семьи — ", code_span("/family"), "."))
            return IncomingResponse(compose_content(*parts))

        row = len(visible)
        for label, destination in links:
            actions.append(ReplyAction(
                label, f"{MEMBERS_COMMAND} page {destination} {family.id}", row=row,
                purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("family-members", destination),
            ))
            if layout.max_columns == 1:
                row += 1
        if links and layout.max_columns > 1:
            row += 1
        if operation:
            label, command = operation
            actions.append(ReplyAction(label, f"{command} {family.id}", row=row))
            if layout.max_columns == 1:
                row += 1
        actions.append(ReplyAction(
            _BACK, "/family", row=row, purpose=ActionPurpose.NAVIGATION, destination=ScreenDestination("family-home"),
        ))
        return IncomingResponse(
            compose_content(*parts), suggested_actions=tuple(actions), view=_members_view(update=update),
        )

    def details(
        self, context: HandlerContext, address: ReplyAddress, family: Family,
        members: tuple[FamilyMember, ...], reference: str,
    ) -> IncomingResponse:
        """Показывает детали участника и доступные по роли действия.

        Args:
            context: Проверенный профиль с ревизией режима и лимитами кнопок.
            address: Исходный чат, к которому привязан выбор.
            family: Семья с подтверждённым членством исполнителя.
            members: Актуальный состав семьи.
            reference: Короткий код участника.

        Returns:
            Карточка участника либо подсказка открыть актуальный состав.
        """
        member = self.resolve(context, address, family.id, members, reference)
        if member is None:
            return IncomingResponse(
                compose_content("Выбор участника устарел. ", _UNAVAILABLE),
                suggested_actions=(ReplyAction(
                    "Участники", f"{MEMBERS_COMMAND} open {family.id}", purpose=ActionPurpose.NAVIGATION,
                    destination=ScreenDestination("family-members", 1),
                ),),
            )
        name = self.display_name(member)
        role = "Владелец семьи" if member.is_owner else "Участник семьи"
        title = f"{name}\n{role} «{family.name}»."
        available = ()
        if not member.is_owner and any(item.is_owner and item.app_user_id == context.app_user.id for item in members):
            available = ((_REMOVE, REMOVE_COMMAND), (_TRANSFER, TRANSFER_COMMAND))
        actions = tuple(ReplyAction(label, f"{command} {reference}") for label, command in available) + (ReplyAction(
            _BACK, f"{MEMBERS_COMMAND} open {family.id}", purpose=ActionPurpose.NAVIGATION,
            destination=ScreenDestination("family-members", 1),
        ),)
        layout = context.action_layout
        if (layout.selection_capacity() < len(actions)
                or layout.max_label_length < max(len(action.label) for action in actions)):
            parts = [title]
            for label, command in available:
                parts.extend(("\n", label, " — ", code_span(f"{command} {reference}")))
            parts.extend(("\nК составу — ", code_span(MEMBERS_COMMAND), "."))
            return IncomingResponse(compose_content(*parts))
        return IncomingResponse(text_content(title), suggested_actions=actions, view=_members_view())

    def display_name(self, member: FamilyMember) -> str:
        """Возвращает имя уже проверенного участника или нейтральную подпись."""
        return self._display_names((member.app_user_id,)).get(member.app_user_id) or "Участник"

    def resolve(
        self, context: HandlerContext, address: ReplyAddress, family_id: int,
        members: tuple[FamilyMember, ...], reference: str,
    ) -> FamilyMember | None:
        """Находит участника по ссылке в том же чате, режиме и составе.

        Args:
            context: Текущий проверенный профиль и ревизия режима.
            address: Исходный чат, где показан состав.
            family_id: Актуальный идентификатор семьи.
            members: Актуальный состав семьи.
            reference: Короткий код из команды выбора.

        Returns:
            Участник либо None для неизвестной или устаревшей ссылки.
        """
        if len(reference) != 16 or not reference.isascii():
            return None
        actor = next(member for member in members if member.app_user_id == context.app_user.id)
        return next((
            member for member in members
            if compare_digest(self._reference(context, address, family_id, actor, member), reference)
        ), None)

    def _reference(
        self, context: HandlerContext, address: ReplyAddress, family_id: int, actor: FamilyMember, member: FamilyMember,
    ) -> str:
        # Даты вступления отличают прежнее членство исполнителя и цели от текущего.
        scope = dumps((
            actor.app_user_id, actor.joined_at.isoformat(), address.channel, address.chat_id, context.mode_revision, family_id,
            member.app_user_id, member.joined_at.isoformat(),
        )).encode("utf-8")
        return urlsafe_b64encode(digest(self._key, scope, "sha256")[:12]).decode("ascii")
