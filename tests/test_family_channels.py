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
