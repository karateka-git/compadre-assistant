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
from compadre_assistant.core.presentation.incoming.action_contracts import ActionPurpose, ActionScope
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
            identities.link(code=identities.create_link_code(owner.id, target_channel="vk").code, identity=self.owner_vk)
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

    def _deliver(self, response: IncomingResponse, message_id: str) -> None:
        self.assertIsNotNone(response.view_delivery)
        self.assertTrue(response.view_delivery.start())
        response.view_delivery.finish(message_id)

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
        roster = self._send(processor, identity, "/family_members commands")
        reference = re.search(rf"{re.escape(name)} \([^)]+\) — /family_members ([\w-]+)", response_text(roster))
        self.assertIsNotNone(reference)
        return f"/family_{operation} {reference[1]}"

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
            self.assertIn("Список покупок пуст", response_text(opened))
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
                    self.assertEqual(response_text(home), "Семья «Дом»")
                    self.assertIn("/family_members commands [<page>]", response_text(
                        self._send(processor, identity, "/help"),
                    ))
                    roster = (self._choose(processor, identity, home, "Участники") if identity.channel == "telegram"
                              else self._send(processor, identity, "/family_members"))
                    member = self._choose(processor, identity, roster, "Илья")
                    self.assertEqual(response_text(member), "Илья\nУчастник семьи «Дом».")
                    roster = self._choose(processor, identity, member, "Назад")
                    self.assertIn("Участники семьи «Дом»", response_text(roster))
                    home = self._choose(processor, identity, roster, "Назад")
                    self.assertIn("Семья «Дом»", response_text(home))
                    invitation = (self._choose(processor, identity, home, "Пригласить")
                                  if identity.channel == "telegram"
                                  else self._send(processor, identity, "/family_invite"))
                    self.assertEqual(response_text(invitation), "Приглашение в семью «Дом»")
                    issued = self._choose(processor, identity, invitation, "Создать код")
                    self.assertIn("По коду можно вступить один раз", response_text(issued))
                    self.assertNotIn("/family_invite", response_text(issued))
                    invitation = self._choose(processor, identity, issued, "Назад")
                    self.assertEqual(response_text(invitation), "Приглашение в семью «Дом»")
                    self.assertIn("Семья «Дом»", response_text(self._choose(processor, identity, invitation, "Назад")))

    def test_shopping_pages_preserve_groups_numbers_and_full_titles(self) -> None:
        """На странице остаются номера, а полное длинное название доступно отдельно."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            long_title = "<пакет>&" * 20
            self._seed_shopping("\n".join([long_title, "Хлеб", *(f"Товар {number}" for number in range(2, 12))]),
                                buy=(2,))
            opened = self._send(processor, self.owner_tg, "/family_shopping")
            first_page = response_text(opened)
            self.assertIn("1/2", first_page)
            self.assertIn("Нужно купить", first_page)
            self.assertNotIn("Куплено", first_page)
            self.assertNotIn("Смотри другие страницы", first_page)
            self.assertIn("/family_shopping item 1", first_page)
            self.assertNotIn(long_title, first_page)
            self.assertEqual(response_text(self._send(processor, self.owner_tg, "/family_shopping item 1")),
                             f"1. {long_title}")
            self.assertTrue(opened.view_delivery.start())
            opened.view_delivery.finish("4")
            action = next(action for action in opened.bound_actions if action.label == "Следующая страница")
            event = replace(self._event(self.owner_tg, "callback"), callback_message_id="4")
            updated = processor.select_action(event, action.token)
            self.assertIn("2/2", response_text(updated))
            self.assertIn("11. Товар 11\n\nКуплено\n1. Хлеб", response_text(updated))
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

    def test_shopping_text_commands_require_open_section_and_confirmed_page(self) -> None:
        """Ручные команды опираются на показанные позиции в исходном чате."""
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
            self.assertIn("На ввод 10 минут", response_text(self._send(processor, self.owner_tg, "/add")))
            added = self._send(processor, self.owner_tg, "Чай\n\nМолоко, 2 л")
            self.assertIn("3. Чай\n4. Молоко, 2 л", response_text(added))
            self.assertIsNone(added.view_delivery.target_message_id)
            self._deliver(added, "101")
            self._send(processor, self.guest, "/family_shopping")
            for command in ("/buy 2", "/delete 2"):
                self.assertIn("без аргументов", response_text(self._send(processor, self.guest, command)))
            buying = self._send(processor, self.guest, "/buy")
            self.assertIn("пришли номер товара. На выбор 10 минут.", response_text(buying))
            self._deliver(buying, "102")
            for bad in ("0", "-4", "1,2", "5", "9223372036854775807"):
                with self.subTest(bad=bad):
                    invalid = self._send(processor, self.guest, bad)
                    self.assertIn("Пришли один номер", response_text(invalid))
            read = self._send(processor, self.owner_tg, "/family_shopping")
            self.assertIn("2. Чай", response_text(read))
            self.assertNotIn("Куплено", response_text(read))
            self._deliver(read, "103")
            removed = self._send(processor, self.guest, "2")
            self.assertIn("Нужно купить\n1. Хлеб\n2. Чай\n3. Молоко, 2 л", response_text(removed))
            self._deliver(removed, "102")
            self._deliver(self._send(processor, self.owner_tg, "/delete"), "103")
            self._deliver(self._send(processor, self.owner_tg, "1"), "103")
            read = self._send(processor, self.owner_tg, "/family_shopping")
            self.assertIn("Нужно купить\n1. Чай\n2. Молоко, 2 л\n\nКуплено\n1. Чай", response_text(read))
            cleared = self._send(processor, self.owner_tg, "/family_clear_bought")
            self.assertIn("1. Чай", response_text(cleared))
            self.assertNotIn("Куплено", response_text(cleared))
            self.assertNotIn("Очистить купленное", response_text(cleared))

    def test_shopping_menu_query_reads_the_same_actions_without_executing_input(self) -> None:
        """Запрос раздела сохраняет режим, покупки и даже просроченную запись ожидания."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("Хлеб")
            for identity in (self.owner_tg, self.owner_vk):
                with self.subTest(channel=identity.channel):
                    self._send(processor, identity, "/family")
                    ordinary = self._send(processor, identity, "/family_shopping")
                    probe = self._event(identity, "/exit")
                    offered = processor.section_actions(probe)
                    self.assertEqual(offered, ordinary.suggested_actions)
                    self.assertEqual([action.label for action in offered], ["Добавить", "Купить", "Удалить", "Назад"])
                    self.assertTrue(all(action.scope is ActionScope.SECTION for action in offered))

                    prompt = self._send(processor, identity, "/add")
                    probe = self._event(identity, "Непринятая покупка")
                    pending = processor._pending_inputs.get(probe.reply_address)
                    self.assertIsNotNone(pending)
                    cancel = processor.section_actions(probe)
                    self.assertEqual(cancel, prompt.suggested_actions)
                    self.assertEqual([action.purpose for action in cancel], [ActionPurpose.CANCEL])
                    self.assertIs(processor._pending_inputs.get(probe.reply_address), pending)

                    with patch.object(processor._pending_inputs, "_clock", return_value=pending.expires_at):
                        self.assertEqual(processor.section_actions(probe), offered)
                        self.assertIs(processor._pending_inputs.get(probe.reply_address), pending)

                    self._send(processor, identity, "/cancel")
                    final = self._send(processor, identity, "/family_shopping")
                    self.assertIn("1. Хлеб", response_text(final))
                    self.assertNotIn("Непринятая покупка", response_text(final))

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
            self.assertIn("Список покупок пуст", response_text(self._send(processor, self.guest, "/family_shopping")))
            self.assertIn("1. Свежая покупка", response_text(self._send(processor, self.guest, "/add Свежая покупка")))

    def test_previous_membership_buttons_and_target_cannot_be_used_after_failed_reopen(self) -> None:
        """После повторного вступления прежний список недоступен даже при сбое новой отправки."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("Хлеб")
            self.assertIsNone(self._send(processor, self.guest, "/family_shopping").view_delivery.target_message_id)
            selecting = self._send(processor, self.guest, "/buy")
            self.assertIsNone(selecting.view_delivery.target_message_id)
            self.assertTrue(selecting.view_delivery.start())
            selecting.view_delivery.finish("40")
            old_choice = next(action for action in selecting.bound_actions if action.label == "1. Хлеб")

            self._rejoin_guest()
            before_reopen = self._send(processor, self.guest, "/buy")
            self.assertIn("Доступ к семье завершён", response_text(before_reopen))
            self._send(processor, self.guest, "/family_shopping")
            reopened = self._send(processor, self.guest, "/buy")
            self.assertIsNone(reopened.view_delivery.target_message_id)
            self.assertTrue(reopened.view_delivery.start())
            reopened.view_delivery.finish(None)
            event = replace(self._event(self.guest, "callback"), callback_message_id="40")
            stale = processor.select_action(event, old_choice.token)
            self.assertIn("больше недоступна", response_text(stale))
            self.assertIsNone(stale.view_delivery)

            recovery = self._send(processor, self.guest, "/buy")
            self.assertIn("Не удалось обновить список", response_text(recovery))
            self.assertIsNone(recovery.view_delivery)
            latest = self._send(processor, self.guest, "/family_shopping")
            self.assertIn("Нужно купить\n1. Хлеб", response_text(latest))
            self.assertNotIn("Куплено", response_text(latest))

    def test_concurrent_channel_choices_of_same_item_apply_once(self) -> None:
        """Участники Telegram и VK получают один результат покупки и независимые edit-цели."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("Хлеб")
            choices = []
            for identity, message_id in ((self.owner_tg, "41"), (self.guest, "42")):
                opened = self._send(processor, identity, "/family_shopping")
                if identity.channel == "telegram":
                    self._deliver(opened, message_id)
                selecting = self._send(processor, identity, "/buy")
                self.assertEqual(selecting.view_delivery.target_message_id,
                                 message_id if identity.channel == "telegram" else None)
                self._deliver(selecting, message_id)
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
                self.assertIn("В «Нужно купить» пока ничего нет", response_text(result))
            self.assertEqual(sum("№1 уже куплен" in response_text(result) for result in results), 1)

    def test_number_is_unavailable_until_that_page_is_delivered(self) -> None:
        """Ошибки и запоздалые подтверждения доставки не активируют номер другой страницы."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("Хлеб\nМолоко")
            self._send(processor, self.guest, "/family_shopping")
            buy = self._send(processor, self.guest, "/buy")
            self.assertTrue(buy.view_delivery.start())
            self.assertIn("Список для выбора недоступен", response_text(self._send(processor, self.guest, "1")))

            # Первая публикация ещё идёт, поэтому новое представление не допускается.
            skipped = self._send(processor, self.guest, "/delete")
            self.assertIsNone(skipped.view_delivery)
            buy.view_delivery.finish("100")
            self.assertIn("Список для выбора недоступен", response_text(self._send(processor, self.guest, "1")))

            failed = self._send(processor, self.guest, "/delete")
            self.assertTrue(failed.view_delivery.start())
            failed.view_delivery.finish(None)
            self.assertIn("Список для выбора недоступен", response_text(self._send(processor, self.guest, "1")))
            read = self._send(processor, self.guest, "/family_shopping")
            self.assertIn("Нужно купить\n1. Хлеб\n2. Молоко", response_text(read))
            self.assertNotIn("Куплено", response_text(read))

    def test_position_shift_and_cancel_do_not_apply_a_different_item(self) -> None:
        """Второй чат сдвигает номер, первый должен получить новый список и подтвердить выбор."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("Хлеб\nМолоко\nСыр")
            self._deliver(self._send(processor, self.owner_tg, "/family_shopping"), "100")
            self._send(processor, self.guest, "/family_shopping")
            self._deliver(self._send(processor, self.owner_tg, "/buy"), "100")
            self._deliver(self._send(processor, self.guest, "/delete"), "101")
            self._deliver(self._send(processor, self.guest, "1"), "101")
            shifted = self._send(processor, self.owner_tg, "2")
            self.assertIn("Нумерация изменилась", response_text(shifted))
            self.assertIn("Нужно купить\n1. Молоко\n2. Сыр", response_text(shifted))
            self._deliver(shifted, "100")
            self.assertIn("Ввод отменён", response_text(self._send(processor, self.owner_tg, "/cancel")))
            self._send(processor, self.owner_tg, "2")
            read = self._send(processor, self.owner_tg, "/family_shopping")
            self.assertIn("1. Молоко\n2. Сыр", response_text(read))
            self.assertNotIn("Куплено", response_text(read))
            self._deliver(read, "102")
            selecting = self._send(processor, self.owner_tg, "/buy")
            self._deliver(selecting, "102")
            back = next(action for action in selecting.bound_actions if action.label == "Назад")
            event = replace(self._event(self.owner_tg, "callback"), callback_message_id="102")
            self._deliver(processor.select_action(event, back.token), "102")
            self._send(processor, self.owner_tg, "1")
            ordinary = self._send(processor, self.owner_tg, "/family_shopping")
            self.assertIn("Нужно купить\n1. Молоко\n2. Сыр", response_text(ordinary))
            self.assertNotIn("Куплено", response_text(ordinary))

    def test_repeated_number_message_is_not_applied_to_next_item(self) -> None:
        """Повтор старого сообщения после обновления не выбирает товар с новым номером."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("Хлеб\nМолоко\nСыр")
            self._deliver(self._send(processor, self.owner_tg, "/family_shopping"), "100")
            self._deliver(self._send(processor, self.owner_tg, "/buy"), "100")
            message = self._event(self.owner_tg, "1")
            self._deliver(processor(message), "100")
            repeat = processor(message)
            self.assertIn("Это сообщение уже обработано", response_text(repeat))
            self.assertIsNone(repeat.view_delivery)
            self._deliver(self._send(processor, self.owner_tg, "1"), "100")
            ordinary = self._send(processor, self.owner_tg, "/family_shopping")
            self.assertIn("Нужно купить\n1. Сыр\n\nКуплено\n1. Хлеб\n2. Молоко", response_text(ordinary))

    def test_choice_beyond_ten_keeps_the_product_after_another_chat_shifts_numbers(self) -> None:
        """Номер требует пересмотра страницы, а кнопка выбирает показанный товар."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._seed_shopping("\n".join(f"Товар {number:02d}" for number in range(1, 14)))
            self._deliver(self._send(processor, self.owner_tg, "/family_shopping"), "100")
            origin = self._send(processor, self.owner_tg, "/family_shopping page 2")
            self._deliver(origin, "101")
            first_action = next(action for action in origin.bound_actions if action.label == "Купить")
            event = replace(self._event(self.owner_tg, "callback"), callback_message_id="101")
            choosing = processor.select_action(event, first_action.token)
            self.assertIn("3/3", response_text(choosing))
            self.assertIn("11. Товар 11", response_text(choosing))
            self._deliver(choosing, "101")

            self._send(processor, self.guest, "/family_shopping")
            self._deliver(self._send(processor, self.guest, "/delete"), "102")
            self._deliver(self._send(processor, self.guest, "1"), "102")
            shifted = self._send(processor, self.owner_tg, "11")
            self.assertIn("Нумерация изменилась", response_text(shifted))
            self.assertIn("11. Товар 12", response_text(shifted))
            self._deliver(shifted, "101")
            chosen = next(action for action in shifted.bound_actions if action.label == "11. Товар 12")
            # Второй чат снова меняет позицию. Кнопка хранит номер показанного товара.
            self._deliver(self._send(processor, self.guest, "1"), "102")
            after = processor.select_action(event, chosen.token)
            self._deliver(after, "101")
            back = next(action for action in after.bound_actions if action.label == "Назад")
            ordinary = response_text(processor.select_action(event, back.token))
            self.assertIn("Куплено\n1. Товар 12", ordinary)
            self.assertIn("9. Товар 11\n10. Товар 13", ordinary)
            self.assertNotIn("2/2", ordinary)

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
            self.assertEqual(response_text(initial), "Семья")
            self.assertIn("/family_create", response_text(self._send(processor, self.owner_tg, "/help")))
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
                    self.assertIn("Вступить по коду", {action.label for action in result.bound_actions})

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
            self.assertNotIn("/family", text)
            manual = self._send(processor, self.owner_tg, "/family_members commands")
            self.assertFalse(manual.bound_actions)
            reference = re.search(r"Илья \(участник\) — (/family_members [\w-]+)", response_text(manual))
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
            roster = response_text(self._send(processor, self.owner_tg, "/family_members commands"))
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
            self.assertNotIn("/family_members", response_text(first))
            self.assertNotIn("Участник 309", response_text(first))
            second = self._choose(processor, self.owner_tg, first, "Следующая страница")
            self.assertIn("Участник 309", response_text(second))
            self.assertNotIn("/family_members", response_text(second))
            manual = self._send(processor, self.owner_tg, "/family_members page 2")
            self.assertEqual(response_text(manual), response_text(second))
            first_commands = self._send(processor, self.owner_tg, "/family_members commands")
            self.assertIn("/family_members commands 2", response_text(first_commands))
            second_commands = self._send(processor, self.owner_tg, "/family_members commands 2")
            self.assertIn("Участник 309", response_text(second_commands))
            self.assertIn("/family_members commands 1", response_text(second_commands))

    def test_member_confirmation_is_addressed_and_one_use(self) -> None:
        """Решение привязано к профилю, чату, операции, версии и текущему запросу."""
        with self._runtime("family") as processor:
            self._joined_family(processor)
            self._send(processor, self.owner_vk, "/family")
            command = self._member_command(processor, self.owner_tg, "Илья", "remove")
            pending = self._send(processor, self.owner_tg, command)
            self.assertIn("Исключить участника «Илья»", response_text(pending))
            self.assertNotIn("/confirm", response_text(pending))
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
            self.assertIn("Создать семью", {action.label for action in result.bound_actions})
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
