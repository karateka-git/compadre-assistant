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
    InvalidShoppingNames, ItemNumbersExhausted, ManageShopping, ShoppingItem,
)
from compadre_assistant.capabilities.family.presentation.confirmations import MemberConfirmation, MemberConfirmations
from compadre_assistant.capabilities.family.presentation.members import (
    CLOSE_COMMAND, LEAVE_COMMAND, MEMBERS_COMMAND, REMOVE_COMMAND, TRANSFER_COMMAND, MemberDisplayNames, MembersView,
)
from compadre_assistant.capabilities.family.presentation.shopping import (
    ADD_COMMAND, BUY_COMMAND, CLEAR_BOUGHT_COMMAND, DELETE_COMMAND, SHOPPING_COMMAND,
    ShoppingChoice, ShoppingSection, ShoppingSections, ShoppingSelection, parse_item_number, shopping_list,
    shopping_notice, shopping_page,
)
from compadre_assistant.core.domain.users.entities import AppUser
from compadre_assistant.core.presentation.incoming.events import IncomingEvent, ReplyAddress
from compadre_assistant.core.presentation.incoming.pending_input import PendingInput
from compadre_assistant.core.presentation.incoming.ports import (
    CapabilityCommand, CapabilityDescriptor, CapabilityHelp, CapabilityMode, CapabilityResult,
    CommandParameter, CommandSuggestion, ConfirmationQueryResult, ContextualCommandMenuPort,
    ContextualCommandQueryPort, HandlerContext, HelpSection,
    IncomingCapabilityPort, IncomingResponse, PendingConfirmationQueryPort, PendingInputContinuationPort,
    PendingInputOutcome, PendingInputResult, ReplyAction, ReplyActionRecoveryPort, ResponseView,
)
from compadre_assistant.core.presentation.message_content import code_span, compose_content, text_content


_CREATE = "/family_create"
_JOIN = "/family_join"
_INVITE = "/family_invite"
_MEMBER_ACTIONS = (LEAVE_COMMAND, REMOVE_COMMAND, TRANSFER_COMMAND, CLOSE_COMMAND)
_SHOPPING_ACTIONS = (ADD_COMMAND, BUY_COMMAND, DELETE_COMMAND, CLEAR_BOUGHT_COMMAND)
_VK_NO_FAMILY_MENU = (
    CommandSuggestion(_CREATE, "создать семью", "Создать семью"),
    CommandSuggestion(_JOIN, "вступить по коду приглашения", "Вступить по коду"),
)
_VK_FAMILY_MENU = (
    CommandSuggestion(SHOPPING_COMMAND, "открыть покупки", "Покупки"),
    CommandSuggestion(MEMBERS_COMMAND, "посмотреть участников", "Участники"),
    CommandSuggestion(_INVITE, "пригласить в семью", "Пригласить"),
    CommandSuggestion("/help", "показать справку о покупках", "Помощь"),
    CommandSuggestion("/exit", "выйти из режима семьи", "Выйти из режима"),
)
_VK_SHOPPING_MENU = (
    CommandSuggestion(ADD_COMMAND, "добавить покупки", "Добавить"),
    CommandSuggestion(BUY_COMMAND, "отметить купленными", "Купить"),
    CommandSuggestion(DELETE_COMMAND, "удалить из списка", "Удалить"),
)
_VK_CLEAR_BOUGHT_MENU = CommandSuggestion(CLEAR_BOUGHT_COMMAND, "очистить купленное", "Очистить купленное")
_VK_SHOPPING_BACK = CommandSuggestion(f"{SHOPPING_COMMAND} up", "вернуться на предыдущий экран", "Назад")


