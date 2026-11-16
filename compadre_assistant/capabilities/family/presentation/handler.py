"""Ведёт диалог о создании семьи и вступлении по приглашению."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC

from compadre_assistant.capabilities.family.domain.invitations import (
    InvalidInvitation, InvitationExpired, InvitationUnavailable, InvitationUsed, ManageInvitations,
)
from compadre_assistant.capabilities.family.domain.manage_family import ManageFamily
from compadre_assistant.capabilities.family.domain.membership import (
    AlreadyInFamily, Family, FamilyAccessEnded, InvalidFamilyName,
)
from compadre_assistant.capabilities.family.presentation.members import MEMBERS_COMMAND, MemberDisplayNames, MembersView
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode, CapabilityResult,
    CommandParameter, ConfirmationQueryResult, ContextualCommandQueryPort, HandlerContext, HelpSection,
    IncomingCapabilityPort, IncomingResponse, PendingConfirmationQueryPort, PendingInputContinuationPort,
    PendingInputOutcome, PendingInputResult, ReplyAction,
)
from compadre_assistant.core.presentation.message_content import code_span, compose_content, text_content


_CREATE = "/family_create"
_JOIN = "/family_join"
_INVITE = "/family_invite"


class FamilyCapability(
    IncomingCapabilityPort, ContextualCommandQueryPort, PendingInputContinuationPort, PendingConfirmationQueryPort,
):
    """Открывает семейный режим и обрабатывает команды зарегистрированного профиля."""

    descriptor = CapabilityDescriptor(
        "family",
        CapabilityCommand("/family", (), "открыть раздел «Семья»"),
        CapabilityHelp(sections=(HelpSection("Семья", (
            CapabilityCommand(_CREATE, (), "создать семью"),
            CapabilityCommand(_JOIN, (), "вступить по коду"),
            CapabilityCommand(_INVITE, (CommandParameter("action", "create", False),), "пригласить в семью"),
            CapabilityCommand(MEMBERS_COMMAND, (CommandParameter("member", "<member>", False),), "посмотреть участников"),
        )),)),
        enabled_by_default=True,
        display_name="«Семья»",
        private_chat_hint="Открой /family в личном чате с ботом.",
    )

    def __init__(
        self, open_scenarios: Callable[[], AbstractContextManager[tuple[ManageFamily, ManageInvitations]]],
        display_names: MemberDisplayNames,
    ) -> None:
        """Принимает фабрику предметных сценариев на одно событие."""
        self._open_scenarios = open_scenarios
        self._members_view = MembersView(display_names)

    def enter(self, context: HandlerContext, event: IncomingEvent, arguments: str) -> CapabilityResult:
        """Открывает начальный экран или показывает текущую семью.

        Args:
            context: Допущенный профиль и доступные порты ядра.
            event: Событие команды входа.
            arguments: Текст после команды, которая вызывается без аргументов.

        Returns:
            Текущий экран или подсказка без изменения режима при неверном синтаксисе.
        """
        if arguments:
            return CapabilityResult(
                IncomingResponse(compose_content("Используй ", code_span("/family"), " без аргументов.")),
                CapabilityMode.COMPLETE,
            )
        with self._open_scenarios() as (families, _):
            return CapabilityResult(self._home(families.find_for_member(context.app_user.id)))

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Выполняет команду семьи после проверки режима ядром.

        Args:
            context: Допущенный профиль и ожидание ввода в текущем чате.
            event: Текстовая команда или действие ответа из этого чата.

        Returns:
            Экран семьи, запрос ввода или результат выпуска приглашения.
        """
        parts = event.text.split(maxsplit=1)
        command = parts[0] if parts else ""
        arguments = parts[1] if len(parts) > 1 else ""
        app_user_id = context.app_user.id
        with self._open_scenarios() as (families, invitations):
            family = families.find_for_member(app_user_id)
            if command in (_CREATE, _JOIN):
                return CapabilityResult(self._begin_input(context, event, family, command, arguments))
            if command == _INVITE:
                return CapabilityResult(self._invite(invitations, app_user_id, family, arguments))
            if command == MEMBERS_COMMAND:
                return CapabilityResult(self._members(context, event.reply_address, families, family, arguments))
            return CapabilityResult(self._home(family))

    def visible_mode_commands(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int,
    ) -> frozenset[str]:
        """Показывает команды активного режима по текущему членству профиля."""
        with self._open_scenarios() as (families, _):
            if families.find_for_member(app_user.id) is None:
                return frozenset((_CREATE, _JOIN))
            return frozenset((_INVITE, MEMBERS_COMMAND))

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Сообщает ядру, что сейчас нет семейных действий для подтверждения."""
        return ConfirmationQueryResult("complete")

    def continue_input(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent,
    ) -> PendingInputResult:
        """Принимает название или код в чате, где начато ожидание.

        Args:
            context: Повторно допущенный профиль.
            pending: Действующее ожидание с исходным сроком.
            event: Сообщение с ожидаемым значением.

        Returns:
            Результат и признак того, может ли пользователь исправить ввод.
        """
        app_user_id = context.app_user.id
        command = pending.input_kind
        if command not in (_CREATE, _JOIN):
            return PendingInputResult(IncomingResponse(compose_content(
                "Открой ", code_span("/family"), " и начни действие заново.",
            )), PendingInputOutcome.COMPLETE)
        with self._open_scenarios() as (families, invitations):
            try:
                if command == _CREATE:
                    family = families.create(app_user_id, event.text)
                    prefix = "Семья создана."
                else:
                    family = invitations.join(app_user_id, event.text)
                    prefix = "Ты вступил в семью."
            except AlreadyInFamily:
                return PendingInputResult(self._home(
                    families.find_for_member(app_user_id), prefix="Твой профиль уже состоит в семье.",
                ), PendingInputOutcome.COMPLETE)
            except InvalidFamilyName:
                return self._retry_input("Напиши непустое название семьи.")
            except InvitationUsed:
                return self._retry_input("Код уже использован. Попроси новый у того, кто тебя пригласил.")
            except InvitationExpired:
                return self._retry_input("Код просрочен. Попроси новый у того, кто тебя пригласил.")
            except InvalidInvitation:
                return self._retry_input("Код недействителен. Проверь его или попроси новый у того, кто тебя пригласил.")
        return PendingInputResult(self._home(family, prefix=prefix), PendingInputOutcome.COMPLETE)

    @staticmethod
    def _begin_input(
        context: HandlerContext, event: IncomingEvent, family: Family | None, command: str, arguments: str,
    ) -> IncomingResponse:
        if family is not None:
            return FamilyCapability._home(family, prefix="Твой профиль уже состоит в семье.")
        if arguments:
            return IncomingResponse(compose_content(
                "Отправь ", code_span(command), " отдельным сообщением, чтобы начать ввод.",
            ))
        if context.pending_inputs is None:
            return IncomingResponse(text_content("Не удалось начать ввод. Попробуй ещё раз."))
        label = "название семьи" if command == _CREATE else "код приглашения"
        context.pending_inputs.begin(
            event.reply_address, FamilyCapability.descriptor.capability_id, command, app_user_id=context.app_user.id,
            required_mode=FamilyCapability.descriptor.capability_id, value_label=label, retry_command=command,
            cancel_only_menu=True,
        )
        return IncomingResponse(compose_content(
            f"Пришли {label} следующим сообщением. Ожидание действует 10 минут. Для отмены введи ",
            code_span("/cancel"), ".",
        ))

    @staticmethod
    def _retry_input(answer: str) -> PendingInputResult:
        return PendingInputResult(IncomingResponse(compose_content(
            answer, " Пришли значение снова или введи ", code_span("/cancel"), ".",
        )), PendingInputOutcome.RETRY)

    @staticmethod
    def _home(family: Family | None, *, prefix: str = "") -> IncomingResponse:
        introduction = prefix + "\n\n" if prefix else ""
        if family is None:
            return IncomingResponse(compose_content(
                introduction, "Семья\n\nСоздать семью: ", code_span(_CREATE),
                "\nВступить по коду: ", code_span(_JOIN),
            ), suggested_actions=(ReplyAction("Создать семью", _CREATE), ReplyAction("Вступить по коду", _JOIN)))
        return IncomingResponse(compose_content(
            introduction, f"Семья «{family.name}»\n\nУчастники: ", code_span(MEMBERS_COMMAND),
            "\nПригласить: ", code_span(_INVITE),
        ), suggested_actions=(
            ReplyAction("Участники", f"{MEMBERS_COMMAND} open {family.id}"),
            ReplyAction("Пригласить", f"{_INVITE} open {family.id}"),
        ))

    def _members(
        self, context: HandlerContext, address: ReplyAddress, families: ManageFamily,
        family: Family | None, arguments: str,
    ) -> IncomingResponse:
        if family is None:
            return self._family_access_ended()
        parts = arguments.split()
        page = 1
        reference = None
        if len(parts) == 2 and parts[0] == "open":
            if parts[1] != str(family.id):
                return self._family_access_ended()
        elif 2 <= len(parts) <= 3 and parts[0] == "page":
            if len(parts) == 3 and parts[2] != str(family.id):
                return self._family_access_ended()
            number = parts[1]
            if len(number) > 10 or not number.isascii() or not number.isdecimal() or int(number) < 1:
                return IncomingResponse(compose_content(
                    "Укажи номер страницы или отправь ", code_span(MEMBERS_COMMAND), ".",
                ))
            page = int(number)
        elif len(parts) == 1:
            reference = parts[0]
        elif parts:
            return IncomingResponse(compose_content("Посмотреть участников: ", code_span(MEMBERS_COMMAND), "."))
        try:
            members = families.list_members(context.app_user.id, family.id)
        except FamilyAccessEnded:
            return self._family_access_ended()
        if reference is None:
            return self._members_view.show(context, address, family, members, page)
        return self._members_view.details(context, address, family, members, reference)

    @staticmethod
    def _family_access_ended() -> IncomingResponse:
        return IncomingResponse(compose_content(
            "Доступ к семье завершён. Открой ", code_span("/family"), " для актуальных действий.",
        ))

    @staticmethod
    def _invite(
        invitations: ManageInvitations, app_user_id: int, family: Family | None, arguments: str,
    ) -> IncomingResponse:
        parts = arguments.split()
        action = parts[0] if parts else "open"
        if action not in ("open", "create") or len(parts) > 2:
            return IncomingResponse(compose_content("Чтобы пригласить участника, открой ", code_span(_INVITE), "."))
        # Кнопка привязана к семье, показанной в ответе. Ручная команда открывает текущую.
        if family is None or (len(parts) == 2 and parts[1] != str(family.id)):
            return FamilyCapability._family_access_ended()
        if action == "open":
            return IncomingResponse(compose_content(
                f"Приглашение в семью «{family.name}». Чтобы получить одноразовый код, отправь ",
                code_span(f"{_INVITE} create"), ".",
            ), suggested_actions=(ReplyAction("Создать код", f"{_INVITE} create {family.id}"),))
        try:
            issued = invitations.issue(app_user_id, family.id)
        except FamilyAccessEnded:
            return FamilyCapability._family_access_ended()
        except InvitationUnavailable:
            return IncomingResponse(compose_content(
                "Не удалось создать код. Попробуй ещё раз через ", code_span(_INVITE), ".",
            ))
        deadline = issued.expires_at.astimezone(UTC).strftime("%d.%m.%Y, %H:%M UTC")
        return IncomingResponse(compose_content(
            f"Приглашение в семью «{issued.family.name}»\n\nКод: ", code_span(issued.code),
            f"\nДействует до {deadline}. По коду можно вступить один раз.",
        ))
