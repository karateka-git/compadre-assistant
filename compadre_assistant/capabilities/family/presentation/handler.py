"""Ведёт семейные диалоги в активном режиме личного чата."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC

from compadre_assistant.capabilities.family.domain.invitations import (
    InvalidInvitation, InvitationExpired, InvitationUnavailable, InvitationUsed, ManageInvitations,
)
from compadre_assistant.capabilities.family.domain.manage_family import ManageFamily
from compadre_assistant.capabilities.family.domain.membership import (
    AlreadyInFamily, Family, FamilyAccessEnded, FamilyHasOtherMembers, FamilyMember, InvalidFamilyName,
    OtherMemberRequired, OwnerCannotLeave, OwnerRequired,
)
from compadre_assistant.capabilities.family.domain.shopping import (
    InvalidItemNumbers, InvalidShoppingNames, ItemNumbersExhausted, ManageShopping,
)
from compadre_assistant.capabilities.family.presentation.confirmations import MemberConfirmation, MemberConfirmations
from compadre_assistant.capabilities.family.presentation.members import (
    CLOSE_COMMAND, LEAVE_COMMAND, MEMBERS_COMMAND, REMOVE_COMMAND, TRANSFER_COMMAND, MemberDisplayNames, MembersView,
)
from compadre_assistant.capabilities.family.presentation.shopping import (
    ADD_COMMAND, BUY_COMMAND, CLEAR_BOUGHT_COMMAND, DELETE_COMMAND, SHOPPING_COMMAND,
    ShoppingSection, ShoppingSections, ShoppingSelection, needs_notice_pages, parse_item_number, shopping_list,
    shopping_notices, shopping_report,
)
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
_MEMBER_ACTIONS = (LEAVE_COMMAND, REMOVE_COMMAND, TRANSFER_COMMAND, CLOSE_COMMAND)
_SHOPPING_ACTIONS = (ADD_COMMAND, BUY_COMMAND, DELETE_COMMAND, CLEAR_BOUGHT_COMMAND)


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
            CapabilityCommand(LEAVE_COMMAND, (), "выйти из семьи"),
            CapabilityCommand(REMOVE_COMMAND, (CommandParameter("member", "<member>", False),), "исключить участника"),
            CapabilityCommand(TRANSFER_COMMAND, (CommandParameter("member", "<member>", False),), "передать роль владельца"),
            CapabilityCommand(CLOSE_COMMAND, (), "закрыть семью"),
            CapabilityCommand(SHOPPING_COMMAND, (CommandParameter("view", "<view>", False),), "открыть покупки"),
        )), HelpSection("Покупки", (
            CapabilityCommand(ADD_COMMAND, (CommandParameter("items", "<items>", False),), "добавить покупки"),
            CapabilityCommand(BUY_COMMAND, (CommandParameter("numbers", "<numbers>", False),), "отметить купленными"),
            CapabilityCommand(DELETE_COMMAND, (CommandParameter("numbers", "<numbers>", False),), "удалить из списка"),
            CapabilityCommand(CLEAR_BOUGHT_COMMAND, (), "очистить купленное"),
        )))),
        enabled_by_default=True,
        display_name="«Семья»",
        private_chat_hint="Открой /family в личном чате с ботом.",
    )

    def __init__(
        self, open_scenarios: Callable[[], AbstractContextManager[tuple[ManageFamily, ManageInvitations, ManageShopping]]],
        display_names: MemberDisplayNames,
    ) -> None:
        """Принимает фабрику предметных сценариев на одно событие."""
        self._open_scenarios = open_scenarios
        self._members_view = MembersView(display_names)
        self._confirmations = MemberConfirmations()
        self._shopping_sections = ShoppingSections()

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
        with self._open_scenarios() as (families, _, _):
            return CapabilityResult(self._home(families.find_for_member(context.app_user.id)))

    def handle(self, context: HandlerContext, event: IncomingEvent) -> CapabilityResult:
        """Выполняет команду семьи после проверки режима ядром.

        Args:
            context: Допущенный профиль и ожидание ввода в текущем чате.
            event: Текстовая команда или действие ответа из этого чата.

        Returns:
            Экран семьи, запрос ввода, приглашение или результат действия с участниками.
        """
        parts = event.text.split(maxsplit=1)
        command = parts[0] if parts else ""
        arguments = parts[1] if len(parts) > 1 else ""
        app_user_id = context.app_user.id
        with self._open_scenarios() as (families, invitations, shopping):
            family = families.find_for_member(app_user_id)
            if command == SHOPPING_COMMAND or command in _SHOPPING_ACTIONS:
                return CapabilityResult(self._shopping(context, event, families, family, shopping, command, arguments))
            self._shopping_sections.close(app_user_id, event.reply_address)
            if command in (_CREATE, _JOIN):
                return CapabilityResult(self._begin_input(context, event, family, command, arguments))
            if command == _INVITE:
                return CapabilityResult(self._invite(invitations, app_user_id, family, arguments))
            if command == MEMBERS_COMMAND:
                return CapabilityResult(self._members(context, event.reply_address, families, family, arguments))
            if command in _MEMBER_ACTIONS:
                return CapabilityResult(self._begin_member_action(
                    context, event.reply_address, families, family, command, arguments,
                ))
            if command == "/confirm" and arguments and arguments.split(maxsplit=1)[0] == "family":
                return CapabilityResult(self._confirm_member_action(
                    context, event.reply_address, families, family, " ".join(event.text.split()),
                ))
            return CapabilityResult(self._home(family))

    def visible_mode_commands(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int,
    ) -> frozenset[str]:
        """Показывает команды активного режима по текущему членству профиля."""
        with self._open_scenarios() as (families, _, _):
            family = families.find_for_member(app_user.id)
            if family is None:
                return frozenset((_CREATE, _JOIN))
            try:
                members = families.list_members(app_user.id, family.id)
            except FamilyAccessEnded:
                return frozenset((_CREATE, _JOIN))
            actor = next(member for member in members if member.app_user_id == app_user.id)
            sections = self._member_commands(actor, len(members))
            current = self._shopping_sections.current(app_user.id, address, mode_revision)
            if current is not None and current.matches(family.id, actor.joined_at):
                return frozenset((SHOPPING_COMMAND, ADD_COMMAND, BUY_COMMAND, DELETE_COMMAND))
            return sections | {SHOPPING_COMMAND}

    @staticmethod
    def _member_commands(actor: FamilyMember, member_count: int) -> frozenset[str]:
        if not actor.is_owner:
            return frozenset((_INVITE, MEMBERS_COMMAND, LEAVE_COMMAND))
        if member_count == 1:
            return frozenset((_INVITE, MEMBERS_COMMAND, CLOSE_COMMAND))
        return frozenset((_INVITE, MEMBERS_COMMAND, REMOVE_COMMAND, TRANSFER_COMMAND))

    def list_pending_confirmations(
        self, context: HandlerContext, origin: ReplyAddress,
    ) -> ConfirmationQueryResult:
        """Возвращает адресное подтверждение, пока его исходные условия действуют."""
        pending = self._confirmations.current(context, origin)
        if pending is None:
            return ConfirmationQueryResult("complete")
        with self._open_scenarios() as (families, _, _):
            if not self._member_action_is_current(families, pending):
                return ConfirmationQueryResult("complete")
        return ConfirmationQueryResult("complete", (pending.confirmation,))

    @staticmethod
    def _member_action_is_current(families: ManageFamily, pending: MemberConfirmation) -> bool:
        # Снимок для меню и отмены. При выполнении записи репозиторий проверит условия в транзакции.
        try:
            members = families.list_members(pending.actor.app_user_id, pending.family_id)
        except FamilyAccessEnded:
            return False
        return (
            pending.actor in members
            and (pending.target is None or pending.target in members)
            and (pending.confirmation.operation != "close" or len(members) == 1)
        )

    def _begin_member_action(
        self, context: HandlerContext, address: ReplyAddress, families: ManageFamily,
        family: Family | None, command: str, arguments: str,
    ) -> IncomingResponse:
        if family is None or (command in (LEAVE_COMMAND, CLOSE_COMMAND) and arguments and arguments != str(family.id)):
            return self._family_access_ended()
        try:
            members = families.list_members(context.app_user.id, family.id)
        except FamilyAccessEnded:
            return self._family_access_ended()
        actor = next(member for member in members if member.app_user_id == context.app_user.id)
        target = None
        if command == LEAVE_COMMAND:
            if actor.is_owner:
                return self._owner_cannot_leave(len(members) > 1)
            question, label = f"Выйти из семьи «{family.name}»? Ты потеряешь доступ к общим покупкам.", "Выйти из семьи"
        elif not actor.is_owner:
            return IncomingResponse(text_content("Это действие доступно только владельцу семьи."))
        elif command == CLOSE_COMMAND:
            if len(members) > 1:
                return self._cannot_close()
            question = f"Закрыть семью «{family.name}»? Общий список покупок удалится, а коды перестанут действовать."
            label = "Закрыть семью"
        else:
            if not arguments:
                return self._members_view.show(context, address, family, members, 1)
            target = self._members_view.resolve(context, address, family.id, members, arguments)
            if target is None or target.app_user_id == actor.app_user_id:
                return self._target_changed()
            name = self._members_view.display_name(target)
            if command == REMOVE_COMMAND:
                question, label = f"Исключить участника «{name}» из семьи «{family.name}»?", "Исключить из семьи"
            else:
                question, label = f"Передать роль владельца семьи «{family.name}» участнику «{name}»?", "Передать роль"
        pending = self._confirmations.begin(
            context, address, command.removeprefix("/family_"), family.id, actor, target, label,
        )
        approve, cancel = pending.actions
        return IncomingResponse(compose_content(
            question, "\n\nПодтверждение действует 10 минут.\nПодтвердить: ", code_span(approve.command),
            "\nОтменить: ", code_span(cancel.command),
        ), actions=pending.actions)

    def _confirm_member_action(
        self, context: HandlerContext, address: ReplyAddress, families: ManageFamily,
        family: Family | None, command: str,
    ) -> IncomingResponse:
        taken = self._confirmations.take(context, address, command)
        if taken is None:
            return self._confirmation_unavailable()
        pending, approved = taken
        if family is None or family.id != pending.family_id:
            return self._family_access_ended()
        if not approved:
            if not self._member_action_is_current(families, pending):
                return self._confirmation_unavailable()
            return self._home(family, prefix="Действие отменено.")
        actor_id = pending.actor.app_user_id
        actor_joined_at = pending.actor.joined_at
        operation = pending.confirmation.operation
        try:
            if operation == "leave":
                families.leave(actor_id, pending.family_id, actor_joined_at=actor_joined_at)
                return self._home(None, prefix="Ты вышел из семьи.")
            if operation == "close":
                families.close(actor_id, pending.family_id, actor_joined_at=actor_joined_at)
                return self._home(None, prefix="Семья закрыта.")
            target = pending.target
            if target is None:
                raise ValueError("Для этого подтверждения нужен другой участник")
            if operation == "remove":
                families.remove_member(
                    actor_id, pending.family_id, target.app_user_id,
                    actor_joined_at=actor_joined_at, target_joined_at=target.joined_at,
                )
                return self._home(family, prefix="Участник исключён из семьи.")
            families.transfer_ownership(
                actor_id, pending.family_id, target.app_user_id,
                actor_joined_at=actor_joined_at, target_joined_at=target.joined_at,
            )
            return self._home(family, prefix="Роль владельца передана.")
        except FamilyAccessEnded:
            return self._family_access_ended()
        except OwnerCannotLeave as error:
            return self._owner_cannot_leave(error.has_other_members)
        except FamilyHasOtherMembers:
            return self._cannot_close()
        except OwnerRequired:
            return IncomingResponse(text_content("Это действие доступно только владельцу семьи."))
        except OtherMemberRequired:
            return self._target_changed()

    @staticmethod
    def _owner_cannot_leave(has_other_members: bool) -> IncomingResponse:
        if has_other_members:
            return IncomingResponse(compose_content(
                "Перед выходом передай роль владельца. Выбери участника через ", code_span(MEMBERS_COMMAND), ".",
            ))
        return IncomingResponse(compose_content(
            "Ты единственный участник. Чтобы закрыть семью, отправь ", code_span(CLOSE_COMMAND), ".",
        ))

    @staticmethod
    def _cannot_close() -> IncomingResponse:
        return IncomingResponse(compose_content(
            "Закрыть семью может единственный участник. Открой ", code_span(MEMBERS_COMMAND), ", чтобы увидеть состав.",
        ))

    @staticmethod
    def _target_changed() -> IncomingResponse:
        return IncomingResponse(compose_content(
            "Выбери другого действующего участника через ", code_span(MEMBERS_COMMAND), ".",
        ))

    @staticmethod
    def _confirmation_unavailable() -> IncomingResponse:
        return IncomingResponse(compose_content(
            "Подтверждение недоступно. Открой ", code_span("/family"), " и начни действие заново.",
        ))

    def continue_input(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent,
    ) -> PendingInputResult:
        """Принимает название, код или покупки в чате, где начато ожидание.

        Args:
            context: Повторно допущенный профиль.
            pending: Действующее ожидание с исходным сроком.
            event: Сообщение с ожидаемым значением.

        Returns:
            Результат и признак того, может ли пользователь исправить ввод.
        """
        app_user_id = context.app_user.id
        command = pending.input_kind
        if command == ADD_COMMAND:
            return self._continue_shopping_add(context, pending, event)
        if command not in (_CREATE, _JOIN):
            return PendingInputResult(IncomingResponse(compose_content(
                "Открой ", code_span("/family"), " и начни действие заново.",
            )), PendingInputOutcome.COMPLETE)
        with self._open_scenarios() as (families, invitations, _):
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
            introduction, f"Семья «{family.name}»\n\nПокупки: ", code_span(SHOPPING_COMMAND),
            "\nУчастники: ", code_span(MEMBERS_COMMAND),
            "\nПригласить: ", code_span(_INVITE),
        ), suggested_actions=(
            ReplyAction("Покупки", f"{SHOPPING_COMMAND} open {family.id}"),
            ReplyAction("Участники", f"{MEMBERS_COMMAND} open {family.id}"),
            ReplyAction("Пригласить", f"{_INVITE} open {family.id}"),
        ))

    def _shopping(
        self, context: HandlerContext, event: IncomingEvent, families: ManageFamily,
        family: Family | None, shopping: ManageShopping, command: str, arguments: str,
    ) -> IncomingResponse:
        app_user_id = context.app_user.id
        address = event.reply_address
        revision = context.mode_revision
        parts = arguments.split()
        if family is None or revision is None:
            return self._family_access_ended()
        if command == SHOPPING_COMMAND and len(parts) == 2 and parts[0] == "open" and parts[1] != str(family.id):
            return self._family_access_ended()
        try:
            actor = next(member for member in families.list_members(app_user_id, family.id)
                         if member.app_user_id == app_user_id)
            if command == SHOPPING_COMMAND and (not parts or parts == ["open", str(family.id)]):
                section = ShoppingSection(revision, family.id, actor.joined_at)
                items = shopping.list_items(app_user_id, family.id, actor_joined_at=section.joined_at)
                self._shopping_sections.open(app_user_id, address, section)
                return shopping_list(items, section)
            current = self._shopping_sections.current(app_user_id, address, revision)
            if current is not None and not current.matches(family.id, actor.joined_at):
                return self._family_access_ended()
            if current is None:
                return self._open_shopping_hint()
            if command in _SHOPPING_ACTIONS:
                return self._shopping_action(context, event, shopping, current, command, arguments)
            if len(parts) >= 2 and parts[0] == "click":
                if event.callback_message_id is None:
                    return self._open_shopping_hint()
                if parts[1] != current.action_key:
                    return IncomingResponse(compose_content(
                        "Эта кнопка устарела. Открой список командой ", code_span(SHOPPING_COMMAND), ".",
                    ))
                if parts[2:] == ["home"]:
                    self._shopping_sections.close(app_user_id, address)
                    return self._home(family)
                return self._shopping_click(shopping, app_user_id, current, parts[2:])
            if parts == ["home"]:
                self._shopping_sections.close(app_user_id, address)
                return self._home(family)
            if parts == ["back"]:
                items = shopping.list_items(app_user_id, family.id, actor_joined_at=current.joined_at)
                return shopping_list(items, current, update=True)
            if len(parts) != 2 or parts[0] not in ("page", "item", "result", *ShoppingSelection):
                return self._open_shopping_hint()
            number = parse_item_number(parts[1])
            if number is None:
                return self._open_shopping_hint()
            items = shopping.list_items(app_user_id, family.id, actor_joined_at=current.joined_at)
        except FamilyAccessEnded:
            return self._family_access_ended()
        if parts[0] == "result":
            if not current.report:
                return IncomingResponse(text_content("В этом разделе нет подробного результата операции."))
            return shopping_report(current.report, number)
        if parts[0] == "page":
            return shopping_list(items, current, number)
        if parts[0] in tuple(ShoppingSelection):
            return shopping_list(items, current, number, update=True, selection=ShoppingSelection(parts[0]))
        item = next((item for item in items if item.number == number), None)
        if item is None:
            return IncomingResponse(text_content(f"№{number} нет в списке."))
        return IncomingResponse(text_content(f"{item.number}. {item.name}"))

    def _shopping_click(
        self, shopping: ManageShopping, app_user_id: int, section: ShoppingSection, action: list[str],
    ) -> IncomingResponse:
        # У ядра уже есть адрес и подтверждённое поколение сообщения. Ключ раздела
        # дополнительно исключает нажатия до повторного вступления или открытия раздела.
        if len(action) < 2 or (page := parse_item_number(action[1])) is None:
            return self._open_shopping_hint()
        family_id = section.family_id
        joined_at = section.joined_at
        match action:
            case ["clear", _]:
                items = shopping.clear_bought(app_user_id, family_id, actor_joined_at=joined_at)
                return shopping_list(items, section, page, update=True)
            case ["pick", _, "buy" | "delete" as view, value] if (number := parse_item_number(value)) is not None:
                selection = ShoppingSelection(view)
                change = shopping.buy if selection == ShoppingSelection.BUY else shopping.delete
                result = change(app_user_id, family_id, (number,), actor_joined_at=joined_at)
                return shopping_list(
                    result.items, section, page, update=True, selection=selection,
                    notices=shopping_notices(result.changes),
                )
            case ["show", _, "page" | "buy" | "delete" as view]:
                selection = None if view == "page" else ShoppingSelection(view)
                items = shopping.list_items(app_user_id, family_id, actor_joined_at=joined_at)
                return shopping_list(items, section, page, update=True, selection=selection)
            case _:
                return self._open_shopping_hint()

    def _shopping_action(
        self, context: HandlerContext, event: IncomingEvent, shopping: ManageShopping,
        section: ShoppingSection, command: str, arguments: str,
    ) -> IncomingResponse:
        app_user_id = context.app_user.id
        family_id = section.family_id
        address = event.reply_address
        if command == ADD_COMMAND:
            if not arguments:
                return self._begin_shopping_add(context, address, section)
            try:
                items = shopping.add(app_user_id, family_id, arguments, actor_joined_at=section.joined_at)
                return shopping_list(items, section)
            except ItemNumbersExhausted:
                return self._shopping_numbers_exhausted()
        if command == CLEAR_BOUGHT_COMMAND:
            if arguments:
                return IncomingResponse(compose_content("Отправь ", code_span(command), " без аргументов."))
            items = shopping.clear_bought(app_user_id, family_id, actor_joined_at=section.joined_at)
            return shopping_list(items, section)

        if not arguments:
            selection = ShoppingSelection.BUY if command == BUY_COMMAND else ShoppingSelection.DELETE
            items = shopping.list_items(app_user_id, family_id, actor_joined_at=section.joined_at)
            return shopping_list(items, section, update=True, selection=selection)

        try:
            numbers = tuple(parse_item_number(value) or 0 for value in arguments.split())
            change = shopping.buy if command == BUY_COMMAND else shopping.delete
            result = change(app_user_id, family_id, numbers, actor_joined_at=section.joined_at)
        except InvalidItemNumbers:
            return IncomingResponse(compose_content(
                "Укажи один или несколько положительных номеров через пробел. Например, ",
                code_span(f"{command} 1 2"), ".",
            ))
        notices = shopping_notices(result.changes)
        if needs_notice_pages(notices):
            self._shopping_sections.replace(app_user_id, address, section, replace(section, report=notices))
        return shopping_list(result.items, section, notices=notices)

    def _begin_shopping_add(
        self, context: HandlerContext, address: ReplyAddress, section: ShoppingSection,
    ) -> IncomingResponse:
        if context.pending_inputs is None:
            return IncomingResponse(text_content("Не удалось начать ввод. Попробуй ещё раз."))
        app_user_id = context.app_user.id
        with context.pending_inputs.lock(address):
            current = self._shopping_sections.current(app_user_id, address, section.revision)
            if current is None or current != section:
                return self._family_access_ended()
            pending = context.pending_inputs.begin(
                address, self.descriptor.capability_id, ADD_COMMAND, app_user_id=app_user_id,
                required_mode=self.descriptor.capability_id, value_label="список покупок", retry_command=ADD_COMMAND,
                cancel_only_menu=True,
            )
            self._shopping_sections.replace(
                app_user_id, address, current, replace(current, add_request_id=pending.request_id),
            )
        return IncomingResponse(compose_content(
            "Напиши, что купить. Можно отправить несколько покупок одним сообщением, по одной на строку. "
            "Ожидание действует 10 минут. Для отмены введи ", code_span("/cancel"), ".",
        ))

    def _continue_shopping_add(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent,
    ) -> PendingInputResult:
        revision = context.mode_revision
        app_user_id = context.app_user.id
        section = None if revision is None else self._shopping_sections.current(app_user_id, event.reply_address, revision)
        if section is None or section.add_request_id != pending.request_id:
            return PendingInputResult(self._family_access_ended(), PendingInputOutcome.COMPLETE)
        with self._open_scenarios() as (_, _, shopping):
            try:
                items = shopping.add(app_user_id, section.family_id, event.text, actor_joined_at=section.joined_at)
            except FamilyAccessEnded:
                return PendingInputResult(self._family_access_ended(), PendingInputOutcome.COMPLETE)
            except InvalidShoppingNames:
                return self._retry_input("Напиши хотя бы одну покупку.")
            except ItemNumbersExhausted:
                return PendingInputResult(self._shopping_numbers_exhausted(), PendingInputOutcome.COMPLETE)
        return PendingInputResult(shopping_list(items, section), PendingInputOutcome.COMPLETE)

    @staticmethod
    def _shopping_numbers_exhausted() -> IncomingResponse:
        return IncomingResponse(text_content("Новые покупки добавить не удалось, номера списка закончились."))

    @staticmethod
    def _open_shopping_hint() -> IncomingResponse:
        return IncomingResponse(compose_content("Открой список командой ", code_span(SHOPPING_COMMAND), "."))

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
