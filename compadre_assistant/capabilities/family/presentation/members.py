"""Показывает текущий состав семьи и связывает выбор с исходным чатом."""

from base64 import urlsafe_b64encode
from collections.abc import Mapping
from hmac import compare_digest, digest
from json import dumps
from secrets import token_bytes
from typing import Protocol

from compadre_assistant.capabilities.family.domain.membership import Family, FamilyMember
from compadre_assistant.core.presentation.incoming.events import ReplyAddress
from compadre_assistant.core.presentation.incoming.ports import HandlerContext, IncomingResponse, ReplyAction
from compadre_assistant.core.presentation.message_content import code_span, compose_content


MEMBERS_COMMAND = "/family_members"
_PAGE_SIZE = 10  # Оставляет место для кнопок перехода в Telegram и VK.


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
        members: tuple[FamilyMember, ...], page: int,
    ) -> IncomingResponse:
        """Показывает имена и команды выбора на запрошенной странице.

        Args:
            context: Допущенный профиль и ревизия семейного режима.
            address: Исходный чат Telegram или VK.
            family: Семья, членство в которой проверено.
            members: Актуальный состав из семейного сценария.
            page: Номер страницы, начиная с единицы.

        Returns:
            Список участников с кнопками и полным ручным путём.
        """
        total_pages = max(1, (len(members) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(page, total_pages)
        first = (page - 1) * _PAGE_SIZE
        visible = members[first:first + _PAGE_SIZE]
        names = self._display_names(tuple(member.app_user_id for member in visible))
        parts = [f"Участники семьи «{family.name}»"]
        if total_pages > 1:
            parts.append(f" · {page}/{total_pages}")
        actions = []
        for member in visible:
            name = names.get(member.app_user_id) or "Участник"
            role = "владелец" if member.is_owner else "участник"
            own_label = ", ты" if member.app_user_id == context.app_user.id else ""
            reference = self._reference(context, address, family.id, member)
            command = f"{MEMBERS_COMMAND} {reference}"
            parts.extend(("\n", name, f" ({role}{own_label}) — ", code_span(command)))
            label = name if len(name) <= 24 else name[:23] + "…"
            suffix = " (ты)" if own_label else " (владелец)" if member.is_owner else ""
            actions.append(ReplyAction(label + suffix, command))
        for label, destination in (("Предыдущая страница", page - 1), ("Следующая страница", page + 1)):
            if 1 <= destination <= total_pages:
                command = f"{MEMBERS_COMMAND} page {destination}"
                parts.extend(("\n", label, " — ", code_span(command)))
                actions.append(ReplyAction(label, f"{command} {family.id}"))
        return IncomingResponse(compose_content(*parts), suggested_actions=tuple(actions))

    def details(
        self, context: HandlerContext, address: ReplyAddress, family: Family,
        members: tuple[FamilyMember, ...], reference: str,
    ) -> IncomingResponse:
        """Показывает детали участника, если ссылка относится к текущему составу."""
        member = self.resolve(context, address, family.id, members, reference)
        if member is None:
            return IncomingResponse(compose_content(
                "Выбор участника устарел. Открой ", code_span(MEMBERS_COMMAND), ", чтобы увидеть актуальный состав.",
            ), suggested_actions=(ReplyAction("Участники", f"{MEMBERS_COMMAND} open {family.id}"),))
        name = self._display_names((member.app_user_id,)).get(member.app_user_id) or "Участник"
        role = "Владелец семьи" if member.is_owner else "Участник семьи"
        return IncomingResponse(compose_content(
            name, "\n", f"{role} «{family.name}».\n", "Посмотреть состав: ", code_span(MEMBERS_COMMAND),
        ), suggested_actions=(ReplyAction("Участники", f"{MEMBERS_COMMAND} open {family.id}"),))

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
        return next((
            member for member in members
            if compare_digest(self._reference(context, address, family_id, member), reference)
        ), None)

    def _reference(self, context: HandlerContext, address: ReplyAddress, family_id: int, member: FamilyMember) -> str:
        # Дата вступления отличает прежнее членство того же профиля после повторного вступления.
        scope = dumps((
            context.app_user.id, address.channel, address.chat_id, context.mode_revision, family_id,
            member.app_user_id, member.joined_at.isoformat(),
        )).encode("utf-8")
        return urlsafe_b64encode(digest(self._key, scope, "sha256")[:12]).decode("ascii")