class FamilyCapability(
    IncomingCapabilityPort, ContextualCommandQueryPort, ContextualCommandMenuPort,
    PendingInputContinuationPort, PendingConfirmationQueryPort, ReplyActionRecoveryPort,
):
    """Открывает семейный режим и обрабатывает команды зарегистрированного профиля."""

    descriptor = CapabilityDescriptor(
        "family",
        CapabilityCommand("/family", (), "открыть раздел «Семья»"),
        CapabilityHelp(sections=(HelpSection("Семья", (
            CapabilityCommand(_CREATE, (), "создать семью"),
            CapabilityCommand(_JOIN, (), "вступить по коду"),
            CapabilityCommand(_INVITE, (CommandParameter("action", "create", False),), "пригласить в семью"),
            CapabilityCommand(SHOPPING_COMMAND, (), "открыть покупки"),
        )), HelpSection("Участники", (
            CapabilityCommand(MEMBERS_COMMAND, (CommandParameter("member", "<member>", False),), "посмотреть участников"),
            CapabilityCommand(LEAVE_COMMAND, (), "выйти из семьи"),
            CapabilityCommand(REMOVE_COMMAND, (CommandParameter("member", "<member>", False),), "исключить участника"),
            CapabilityCommand(TRANSFER_COMMAND, (CommandParameter("member", "<member>", False),), "передать роль владельца"),
            CapabilityCommand(CLOSE_COMMAND, (), "закрыть семью"),
        ), instructions=(
            compose_content(code_span(f"{MEMBERS_COMMAND} commands [<page>]"),
                            " — показать коды участников для ручных команд"),
        )), HelpSection(
            "Команды после открытия списка «Покупки»",
            (
                CapabilityCommand(
                    ADD_COMMAND, (CommandParameter("items", "[<items>]", False),),
                    "добавить покупки. Без аргументов бот попросит прислать названия, по одному на строку",
                ),
                CapabilityCommand(BUY_COMMAND, (), "выбрать покупку кнопкой или номером и отметить её купленной"),
                CapabilityCommand(DELETE_COMMAND, (), "выбрать покупку кнопкой или номером и удалить её из «Нужно купить»"),
                CapabilityCommand(CLEAR_BOUGHT_COMMAND, (), "удалить все купленные позиции"),
            ),
            instructions=(
                compose_content(code_span(f"{SHOPPING_COMMAND} page <number>"), " — открыть страницу списка"),
                compose_content(code_span(f"{SHOPPING_COMMAND} buy <page>"), " — открыть страницу выбора, что куплено"),
                compose_content(code_span(f"{SHOPPING_COMMAND} delete <page>"), " — открыть страницу выбора для удаления"),
                compose_content(code_span(f"{SHOPPING_COMMAND} back"), " — вернуться из выбора товара к списку"),
                compose_content(code_span(f"{SHOPPING_COMMAND} home"), " — вернуться из списка в меню семьи"),
            ),
            menu_help_only=True,
        ))),
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
            return CapabilityResult(self._home(families.find_for_member(context.app_user.id), channel=event.channel))

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
                return CapabilityResult(replace(
                    self._begin_input(context, event, family, command, arguments), close_managed_views=True,
                ))
            if command == _INVITE:
                return CapabilityResult(replace(
                    self._invite(invitations, app_user_id, family, arguments),
                    command_menu_delivery="prefer", close_managed_views=True,
                ))
            if command == MEMBERS_COMMAND:
                return CapabilityResult(replace(
                    self._members(context, event.reply_address, families, family, arguments),
                    command_menu_delivery="prefer", close_managed_views=True,
                ))
            if command in _MEMBER_ACTIONS:
                response = self._begin_member_action(
                    context, event.reply_address, families, family, command, arguments,
                )
                return CapabilityResult(replace(response, command_menu_delivery="prefer", close_managed_views=True))
            if command == "/confirm" and arguments and arguments.split(maxsplit=1)[0] == "family":
                return CapabilityResult(replace(self._confirm_member_action(
                    context, event.reply_address, families, family, " ".join(event.text.split()),
                ), close_managed_views=True))
            return CapabilityResult(self._home(family, channel=event.channel))

    def visible_mode_commands(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int,
    ) -> frozenset[str]:
        """Отбирает разделы справки по членству профиля и открытому экрану."""
        with self._open_scenarios() as (families, _, _):
            family = families.find_for_member(app_user.id)
            if family is None:
                return frozenset((_CREATE, _JOIN))
            try:
                members = families.list_members(app_user.id, family.id)
            except FamilyAccessEnded:
                return frozenset((_CREATE, _JOIN))
            actor = next(member for member in members if member.app_user_id == app_user.id)
            current = self._shopping_sections.current(app_user.id, address, mode_revision)
            if current is not None and current.matches(family.id, actor.joined_at):
                return frozenset((SHOPPING_COMMAND, *_SHOPPING_ACTIONS))
            sections = self._member_commands(actor, len(members)) | {SHOPPING_COMMAND}
            if address.channel == "vk":
                # Кнопка «Помощь» в меню VK объясняет, что сначала нужно открыть покупки.
                return sections | set(_SHOPPING_ACTIONS)
            return sections

    def contextual_command_menu(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int, pending: PendingInput | None,
    ) -> tuple[CommandSuggestion, ...] | None:
        """Проецирует подписанные кнопки VK для текущего раздела и ввода."""
        if address.channel != "vk":
            return None
        if pending is not None:
            if pending.owner_id != self.descriptor.capability_id:
                return None
            if pending.cancel_only_menu:
                return (CommandSuggestion("/cancel", "отменить ввод", "Отмена"),)
        app_user_id = app_user.id
        with self._open_scenarios() as (families, _, _):
            family = families.find_for_member(app_user_id)
            section = self._shopping_sections.current(app_user_id, address, mode_revision)
            if family is None:
                return _VK_NO_FAMILY_MENU
            if section is None:
                return _VK_FAMILY_MENU
            try:
                actor = next(member for member in families.list_members(app_user_id, family.id)
                             if member.app_user_id == app_user_id)
            except FamilyAccessEnded:
                return _VK_NO_FAMILY_MENU
            if not section.matches(family.id, actor.joined_at):
                return _VK_FAMILY_MENU
        actions = list(_VK_SHOPPING_MENU)
        if section.bought_visible:
            actions.append(_VK_CLEAR_BOUGHT_MENU)
        for page, label in ((section.ordinary_page - 1, "Предыдущая страница"),
                            (section.ordinary_page + 1, "Следующая страница")):
            if 1 <= page <= section.ordinary_last_page:
                actions.append(CommandSuggestion(f"{SHOPPING_COMMAND} page {page}", label, label))
        return (*actions, _VK_SHOPPING_BACK)

    def reply_action_recovery_command(
        self, app_user: AppUser, address: ReplyAddress, mode_revision: int | None,
    ) -> str:
        """Предлагает покупки только для ещё открытого и доступного списка."""
        section = (self._shopping_sections.current(app_user.id, address, mode_revision)
                   if mode_revision is not None else None)
        if section is None:
            return "/family"
        with self._open_scenarios() as (families, _, _):
            try:
                members = families.list_members(app_user.id, section.family_id)
            except FamilyAccessEnded:
                return "/family"
        return (SHOPPING_COMMAND if any(
            member.app_user_id == app_user.id and section.joined_at == member.joined_at
            for member in members
        ) else "/family")

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
        return IncomingResponse(text_content(
            f"{question}\n\nПодтверждение действует 10 минут.",
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
            return self._home(family, channel=address.channel, prefix="Действие отменено.")
        actor_id = pending.actor.app_user_id
        actor_joined_at = pending.actor.joined_at
        operation = pending.confirmation.operation
        try:
            if operation == "leave":
                families.leave(actor_id, pending.family_id, actor_joined_at=actor_joined_at)
                return self._home(None, channel=address.channel, prefix="Ты вышел из семьи.")
            if operation == "close":
                families.close(actor_id, pending.family_id, actor_joined_at=actor_joined_at)
                return self._home(None, channel=address.channel, prefix="Семья закрыта.")
            target = pending.target
            if target is None:
                raise ValueError("Для этого подтверждения нужен другой участник")
            if operation == "remove":
                families.remove_member(
                    actor_id, pending.family_id, target.app_user_id,
                    actor_joined_at=actor_joined_at, target_joined_at=target.joined_at,
                )
                return self._home(family, channel=address.channel, prefix="Участник исключён из семьи.")
            families.transfer_ownership(
                actor_id, pending.family_id, target.app_user_id,
                actor_joined_at=actor_joined_at, target_joined_at=target.joined_at,
            )
            return self._home(family, channel=address.channel, prefix="Роль владельца передана.")
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
        """Принимает название, код, список покупок или номер из показанного списка.

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
        if command in (BUY_COMMAND, DELETE_COMMAND):
            return self._continue_shopping_choice(context, pending, event)
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
                    families.find_for_member(app_user_id), channel=event.channel,
                    prefix="Твой профиль уже состоит в семье.",
                ), PendingInputOutcome.COMPLETE)
            except InvalidFamilyName:
                return self._retry_input("Напиши непустое название семьи.")
            except InvitationUsed:
                return self._retry_input("Код уже использован. Попроси новый у того, кто тебя пригласил.")
            except InvitationExpired:
                return self._retry_input("Код просрочен. Попроси новый у того, кто тебя пригласил.")
            except InvalidInvitation:
                return self._retry_input("Код недействителен. Проверь его или попроси новый у того, кто тебя пригласил.")
        return PendingInputResult(self._home(family, channel=event.channel, prefix=prefix), PendingInputOutcome.COMPLETE)

    @staticmethod
    def _begin_input(
        context: HandlerContext, event: IncomingEvent, family: Family | None, command: str, arguments: str,
    ) -> IncomingResponse:
        if family is not None:
            return FamilyCapability._home(family, channel=event.channel, prefix="Твой профиль уже состоит в семье.")
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
        prompt = f"Пришли {label}. На ввод 10 минут."
        if event.channel == "vk":
            return IncomingResponse(text_content(prompt))
        return IncomingResponse(compose_content(prompt, " Для отмены введи ", code_span("/cancel"), "."))

    @staticmethod
    def _retry_input(answer: str) -> PendingInputResult:
        return PendingInputResult(IncomingResponse(text_content(answer)), PendingInputOutcome.RETRY)

    @staticmethod
    def _home(family: Family | None, *, channel: str, prefix: str = "") -> IncomingResponse:
        introduction = prefix + "\n\n" if prefix else ""
        if family is None:
            title = "Семья"
            actions = (ReplyAction("Создать семью", _CREATE), ReplyAction("Вступить по коду", _JOIN))
        else:
            title = f"Семья «{family.name}»"
            actions = (
                ReplyAction("Покупки", f"{SHOPPING_COMMAND} open {family.id}"),
                ReplyAction("Участники", f"{MEMBERS_COMMAND} open {family.id}"),
                ReplyAction("Пригласить", f"{_INVITE} open {family.id}"),
            )
        return IncomingResponse(
            text_content(introduction + title), close_managed_views=True,
            suggested_actions=() if channel == "vk" else actions,
        )

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
                section = ShoppingSection(
                    revision, family.id, actor.joined_at, inline_commands=event.channel == "telegram",
                    vk_menu=event.channel == "vk",
                )
                items = shopping.list_items(app_user_id, family.id, actor_joined_at=section.joined_at)
                section = section.ordinary_view(items, 1)
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
                    return self._home(family, channel=event.channel)
                return self._shopping_click(context, address, shopping, current, parts[2:])
            if parts == ["home"] or (parts == ["up"] and not current.selection_open):
                self._shopping_sections.close(app_user_id, address)
                return self._home(family, channel=event.channel)
            if parts in (["back"], ["up"]):
                items = shopping.list_items(app_user_id, family.id, actor_joined_at=current.joined_at)
                return self._show_shopping_list(context, address, current, items, current.ordinary_page, update=True)
            if len(parts) != 2 or parts[0] not in ("page", "item", *ShoppingSelection):
                return self._open_shopping_hint()
            number = parse_item_number(parts[1])
            if number is None:
                return self._open_shopping_hint()
            items = shopping.list_items(app_user_id, family.id, actor_joined_at=current.joined_at)
        except FamilyAccessEnded:
            return self._family_access_ended()
        if parts[0] == "page":
            return self._show_shopping_list(context, address, current, items, number)
        if parts[0] in tuple(ShoppingSelection):
            return self._begin_shopping_choice(context, address, current, items, ShoppingSelection(parts[0]), number)
        item = next((item for item in items if item.number == number), None)
        if item is None:
            return IncomingResponse(text_content(f"№{number} нет в списке."))
        return IncomingResponse(text_content(f"{item.number}. {item.name}"))

    def _shopping_click(
        self, context: HandlerContext, address: ReplyAddress, shopping: ManageShopping, section: ShoppingSection,
        action: list[str],
    ) -> IncomingResponse:
        # У ядра уже есть адрес и подтверждённое поколение сообщения. Ключ раздела
        # дополнительно исключает нажатия до повторного вступления или открытия раздела.
        if len(action) < 2 or (page := parse_item_number(action[1])) is None:
            return self._open_shopping_hint()
        app_user_id = context.app_user.id
        family_id = section.family_id
        joined_at = section.joined_at
        match action:
            case ["add", _]:
                return self._begin_shopping_add(context, address, section)
            case ["clear", _]:
                items = shopping.clear_bought(app_user_id, family_id, actor_joined_at=joined_at)
                return self._show_shopping_list(context, address, section, items, page, update=True)
            case ["pick", _, "buy" | "delete" as view, position_text, value]:
                number = parse_item_number(value)
                position = parse_item_number(position_text)
                if number is None or position is None:
                    return self._open_shopping_hint()
                selection = ShoppingSelection(view)
                change = shopping.buy if selection == ShoppingSelection.BUY else shopping.delete
                result = change(app_user_id, family_id, (number,), actor_joined_at=joined_at)
                return self._begin_shopping_choice(
                    context, address, section, result.items, selection, page,
                    notice=shopping_notice(result.changes[0], position),
                )
            case ["show", _, "page" | "buy" | "delete" as view]:
                items = shopping.list_items(app_user_id, family_id, actor_joined_at=joined_at)
                if view == "page":
                    return self._show_shopping_list(context, address, section, items, page, update=True)
                return self._begin_shopping_choice(context, address, section, items, ShoppingSelection(view), page)
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
                return self._show_shopping_list(context, address, section, items)
            except ItemNumbersExhausted:
                return self._shopping_numbers_exhausted()
        if command == CLEAR_BOUGHT_COMMAND:
            if arguments:
                return IncomingResponse(compose_content("Отправь ", code_span(command), " без аргументов."))
            items = shopping.clear_bought(app_user_id, family_id, actor_joined_at=section.joined_at)
            return self._show_shopping_list(context, address, section, items)

        if arguments:
            return IncomingResponse(compose_content(
                "Отправь ", code_span(command), " без аргументов. Затем выбери товар или пришли его номер.",
            ))
        selection = ShoppingSelection.BUY if command == BUY_COMMAND else ShoppingSelection.DELETE
        items = shopping.list_items(app_user_id, family_id, actor_joined_at=section.joined_at)
        return self._begin_shopping_choice(context, address, section, items, selection, 1)

    def _begin_shopping_choice(
        self, context: HandlerContext, address: ReplyAddress, section: ShoppingSection, items: tuple[ShoppingItem, ...],
        selection: ShoppingSelection, page: int, *, notice: str | None = None,
    ) -> IncomingResponse:
        page, _, visible = shopping_page(items, page, selecting=True, vk_menu=section.vk_menu)
        update = not section.vk_menu or section.selection_open
        pending_inputs = context.pending_inputs
        if pending_inputs is None:
            return self._open_shopping_hint()
        app_user_id = context.app_user.id
        command = BUY_COMMAND if selection is ShoppingSelection.BUY else DELETE_COMMAND
        with pending_inputs.lock(address):
            current = self._shopping_sections.current(app_user_id, address, section.revision)
            if current is not section:
                return self._open_shopping_hint()
            choice = None
            if visible:
                pending = pending_inputs.begin(
                    address, self.descriptor.capability_id, command, app_user_id=app_user_id,
                    required_mode=self.descriptor.capability_id, value_label="номер покупки", retry_command=command,
                    cancel_return_command=SHOPPING_COMMAND,
                )
                choice = ShoppingChoice(pending.request_id, selection, page,
                                        tuple((position, item.number) for position, item in visible))
            selection_view = replace(section, choice=choice, selection_open=True)
            if not self._shopping_sections.replace(app_user_id, address, section, selection_view):
                if choice is not None:
                    pending_inputs.clear(address, choice.request_id)
                return self._open_shopping_hint()
        return shopping_list(items, selection_view, page, update=update, selection=selection, choice=choice, notice=notice)

    def _show_shopping_list(
        self, context: HandlerContext, address: ReplyAddress, section: ShoppingSection, items: tuple[ShoppingItem, ...],
        page: int = 1, *, update: bool = False,
    ) -> IncomingResponse:
        ordinary = section.ordinary_view(items, page)
        if not self._shopping_sections.replace(context.app_user.id, address, section, ordinary):
            return self._open_shopping_hint()
        return shopping_list(items, ordinary, page, update=update)

    def _continue_shopping_choice(
        self, context: HandlerContext, pending: PendingInput, event: IncomingEvent,
    ) -> PendingInputResult:
        revision = context.mode_revision
        app_user_id = context.app_user.id
        address = event.reply_address
        section = None if revision is None else self._shopping_sections.current(app_user_id, address, revision)
        choice = section.choice if section is not None else None
        if section is None or choice is None or choice.request_id != pending.request_id:
            return PendingInputResult(self._open_shopping_hint(), PendingInputOutcome.COMPLETE)
        if not choice.delivered.is_set():
            return PendingInputResult(IncomingResponse(compose_content(
                "Список для выбора недоступен. Открой ", code_span(SHOPPING_COMMAND), " и повтори ",
                code_span(pending.input_kind), ".",
            )), PendingInputOutcome.RETRY)
        if event.message_id in section.number_message_ids:
            return PendingInputResult(IncomingResponse(text_content(
                "Это сообщение уже обработано. Для следующего выбора отправь новый номер.",
            )), PendingInputOutcome.RETRY)
        position = parse_item_number(event.text.strip())
        number = dict(choice.numbers).get(position) if position is not None else None
        if position is None or number is None:
            return PendingInputResult(IncomingResponse(compose_content(
                "Пришли один номер с текущей страницы или введи ", code_span("/cancel"), ".",
            )), PendingInputOutcome.RETRY)
        claimed = section.record_number_message(event.message_id)
        if not self._shopping_sections.replace(app_user_id, address, section, claimed):
            return PendingInputResult(self._open_shopping_hint(), PendingInputOutcome.COMPLETE)
        with self._open_scenarios() as (_, _, shopping):
            change = shopping.buy if choice.selection is ShoppingSelection.BUY else shopping.delete
            try:
                result = change(
                    app_user_id, section.family_id, (number,), actor_joined_at=section.joined_at,
                    expected_position=position,
                )
            except FamilyAccessEnded:
                return PendingInputResult(self._family_access_ended(), PendingInputOutcome.COMPLETE)
        response = self._begin_shopping_choice(
            context, address, claimed, result.items, choice.selection, choice.page,
            notice=shopping_notice(result.changes[0], position),
        )
        return PendingInputResult(response, PendingInputOutcome.COMPLETE)

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
                cancel_only_menu=True, cancel_return_command=SHOPPING_COMMAND,
            )
            self._shopping_sections.replace(
                app_user_id, address, current, replace(current, add_request_id=pending.request_id),
            )
        return IncomingResponse(text_content(
            "Напиши, что купить, по одному товару на строку. На ввод 10 минут.",
        ), suggested_actions=(ReplyAction("Отмена", "/cancel"),) if section.inline_commands else (),
            view=ResponseView(f"shopping-add:{section.action_key}", cleanup_previous=True)
            if section.inline_commands else None,
            close_managed_views=not section.inline_commands,
        )

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
        return PendingInputResult(self._show_shopping_list(context, event.reply_address, section, items),
                                  PendingInputOutcome.COMPLETE)

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
        show_commands = bool(parts and parts[0] == "commands")
        if len(parts) == 2 and parts[0] == "open":
            if parts[1] != str(family.id):
                return self._family_access_ended()
        elif (2 <= len(parts) <= 3 and parts[0] == "page") or (show_commands and len(parts) == 2):
            if len(parts) == 3 and parts[2] != str(family.id):
                return self._family_access_ended()
            number = parts[1]
            if len(number) > 10 or not number.isascii() or not number.isdecimal() or int(number) < 1:
                return IncomingResponse(compose_content(
                    "Укажи номер страницы или отправь ", code_span(MEMBERS_COMMAND), ".",
                ))
            page = int(number)
        elif len(parts) == 1 and not show_commands:
            reference = parts[0]
        elif len(parts) > 1:
            return IncomingResponse(compose_content("Посмотреть участников: ", code_span(MEMBERS_COMMAND), "."))
        try:
            members = families.list_members(context.app_user.id, family.id)
        except FamilyAccessEnded:
            return self._family_access_ended()
        if reference is None:
            return self._members_view.show(context, address, family, members, page, show_commands=show_commands)
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
            return IncomingResponse(text_content(f"Приглашение в семью «{family.name}»"), suggested_actions=(
                ReplyAction("Создать код", f"{_INVITE} create {family.id}"), ReplyAction("Назад", "/family"),
            ))
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
        ), suggested_actions=(ReplyAction("Назад", f"{_INVITE} open {issued.family.id}"),))
