"""Проверяет семейный вход через общую маршрутизацию и временную SQLite."""

from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_channel_message_processor, create_user_identity_service
from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.capabilities.family.domain.invitations import ManageInvitations
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse
from tests.response_assertions import response_text


class FamilyChannelsTest(unittest.TestCase):
    """У семейного членства один профиль и независимые режимы Telegram и VK."""

    def setUp(self) -> None:
        """Мигрирует временную базу и связывает два канала первого профиля."""
        directory = self.enterContext(TemporaryDirectory())
        self.database_path = Path(directory) / "family.db"
        self.owner_tg = IncomingIdentity("telegram", "101", "101")
        self.owner_vk = IncomingIdentity("vk", "201", "201")
        self.guest = IncomingIdentity("vk", "202", "202")
        self.other = IncomingIdentity("telegram", "102", "102")
        self._message_id = 0
        with connect_database(self.database_path) as connection:
            apply_migrations(connection)
            identities = create_user_identity_service(connection)
            owner = identities.register_with_name(self.owner_tg, display_name="Оля")
            identities.link(code=identities.create_link_code(owner.id).code, identity=self.owner_vk)
            identities.register_with_name(self.guest, display_name="Илья")
            identities.register_with_name(self.other, display_name="Аня")

    def _runtime(self, *capabilities: str) -> AbstractContextManager[ChannelMessageProcessor]:
        return create_channel_message_processor(database_path=self.database_path, enabled_capabilities=capabilities)

    def _event(self, identity: IncomingIdentity, text: str) -> IncomingEvent:
        self._message_id += 1
        return IncomingEvent(
            identity.channel, identity.external_chat_id, str(self._message_id), text, identity.external_user_id,
        )

    def _send(self, processor: ChannelMessageProcessor, identity: IncomingIdentity, text: str) -> IncomingResponse:
        return processor(self._event(identity, text))

    def _choose(
        self, processor: ChannelMessageProcessor, identity: IncomingIdentity, response: IncomingResponse, label: str,
    ) -> IncomingResponse:
        action = next(action for action in response.bound_actions if action.label == label)
        return processor.select_action(self._event(identity, "callback"), action.token)

    def _joined_family(self, processor: ChannelMessageProcessor) -> None:
        self._send(processor, self.owner_tg, "/family")
        self._send(processor, self.owner_tg, "/family_create")
        self._send(processor, self.owner_tg, "Дом")
        issued = self._send(processor, self.owner_tg, "/family_invite create")
        code = re.search(r"Код: ([A-Z2-9-]+)", response_text(issued))
        self.assertIsNotNone(code)
        self._send(processor, self.guest, "/family")
        self._send(processor, self.guest, "/family_join")
        self._send(processor, self.guest, code[1])

    def _decision(self, response: IncomingResponse, choice: str = "yes") -> str:
        return next(action.command for action in response.actions if action.command.endswith(f" {choice}"))

    def _member_command(
        self, processor: ChannelMessageProcessor, identity: IncomingIdentity, name: str, operation: str,
    ) -> str:
        roster = self._send(processor, identity, "/family_members")
        member = self._choose(processor, identity, roster, name)
        command = re.search(rf"(/family_{operation} [\w-]+)", response_text(member))
        self.assertIsNotNone(command)
        return command[1]

    def test_family_is_server_opt_in_and_private_only(self) -> None:
        """Семейный вход недоступен без включения, регистрации и личного чата."""
        with self._runtime() as processor:
            self.assertNotIn("/family", response_text(self._send(processor, self.owner_tg, "/help")))
            self.assertFalse(self._send(processor, self.owner_tg, "/family").bound_actions)
        with self._runtime("family") as processor:
            group = IncomingIdentity("telegram", self.owner_tg.external_user_id, "-100101")
            self.assertNotIn("/family", response_text(self._send(processor, group, "/help")))
            self.assertIn("в личном чате", response_text(self._send(processor, group, "/family")))
            stranger = IncomingIdentity("telegram", "666", "666")
            self.assertIn("/register", response_text(self._send(processor, stranger, "/family")))
            self.assertIn("/family", response_text(self._send(processor, self.owner_tg, "/help")))
            self.assertIn("войди в режим", response_text(self._send(processor, self.owner_tg, "/family_create")))

    def test_invitation_joins_distinct_profile_and_membership_follows_linked_profile(self) -> None:
        """Проверяет кнопки и ручные команды на одном хранилище обоих каналов."""
        with self._runtime("family") as processor:
            initial = self._send(processor, self.owner_tg, "/family")
            self.assertIn("/family_create", response_text(initial))
            waiting = self._choose(processor, self.owner_tg, initial, "Создать семью")
            self.assertIn("10 минут", response_text(waiting))
            self.assertIn("Напиши непустое", response_text(self._send(processor, self.owner_tg, " \t ")))
            created = self._send(processor, self.owner_tg, "Дом")
            self.assertIn("Семья «Дом»", response_text(created))

            self.assertIn("войди в режим", response_text(self._send(processor, self.owner_vk, "/family_invite")))
            self.assertIn("Семья «Дом»", response_text(self._send(processor, self.owner_vk, "/family")))
            invite = self._choose(
                processor, self.owner_tg, self._choose(processor, self.owner_tg, created, "Пригласить"), "Создать код",
            )
            invite_text = response_text(invite)
            self.assertIn("UTC", invite_text)
            code = re.search(r"Код: ([A-Z2-9-]+)", invite_text)
            self.assertIsNotNone(code)

            self._send(processor, self.guest, "/family")
            self._send(processor, self.guest, "/family_join")
            self.assertIn("Код недействителен", response_text(self._send(processor, self.guest, "нет-кода")))
            joined = self._send(processor, self.guest, code[1])
            self.assertIn("Ты вступил в семью.", response_text(joined))
            self.assertIn("Семья «Дом»", response_text(joined))
            guest_invite = self._send(processor, self.guest, "/family_invite create")
            self.assertIn("Код:", response_text(guest_invite))

            self._send(processor, self.other, "/family")
            self._send(processor, self.other, "/family_join")
            self.assertIn("Код уже использован", response_text(self._send(processor, self.other, code[1])))
            self.assertNotIn("Семья «Дом»", response_text(self._send(processor, self.other, "/family")))

    def test_reentering_family_and_cancelling_clears_waiting_input(self) -> None:
        """Отмена и новый вход освобождают чат от прежнего запроса названия."""
        with self._runtime("family") as processor:
            for interrupt in ("/cancel", "/family"):
                with self.subTest(interrupt=interrupt):
                    self._send(processor, self.owner_tg, "/family")
                    self._send(processor, self.owner_tg, "/family_create")
                    self._send(processor, self.owner_tg, interrupt)
                    result = self._send(processor, self.owner_tg, "Лишнее название")
                    self.assertNotIn("Семья «Лишнее название»", response_text(result))
                    self.assertIn("/family_join", response_text(result))

    def test_incorrect_code_does_not_extend_pending_input(self) -> None:
        """После десяти минут код требует заново открыть ожидание."""
        now = datetime(2026, 11, 16, tzinfo=UTC)
        with self._runtime("family") as processor, patch.object(processor._pending_inputs, "_clock", lambda: now):
            self._send(processor, self.guest, "/family")
            self._send(processor, self.guest, "/family_join")
            now += timedelta(minutes=9)
            invalid = self._send(processor, self.guest, "ошибочный код")
            self.assertIn("Код недействителен", response_text(invalid))
            now += timedelta(minutes=1)
            expired = self._send(processor, self.guest, "опоздавший код")
            self.assertIn("/family_join", response_text(expired))
            self.assertNotIn("Код недействителен", response_text(expired))

    def test_members_actions_use_current_chat_and_names(self) -> None:
        """Участник и владелец видят общий состав, а команда выбора относится к одному чату."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            home = self._send(processor, self.owner_tg, "/family")
            roster = self._choose(processor, self.owner_tg, home, "Участники")
            text = response_text(roster)
            self.assertIn("Оля (владелец, ты)", text)
            self.assertIn("Илья (участник)", text)
            reference = re.search(r"Илья \(участник\) — (/family_members [\w-]+)", text)
            self.assertIsNotNone(reference)
            command = reference[1]

            self._send(processor, self.owner_vk, "/family")
            self.assertIn("Выбор участника устарел", response_text(self._send(processor, self.owner_vk, command)))
            guest_roster = self._send(processor, self.guest, "/family_members")
            self.assertIn("Оля (владелец)", response_text(guest_roster))
            self.assertIn("Илья (участник, ты)", response_text(guest_roster))
            self.assertIn("Илья\nУчастник семьи «Дом»", response_text(self._send(processor, self.owner_tg, command)))
            self.assertIn("Илья\nУчастник семьи «Дом»", response_text(self._choose(processor, self.owner_tg, roster, "Илья")))

            self._send(processor, self.owner_tg, "/family")
            self.assertIn("Выбор участника устарел", response_text(self._send(processor, self.owner_tg, command)))
            self._send(processor, self.other, "/family")
            other_view = response_text(self._send(processor, self.other, "/family_members"))
            self.assertNotIn("Илья", other_view)

    def test_members_with_equal_names_have_distinct_references(self) -> None:
        """Совпадение имени не подменяет участника в ручной команде."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._send(processor, self.guest, "/settings")
            self._send(processor, self.guest, "/name Оля")
            roster = response_text(self._send(processor, self.owner_tg, "/family_members"))
            references = re.findall(r"Оля \([^)]+\) — (/family_members [\w-]+)", roster)
            self.assertEqual(len(set(references)), 2)
            self.assertIn("Владелец семьи", response_text(self._send(processor, self.owner_tg, references[0])))
            self.assertIn("Участник семьи", response_text(self._send(processor, self.owner_tg, references[1])))

    def test_members_list_paginates_without_losing_text_navigation(self) -> None:
        """Кнопка и ручная команда второй страницы открывают оставшихся участников."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            with connect_database(self.database_path) as connection:
                profiles = create_user_identity_service(connection)
                owner = profiles.resolve_without_touch(self.owner_tg)
                repository = SQLiteFamilyRepository(connection)
                family = repository.find_for_member(owner.id)
                invitations = ManageInvitations(repository)
                for number in range(301, 310):
                    identity = IncomingIdentity("vk", str(number), str(number))
                    member = profiles.register_with_name(identity, display_name=f"Участник {number}")
                    invitations.join(member.id, invitations.issue(owner.id, family.id).code)
            first = self._send(processor, self.owner_tg, "/family_members")
            self.assertIn("1/2", response_text(first))
            self.assertIn("/family_members page 2", response_text(first))
            self.assertNotIn("Участник 309", response_text(first))
            second = self._choose(processor, self.owner_tg, first, "Следующая страница")
            self.assertIn("Участник 309", response_text(second))
            self.assertIn("/family_members page 1", response_text(second))
            manual = self._send(processor, self.owner_tg, "/family_members page 2")
            self.assertEqual(response_text(manual), response_text(second))

    def test_member_confirmation_is_addressed_and_one_use(self) -> None:
        """Решение привязано к профилю, чату, операции, версии и текущему запросу."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._send(processor, self.owner_vk, "/family")
            command = self._member_command(processor, self.owner_tg, "Илья", "remove")
            pending = self._send(processor, self.owner_tg, command)
            self.assertIn("Исключить участника «Илья»", response_text(pending))
            approve = self._decision(pending)
            for identity in (self.guest, self.owner_vk):
                with self.subTest(identity=identity):
                    self.assertNotIn(approve, response_text(self._send(processor, identity, "/confirm")))
                    self.assertIn("Подтверждение недоступно", response_text(self._send(processor, identity, approve)))
                    self.assertIn("кнопка больше недоступна", response_text(self._choose(
                        processor, identity, pending, "Исключить из семьи",
                    )))
            for invalid in (approve.replace(" remove ", " transfer "), approve.replace(" 1 yes", " 2 yes")):
                self.assertIn("Подтверждение недоступно", response_text(self._send(processor, self.owner_tg, invalid)))

            listed = self._send(processor, self.owner_tg, "/confirm")
            self.assertIn(approve, response_text(listed))
            self.assertIn(approve, response_text(self._send(processor, self.owner_tg, "/help")))
            self.assertIn("Илья", response_text(self._send(processor, self.owner_tg, "/family_members")))
            self.assertIn("Действие отменено", response_text(self._send(
                processor, self.owner_tg, self._decision(pending, "no"),
            )))
            self.assertIn("Подтверждение недоступно", response_text(self._send(processor, self.owner_tg, approve)))
            self.assertIn("Илья", response_text(self._send(processor, self.owner_tg, "/family_members")))

            replaced = self._send(processor, self.owner_tg, command)
            pending = self._send(processor, self.owner_tg, command)
            self.assertIn("Подтверждение недоступно", response_text(self._send(
                processor, self.owner_tg, self._decision(replaced),
            )))
            member_roster = self._send(processor, self.guest, "/family_members")
            self.assertIn("Участник исключён", response_text(self._choose(
                processor, self.owner_tg, pending, "Исключить из семьи",
            )))
            self.assertIn("Подтверждение недоступно", response_text(self._send(
                processor, self.owner_tg, self._decision(pending),
            )))
            stale_selection = self._choose(processor, self.guest, member_roster, "Оля (владелец)")
            self.assertIn("Доступ к семье завершён", response_text(stale_selection))
            self.assertNotIn("Дом", response_text(stale_selection))

    def test_transfer_and_leave_update_both_linked_channels(self) -> None:
        """Прежний владелец выходит после передачи роли, а оба канала видят результат."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._send(processor, self.owner_vk, "/family")
            self.assertIn("Перед выходом передай", response_text(self._send(processor, self.owner_tg, "/family_leave")))
            self.assertIn("только владельцу", response_text(self._send(processor, self.guest, "/family_remove")))

            stale_remove = self._send(processor, self.owner_tg, self._member_command(
                processor, self.owner_tg, "Илья", "remove",
            ))
            transfer = self._send(processor, self.owner_vk, self._member_command(
                processor, self.owner_vk, "Илья", "transfer",
            ))
            self.assertIn("Роль владельца передана", response_text(self._send(
                processor, self.owner_vk, self._decision(transfer),
            )))
            self.assertNotIn(self._decision(stale_remove), response_text(self._send(processor, self.owner_tg, "/confirm")))
            self.assertIn("только владельцу", response_text(self._send(
                processor, self.owner_tg, self._decision(stale_remove),
            )))
            self.assertIn("Илья (владелец", response_text(self._send(processor, self.owner_vk, "/family_members")))

            roster = self._send(processor, self.owner_tg, "/family_members")
            leave = self._choose(processor, self.owner_tg, roster, "Выйти из семьи")
            result = self._choose(processor, self.owner_tg, leave, "Выйти из семьи")
            self.assertIn("Ты вышел из семьи", response_text(result))
            self.assertEqual([action.label for action in result.bound_actions], ["Создать семью", "Вступить по коду"])
            self.assertNotIn("Семья «Дом»", response_text(self._send(processor, self.owner_vk, "/family")))
            self.assertIn("Илья (владелец, ты)", response_text(self._send(processor, self.guest, "/family_members")))

    def test_confirmation_expires_after_ttl_and_mode_change(self) -> None:
        """Команда из прошлой ревизии или истёкшего ожидания ничего не меняет."""
        now = 500.0
        with self._runtime("family") as processor, patch(
            "compadre_assistant.capabilities.family.presentation.confirmations.monotonic", lambda: now,
        ):
            self._joined_family(processor)
            pending = self._send(processor, self.guest, "/family_leave")
            now += 599
            self.assertIn(self._decision(pending), response_text(self._send(processor, self.guest, "/confirm")))
            now += 1
            self.assertNotIn(self._decision(pending), response_text(self._send(processor, self.guest, "/confirm")))
            self.assertIn("Подтверждение недоступно", response_text(self._send(
                processor, self.guest, self._decision(pending),
            )))

            pending = self._send(processor, self.guest, "/family_leave")
            self._send(processor, self.guest, "/exit")
            self._send(processor, self.guest, "/family")
            self.assertNotIn(self._decision(pending), response_text(self._send(processor, self.guest, "/help")))
            self.assertIn("Подтверждение недоступно", response_text(self._send(
                processor, self.guest, self._decision(pending),
            )))
            self.assertIn("Илья", response_text(self._send(processor, self.owner_tg, "/family_members")))

    def test_rejoining_family_invalidates_the_previous_target(self) -> None:
        """Запрос исключения не применяется к новому членству того же профиля."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            remove = self._send(processor, self.owner_tg, self._member_command(
                processor, self.owner_tg, "Илья", "remove",
            ))
            leave = self._send(processor, self.guest, "/family_leave")
            self._send(processor, self.guest, self._decision(leave))
            issued = self._send(processor, self.owner_tg, "/family_invite create")
            code = re.search(r"Код: ([A-Z2-9-]+)", response_text(issued))
            self.assertIsNotNone(code)
            self._send(processor, self.guest, "/family_join")
            self.assertIn("Ты вступил в семью", response_text(self._send(processor, self.guest, code[1])))
            self.assertNotIn(self._decision(remove), response_text(self._send(processor, self.owner_tg, "/confirm")))
            self.assertIn("Выбери другого действующего участника", response_text(self._send(
                processor, self.owner_tg, self._decision(remove),
            )))
            self.assertIn("Илья", response_text(self._send(processor, self.owner_tg, "/family_members")))

    def test_close_rechecks_membership_and_revokes_invitations(self) -> None:
        """Подтверждение закрытия защищает нового участника и затем погашает код."""
        with self._runtime("family") as processor:
            self._send(processor, self.owner_tg, "/family")
            self._send(processor, self.owner_tg, "/family_create")
            self._send(processor, self.owner_tg, "Дом")
            close = self._send(processor, self.owner_tg, "/family_close")
            for guest in (self.guest, self.other):
                issued = self._send(processor, self.owner_tg, "/family_invite create")
                code = re.search(r"Код: ([A-Z2-9-]+)", response_text(issued))
                self.assertIsNotNone(code)
                self._send(processor, guest, "/family")
                self._send(processor, guest, "/family_join")
                if guest == self.guest:
                    self._send(processor, guest, code[1])

            self.assertIn("единственный участник", response_text(self._send(
                processor, self.owner_tg, self._decision(close),
            )))
            leave = self._send(processor, self.guest, "/family_leave")
            self._send(processor, self.guest, self._decision(leave))
            roster = self._send(processor, self.owner_tg, "/family_members")
            close = self._choose(processor, self.owner_tg, roster, "Закрыть семью")
            result = self._choose(processor, self.owner_tg, close, "Закрыть семью")
            self.assertIn("Семья закрыта", response_text(result))
            self.assertIn("/family_create", response_text(result))
            self.assertIn("Код недействителен", response_text(self._send(processor, self.other, code[1])))

    def test_server_disabling_preserves_family_and_invalidates_old_confirmation(self) -> None:
        """Серверное отключение сохраняет данные и старый запрос не переживает сборку."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            leave = self._send(processor, self.guest, "/family_leave")
        with self._runtime() as processor:
            self.assertFalse(self._send(processor, self.guest, "/family").bound_actions)
        with self._runtime("family") as processor:
            self.assertIn("Илья", response_text(self._send(processor, self.owner_tg, "/family_members")))
            self.assertIn("Подтверждение недоступно", response_text(self._send(
                processor, self.guest, self._decision(leave),
            )))
            self.assertIn("Семья «Дом»", response_text(self._send(processor, self.guest, "/family")))
