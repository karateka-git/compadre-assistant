"""Проверяет семейный вход через общую маршрутизацию и временную SQLite."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from compadre_assistant.bootstrap import ChannelMessageProcessor, create_channel_message_processor, create_user_identity_service
from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.capabilities.family.domain.invitations import ManageInvitations
from compadre_assistant.capabilities.family.domain.shopping import ManageShopping
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations
from compadre_assistant.core.domain.users.entities import IncomingIdentity
from compadre_assistant.core.presentation.incoming.events import IncomingEvent
from compadre_assistant.core.presentation.incoming.ports import IncomingResponse
from compadre_assistant.presentation.telegram.rendering import render_telegram_content
from compadre_assistant.presentation.vk.rendering import render_vk_content
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

    def _seed_shopping(self, text: str, *, buy: tuple[int, ...] = ()) -> None:
        with connect_database(self.database_path) as connection:
            owner = create_user_identity_service(connection).resolve_without_touch(self.owner_tg)
            self.assertIsNotNone(owner)
            repository = SQLiteFamilyRepository(connection)
            family = repository.find_for_member(owner.id)
            self.assertIsNotNone(family)
            joined_at = next(member.joined_at for member in repository.list_members(owner.id, family.id)
                             if member.app_user_id == owner.id)
            shopping = ManageShopping(repository)
            shopping.add(owner.id, family.id, text, actor_joined_at=joined_at)
            if buy:
                shopping.buy(owner.id, family.id, buy, actor_joined_at=joined_at)

    def _rejoin_guest(self) -> None:
        with connect_database(self.database_path) as connection:
            profiles = create_user_identity_service(connection)
            guest = profiles.resolve_without_touch(self.guest)
            owner = profiles.resolve_without_touch(self.owner_tg)
            repository = SQLiteFamilyRepository(connection)
            family = repository.find_for_member(guest.id)
            actor = next(member for member in repository.list_members(guest.id, family.id)
                         if member.app_user_id == guest.id)
            repository.leave(guest.id, family.id, actor_joined_at=actor.joined_at)
            invitations = ManageInvitations(repository, clock=lambda: actor.joined_at + timedelta(minutes=1))
            invitations.join(guest.id, invitations.issue(owner.id, family.id).code)

    def test_shopping_section_is_scoped_to_tab_chat_and_revision(self) -> None:
        """Покупки открываются участнику и меняют каталог только одного чата."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._send(processor, self.owner_vk, "/family")
            home = self._send(processor, self.owner_tg, "/family")
            self.assertIn("/family_members", response_text(self._send(processor, self.owner_tg, "/help")))
            opened = self._choose(processor, self.owner_tg, home, "Покупки")
            self.assertIn("Список пуст", response_text(opened))
            self.assertIsNotNone(opened.view_delivery)
            self.assertTrue(opened.view_delivery.start())
            opened.view_delivery.finish("1")
            self.assertNotIn("/family_members", response_text(self._send(processor, self.owner_tg, "/help")))
            self.assertIn("/family_members", response_text(self._send(processor, self.owner_vk, "/help")))
            home_action = next(action for action in opened.bound_actions if action.label == "Назад")
            event = replace(self._event(self.owner_tg, "callback"), callback_message_id="1")
            back = processor.select_action(event, home_action.token)
            self.assertIn("Семья «Дом»", response_text(back))
            self.assertIn("/family_members", response_text(self._send(processor, self.owner_tg, "/help")))
            self._send(processor, self.owner_tg, "/family_shopping")
            self._send(processor, self.owner_tg, "/family")
            self.assertIn("/family_members", response_text(self._send(processor, self.owner_tg, "/help")))
            self.assertIn("Открой список", response_text(self._send(processor, self.owner_tg, "/family_shopping page 2")))

    def test_nested_member_and_invitation_navigation_opens_parent(self) -> None:
        """Возврат из вложенных экранов открывает их родителя в обоих каналах."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            for identity in (self.owner_tg, self.owner_vk):
                with self.subTest(channel=identity.channel):
                    home = self._send(processor, identity, "/family")
                    roster = self._choose(processor, identity, home, "Участники")
                    member = self._choose(processor, identity, roster, "Илья")
                    roster = self._choose(processor, identity, member, "Назад")
                    self.assertIn("Участники семьи «Дом»", response_text(roster))
                    home = self._choose(processor, identity, roster, "Назад")
                    self.assertIn("Семья «Дом»", response_text(home))
                    invitation = self._choose(processor, identity, home, "Пригласить")
                    issued = self._choose(processor, identity, invitation, "Создать код")
                    invitation = self._choose(processor, identity, issued, "Назад")
                    self.assertIn("Чтобы получить одноразовый код", response_text(invitation))
                    self.assertIn("Семья «Дом»", response_text(self._choose(processor, identity, invitation, "Назад")))

    def test_shopping_pages_preserve_groups_numbers_and_full_titles(self) -> None:
        """На странице остаются номера, а полное длинное название доступно отдельно."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            long_title = "<пакет>&" * 20
            self._seed_shopping("\n".join([long_title, "Хлеб", "Персик", "Яблоко", "Молоко", "Лук"]), buy=(2,))
            opened = self._send(processor, self.guest, "/family_shopping")
            first_page = response_text(opened)
            self.assertIn("1/2", first_page)
            self.assertIn("Нужно купить", first_page)
            self.assertIn("Купленные товары", first_page)
            self.assertIn("/family_shopping item 1", first_page)
            self.assertNotIn(long_title, first_page)
            self.assertEqual(response_text(self._send(processor, self.guest, "/family_shopping item 1")), f"1. {long_title}")
            self.assertTrue(opened.view_delivery.start())
            opened.view_delivery.finish("4")
            action = next(action for action in opened.bound_actions if action.label == "Следующая страница")
            event = replace(self._event(self.guest, "callback"), callback_message_id="4")
            updated = processor.select_action(event, action.token)
            self.assertIn("2/2", response_text(updated))
            self.assertIn("2. Хлеб", response_text(updated))
            self.assertEqual(updated.view_delivery.target_message_id, "4")

    def test_shopping_page_from_previous_family_cannot_read_new_family(self) -> None:
        """При смене семейного членства старый раздел теряет доступ к покупкам."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._send(processor, self.guest, "/family_shopping")
            with connect_database(self.database_path) as connection:
                user = create_user_identity_service(connection).resolve_without_touch(self.guest)
                self.assertIsNotNone(user)
                repository = SQLiteFamilyRepository(connection)
                old = repository.find_for_member(user.id)
                self.assertIsNotNone(old)
                actor = next(member for member in repository.list_members(user.id, old.id) if member.app_user_id == user.id)
                repository.leave(user.id, old.id, actor_joined_at=actor.joined_at)
                joined_at = datetime.now(UTC)
                new = repository.create(user.id, "Новый дом", joined_at)
                ManageShopping(repository).add(user.id, new.id, "Секретный список", actor_joined_at=joined_at)
            stale = self._send(processor, self.guest, "/family_shopping page 1")
            self.assertIn("Доступ к семье завершён", response_text(stale))
            self.assertNotIn("Секретный список", response_text(stale))
            fresh = self._send(processor, self.guest, "/family_shopping")
            self.assertIn("Секретный список", response_text(fresh))

    def test_shopping_text_commands_require_open_section_and_publish_new_lists(self) -> None:
        """Ручные команды работают в разделе, а покупки разделяют все его участники."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self.assertNotIn("/buy", response_text(self._send(processor, self.owner_tg, "/help")))
            self.assertIn("Открой список", response_text(self._send(processor, self.owner_tg, "/add Чай")))
            self._send(processor, self.owner_tg, "/family_shopping")
            help_text = response_text(self._send(processor, self.owner_tg, "/help"))
            for command in ("/add", "/buy", "/delete"):
                self.assertIn(command, help_text)
            direct = self._send(processor, self.owner_tg, "/add Хлеб\n\n Чай ")
            self.assertIn("1. Хлеб\n2. Чай", response_text(direct))
            self.assertIsNone(direct.view_delivery.target_message_id)
            self.assertTrue(direct.view_delivery.start())
            direct.view_delivery.finish("100")
            self.assertIn("Ожидание действует 10 минут", response_text(self._send(processor, self.owner_tg, "/add")))
            added = self._send(processor, self.owner_tg, "Чай\n\nМолоко, 2 л")
            self.assertIn("3. Чай\n4. Молоко, 2 л", response_text(added))
            self.assertIsNone(added.view_delivery.target_message_id)
            self._send(processor, self.guest, "/family_shopping")
            bought = self._send(processor, self.guest, "/buy 2")
            self.assertIn("Купленные товары\n2. Чай", response_text(bought))
            self.assertIsNone(bought.view_delivery.target_message_id)
            edited = self._send(processor, self.owner_tg, "/buy 2 4 90")
            self.assertIn("№2 уже куплен.", response_text(edited))
            self.assertIn("№90 нет в списке.", response_text(edited))
            self.assertIn("Купленные товары\n2. Чай\n4. Молоко, 2 л", response_text(edited))
            self.assertIsNone(edited.view_delivery.target_message_id)
            for bad in ("0", "-4", "1,2", "9223372036854775807"):
                with self.subTest(bad=bad):
                    invalid = self._send(processor, self.owner_tg, f"/delete 1 {bad}")
                    self.assertIn("положительных номеров", response_text(invalid))
            deleted = self._send(processor, self.owner_tg, "/delete 1 2 91")
            self.assertNotIn("1. Хлеб", response_text(deleted))
            self.assertIn("№2 уже куплен.", response_text(deleted))
            self.assertIn("Купленные товары\n2. Чай\n4. Молоко, 2 л", response_text(deleted))
            self.assertIn("№91 нет в списке.", response_text(deleted))
            self.assertIsNone(deleted.view_delivery.target_message_id)
            cleared = self._send(processor, self.owner_tg, "/family_clear_bought")
            self.assertIn("3. Чай", response_text(cleared))
            self.assertNotIn("Купленные товары", response_text(cleared))
            self.assertNotIn("Очистить купленное", response_text(cleared))

    def test_old_shopping_add_input_cannot_continue_after_rejoining_same_family(self) -> None:
        """Ожидание не добавляет текст после выхода и повторного вступления в ту же семью."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._send(processor, self.guest, "/family_shopping")
            self._send(processor, self.guest, "/add")
            self._rejoin_guest()
            stale = self._send(processor, self.guest, "Старое ожидание")
            self.assertIn("Доступ к семье завершён", response_text(stale))
            stale = self._send(processor, self.guest, "/add Устаревшая команда")
            self.assertIn("Доступ к семье завершён", response_text(stale))
            self.assertIn("Список пуст", response_text(self._send(processor, self.guest, "/family_shopping")))
            self.assertIn("1. Свежая покупка", response_text(self._send(processor, self.guest, "/add Свежая покупка")))

    def test_previous_membership_buttons_and_target_cannot_be_used_after_failed_reopen(self) -> None:
        """После повторного вступления прежний список недоступен даже при сбое новой отправки."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("Хлеб")
            opened = self._send(processor, self.guest, "/family_shopping")
            self.assertTrue(opened.view_delivery.start())
            opened.view_delivery.finish("40")
            selecting = self._send(processor, self.guest, "/buy")
            self.assertEqual(selecting.view_delivery.target_message_id, "40")
            self.assertTrue(selecting.view_delivery.start())
            selecting.view_delivery.finish("40")
            old_choice = next(action for action in selecting.bound_actions if action.label == "1. Хлеб")

            self._rejoin_guest()
            before_reopen = self._send(processor, self.guest, "/buy")
            self.assertIn("Доступ к семье завершён", response_text(before_reopen))
            reopened = self._send(processor, self.guest, "/family_shopping")
            self.assertTrue(reopened.view_delivery.start())
            reopened.view_delivery.finish(None)
            event = replace(self._event(self.guest, "callback"), callback_message_id="40")
            stale = processor.select_action(event, old_choice.token)
            self.assertIn("Открой список", response_text(stale))
            self.assertIsNone(stale.view_delivery)

            recovery = self._send(processor, self.guest, "/buy")
            self.assertIn("Не удалось обновить список", response_text(recovery))
            self.assertIsNone(recovery.view_delivery)
            latest = self._send(processor, self.guest, "/family_shopping")
            self.assertIn("Нужно купить\n1. Хлеб", response_text(latest))
            self.assertNotIn("Купленные товары", response_text(latest))

    def test_concurrent_channel_choices_of_same_item_apply_once(self) -> None:
        """Участники Telegram и VK получают один результат покупки и независимые edit-цели."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("Хлеб")
            choices = []
            for identity, message_id in ((self.owner_tg, "41"), (self.guest, "42")):
                opened = self._send(processor, identity, "/family_shopping")
                self.assertTrue(opened.view_delivery.start())
                opened.view_delivery.finish(message_id)
                selecting = self._send(processor, identity, "/buy")
                self.assertTrue(selecting.view_delivery.start())
                selecting.view_delivery.finish(message_id)
                choice = next(action for action in selecting.bound_actions if action.label == "1. Хлеб")
                event = replace(self._event(identity, "callback"), callback_message_id=message_id)
                choices.append((event, choice.token))

            ready = Barrier(2)

            def choose(choice: tuple[IncomingEvent, str]) -> IncomingResponse:
                ready.wait(timeout=5)
                return processor.select_action(*choice)

            with ThreadPoolExecutor(max_workers=2) as workers:
                results = tuple(workers.map(choose, choices))
            self.assertEqual([result.view_delivery.target_message_id for result in results], ["41", "42"])
            for result in results:
                self.assertIn("Купленные товары\n1. Хлеб", response_text(result))
            self.assertEqual(sum("№1 уже куплен" in response_text(result) for result in results), 1)

    def test_long_batch_report_keeps_editable_list_and_all_notices(self) -> None:
        """Длинный результат читается по страницам, а список остаётся целым сообщением."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("\n".join("'" * 110 for _ in range(5)))
            self._send(processor, self.guest, "/family_shopping")
            numbers = " ".join(str(number) for number in range(500, 560))
            updated = self._send(processor, self.guest, f"/buy 1 {numbers}")
            self.assertEqual(len(render_telegram_content(updated.content)), 1)
            self.assertEqual(len(render_vk_content(updated.content)), 1)
            self.assertIsNone(updated.view_delivery.target_message_id)
            self.assertTrue(updated.view_delivery.start())
            updated.view_delivery.finish("100")
            self.assertIn("Без изменений: 60", response_text(updated))
            self.assertIn("/family_shopping result 1", response_text(updated))
            pages = []
            for page in (1, 2, 3):
                report = self._send(processor, self.guest, f"/family_shopping result {page}")
                self.assertIsNone(report.view_delivery)
                pages.append(response_text(report))
            self.assertEqual(re.findall(r"№(\d+) нет в списке", "\n".join(pages)), numbers.split())

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
