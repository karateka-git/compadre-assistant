"""Поведенческие тесты общего списка на мигрированной файловой SQLite."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event

from compadre_assistant.capabilities.family.data.sqlite_family_repository import SQLiteFamilyRepository
from compadre_assistant.capabilities.family.data.sqlite_shopping_batches import SQLiteShoppingBatchRepository
from compadre_assistant.capabilities.family.domain.membership import FamilyAccessEnded
from compadre_assistant.capabilities.family.domain.shopping import (
    MAX_ITEM_NUMBER, InvalidItemNumbers, InvalidShoppingNames, ItemChange, ItemChangeStatus,
    ItemNumbersExhausted, ManageShopping, ShoppingAddMessage, ShoppingItemStatus,
)
from compadre_assistant.core.data.sqlite.connection import connect_database
from compadre_assistant.core.data.sqlite.migration_runner import apply_migrations


_NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)  # Базовое время для проверки метаданных покупки.
_CHANGED = ItemChangeStatus.CHANGED  # Позиция обработана.
_MISSING = ItemChangeStatus.NOT_FOUND  # Номер отсутствует в текущем списке.


class SQLiteFamilyShoppingRepositoryTest(unittest.TestCase):
    """Проверяет операции списка с актуальным составом семьи и отдельными писателями."""

    def setUp(self) -> None:
        """Создаёт временную БД с семьёй из двух участников и второй семьёй."""
        self.path = Path(self.enterContext(TemporaryDirectory(prefix="family-shopping-"))) / "family.db"
        self.db = self.enterContext(connect_database(self.path))
        apply_migrations(self.db)
        with self.db:
            self.db.execute("INSERT INTO app_users (id) VALUES (1), (2), (3), (4)")
        self.repository = SQLiteFamilyRepository(self.db)
        self.home = self.repository.create(1, "Дом", _NOW).id
        self.other = self.repository.create(3, "Другая семья", _NOW).id
        with self.db:
            self.db.execute("INSERT INTO family_members VALUES (?, 2, ?)", (self.home, _NOW.isoformat()))
        self.now = _NOW
        self.shopping = ManageShopping(self.repository, clock=lambda: self.now)

    def test_add_each_nonempty_line_and_share_list_in_number_order(self) -> None:
        """Равные строки получают разные номера и видны всем участникам."""
        self.assertEqual(self.shopping.list_items(2, self.home, actor_joined_at=_NOW), ())
        with self.assertRaises(InvalidShoppingNames):
            self.shopping.add(1, self.home, " \n\t", actor_joined_at=_NOW)

        added = self.shopping.add(2, self.home, "  Молоко, 2 л\r\n\nЯблоки\nМолоко, 2 л  ", actor_joined_at=_NOW)
        self.assertEqual([(item.number, item.name) for item in added], [
            (1, "Молоко, 2 л"), (2, "Яблоки"), (3, "Молоко, 2 л"),
        ])
        self.assertTrue(all(
            item.created_by_app_user_id == 2 and item.created_at == _NOW
            and item.status is ShoppingItemStatus.NEEDED and item.bought_at is None
            for item in added
        ))
        self.assertEqual(self.shopping.list_items(1, self.home, actor_joined_at=_NOW), added)
        self.assertEqual(self.shopping.list_items(3, self.other, actor_joined_at=_NOW), ())

    def test_batches_survive_restart_and_respect_due_membership_and_item_state(self) -> None:
        """Продлевает персональные окна, закрывает в срок, проверяет адрес и состав перед отправкой."""
        with self.db:
            self.db.execute("INSERT INTO family_members VALUES (?, 4, ?)", (self.home, _NOW.isoformat()))
        queue = SQLiteShoppingBatchRepository(self.db)
        self.shopping.add(1, self.home, "Чай\nСыр", actor_joined_at=_NOW)
        self.now += timedelta(minutes=1)
        self.shopping.add(2, self.home, "Рис", actor_joined_at=_NOW)
        self.shopping.add(3, self.other, "Перец", actor_joined_at=_NOW)
        self.assertEqual(queue.due_batches(self.now), ())
        self.now += timedelta(minutes=1)
        self.shopping.add(1, self.home, "Соль", actor_joined_at=_NOW)
        old, = queue.due_batches(self.now)
        self.assertEqual(old.recipient_id, 2)
        self.assertEqual([i.name for i in queue.current_items(old)], ["Чай", "Сыр"])
        self.assertEqual(queue.current_items(replace(old, recipient_id=1)), ())
        self.assertEqual(queue.current_items(replace(old, family_id=self.other)), ())
        self.assertEqual(queue.current_items(replace(old, joined_at=self.now)), ())
        self.shopping.buy(1, self.home, (1,), actor_joined_at=_NOW)
        self.shopping.delete(1, self.home, (2,), actor_joined_at=_NOW)
        self.now += timedelta(minutes=2)
        with connect_database(self.path) as reopened:
            restarted = SQLiteShoppingBatchRepository(reopened)
            self.assertEqual([(b.recipient_id, [i.name for i in restarted.current_items(b)])
                              for b in restarted.due_batches(self.now)],
                             [(2, []), (1, ["Рис"]), (4, ["Рис", "Соль"]), (2, ["Соль"])])
        self.repository.leave(2, self.home, actor_joined_at=_NOW)
        with self.db:
            self.db.execute("INSERT INTO family_members VALUES (?, 2, ?)", (self.home, self.now.isoformat()))
        self.assertEqual(queue.current_items(old), ())
        self.assertEqual([b.recipient_id for b in queue.due_batches(self.now)], [1, 4])

    def test_batch_failure_rolls_back_receipt_goods_and_number_allocation(self) -> None:
        """Сбой очереди откатывает всё добавление и разрешает повтор того же сообщения."""
        key = ShoppingAddMessage("vk", "42", "23")
        with self.db:
            self.db.execute("""CREATE TRIGGER reject_batch BEFORE INSERT ON family_shopping_batches
                               BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.shopping.add(1, self.home, "Рис", actor_joined_at=_NOW, source=key)
        self.assertEqual(self.shopping.list_items(1, self.home, actor_joined_at=_NOW), ())
        self.assertEqual(self.db.execute("SELECT count(*) FROM family_shopping_add_receipts").fetchone()[0], 0)
        with self.db:
            self.db.execute("DROP TRIGGER reject_batch")
        added = self.shopping.add(1, self.home, "Рис", actor_joined_at=_NOW, source=key)
        self.assertEqual([i.number for i in added], [1])
        with connect_database(self.path) as reopened:
            later = ManageShopping(SQLiteFamilyRepository(reopened), lambda: _NOW + timedelta(hours=1))
            self.assertEqual(later.add(1, self.home, "Рис", actor_joined_at=_NOW, source=key), added)
            queue = SQLiteShoppingBatchRepository(reopened)
            batch, = queue.due_batches(_NOW + timedelta(minutes=2))
            self.assertEqual(queue.current_items(batch), added)

    def test_concurrent_redelivery_creates_one_addition_and_one_batch(self) -> None:
        """Разные подключения, получившие одно сообщение, фиксируют его один раз."""
        barrier = Barrier(2)
        key = ShoppingAddMessage("telegram", "10", "11")

        def add() -> tuple[int, ...]:
            with connect_database(self.path) as db:
                shopping = ManageShopping(SQLiteFamilyRepository(db), lambda: _NOW)
                barrier.wait(timeout=5)
                return tuple(i.number for i in shopping.add(1, self.home, "Сыр\nХлеб", actor_joined_at=_NOW, source=key))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(add) for _ in range(2)]
            self.assertEqual([f.result() for f in futures], [(1, 2), (1, 2)])
        queue = SQLiteShoppingBatchRepository(self.db)
        batch, = queue.due_batches(_NOW + timedelta(minutes=2))
        self.assertEqual([i.number for i in queue.current_items(batch)], [1, 2])

    def test_buy_deduplicates_numbers_and_preserves_first_mark(self) -> None:
        """Пакет сохраняет порядок номеров и не меняет отметку другого участника."""
        self.shopping.add(1, self.home, "Хлеб\nМолоко\nЯблоки", actor_joined_at=_NOW)
        self.assertEqual(self.shopping.buy(2, self.home, (2,), actor_joined_at=_NOW).changes, (ItemChange(2, _CHANGED),))
        self.now += timedelta(minutes=1)

        result = self.shopping.buy(1, self.home, (2, 9, 3, 3, 2), actor_joined_at=_NOW)

        self.assertEqual(result.changes, (
            ItemChange(2, ItemChangeStatus.ALREADY_BOUGHT), ItemChange(9, _MISSING), ItemChange(3, _CHANGED),
        ))
        self.assertEqual([(item.number, item.status, item.bought_by_app_user_id, item.bought_at) for item in result.items], [
            (1, ShoppingItemStatus.NEEDED, None, None),
            (2, ShoppingItemStatus.BOUGHT, 2, _NOW),
            (3, ShoppingItemStatus.BOUGHT, 1, self.now),
        ])
        with self.assertRaises(InvalidItemNumbers):
            self.shopping.buy(1, self.home, (1, 0), actor_joined_at=_NOW)
        with self.assertRaises(InvalidItemNumbers):
            self.shopping.delete(1, self.home, (1, MAX_ITEM_NUMBER + 1), actor_joined_at=_NOW)
        with self.assertRaises(InvalidItemNumbers):
            self.shopping.buy(1, self.home, (1, 2), actor_joined_at=_NOW, expected_position=1)
        with self.assertRaises(InvalidItemNumbers):
            self.shopping.delete(1, self.home, (1,), actor_joined_at=_NOW, expected_position=0)
        self.assertEqual(self.shopping.list_items(2, self.home, actor_joined_at=_NOW), result.items)

    def test_delete_and_clear_do_not_reuse_numbers_or_touch_other_family(self) -> None:
        """Удаление и очистка сохраняют чужие и оставшиеся номера."""
        self.shopping.add(1, self.home, "Хлеб\nМолоко\nЯблоки\nСыр", actor_joined_at=_NOW)
        self.shopping.add(3, self.other, "Другой хлеб", actor_joined_at=_NOW)
        self.shopping.buy(3, self.other, (1,), actor_joined_at=_NOW)
        self.shopping.buy(1, self.home, (1, 2), actor_joined_at=_NOW)
        deleted = self.shopping.delete(2, self.home, (1, 3, 1, 7), actor_joined_at=_NOW)
        self.assertEqual(deleted.changes, (
            ItemChange(1, ItemChangeStatus.ALREADY_BOUGHT), ItemChange(3, _CHANGED), ItemChange(7, _MISSING),
        ))
        self.assertEqual([item.number for item in deleted.items], [1, 2, 4])
        self.assertEqual([item.number for item in self.shopping.clear_bought(2, self.home, actor_joined_at=_NOW)], [4])
        self.shopping.delete(2, self.home, (4,), actor_joined_at=_NOW)

        added = self.shopping.add(1, self.home, "Новый хлеб", actor_joined_at=_NOW)

        self.assertEqual([item.number for item in added], [5])
        self.assertEqual(self.shopping.buy(2, self.home, (1,), actor_joined_at=_NOW).changes, (ItemChange(1, _MISSING),))
        self.assertEqual(self.shopping.delete(2, self.home, (4,), actor_joined_at=_NOW).changes, (ItemChange(4, _MISSING),))
        other_items = self.shopping.list_items(3, self.other, actor_joined_at=_NOW)
        self.assertEqual([item.status for item in other_items], [ShoppingItemStatus.BOUGHT])

    def test_each_operation_rejects_stale_family_even_with_matching_item_number(self) -> None:
        """После перехода в другую семью старые операции не читают и не меняют ни один список."""
        old_list = self.shopping.add(1, self.home, "Хлеб", actor_joined_at=_NOW)
        self.repository.remove_member(
            1, self.home, 2, actor_joined_at=_NOW, target_joined_at=_NOW,
        )
        new_family = self.repository.create(2, "Новая семья", _NOW).id
        new_list = self.shopping.add(2, new_family, "Рис", actor_joined_at=_NOW)

        for operation, args in (
            (self.shopping.list_items, ()),
            (self.shopping.add, ("Молоко",)),
            (self.shopping.buy, ((1,),)),
            (self.shopping.delete, ((1,),)),
            (self.shopping.clear_bought, ()),
        ):
            with self.subTest(operation=operation.__name__), self.assertRaises(FamilyAccessEnded):
                operation(2, self.home, *args, actor_joined_at=_NOW)
        self.assertEqual(self.shopping.list_items(1, self.home, actor_joined_at=_NOW), old_list)
        self.assertEqual(self.shopping.list_items(2, new_family, actor_joined_at=_NOW), new_list)

    def test_each_operation_checks_membership_after_rejoining_same_family(self) -> None:
        """Прежнее членство не даёт читать, писать или расходовать номера прежней семьи."""
        self.shopping.add(1, self.home, "Хлеб\nЧай", actor_joined_at=_NOW)
        previous = self.shopping.buy(1, self.home, (1,), actor_joined_at=_NOW).items
        self.repository.leave(2, self.home, actor_joined_at=_NOW)
        rejoined_at = _NOW + timedelta(minutes=1)
        with self.db:
            self.db.execute("INSERT INTO family_members VALUES (?, 2, ?)", (self.home, rejoined_at.isoformat()))

        for operation, args in (
            (self.shopping.list_items, ()),
            (self.shopping.add, ("Сыр",)),
            (self.shopping.buy, ((2,),)),
            (self.shopping.delete, ((2,),)),
            (self.shopping.clear_bought, ()),
        ):
            with self.subTest(operation=operation.__name__), self.assertRaises(FamilyAccessEnded):
                operation(2, self.home, *args, actor_joined_at=_NOW)

        self.assertEqual(self.shopping.list_items(2, self.home, actor_joined_at=rejoined_at), previous)
        current = self.shopping.add(2, self.home, "Сыр", actor_joined_at=rejoined_at)
        self.assertEqual([item.number for item in current], [1, 2, 3])

    def test_unknown_status_remains_visible_and_unchanged(self) -> None:
        """Адаптер не считает незнакомое состояние нужной или купленной покупкой."""
        self.shopping.add(1, self.home, "Хлеб\nМолоко\nРис", actor_joined_at=_NOW)
        self.db.execute("PRAGMA ignore_check_constraints = ON")
        try:
            with self.db:
                self.db.execute(
                    "UPDATE family_shopping_items SET status = 'future_state' WHERE family_id = ? AND number = 1",
                    (self.home,),
                )
        finally:
            self.db.execute("PRAGMA ignore_check_constraints = OFF")

        self.assertIs(self.shopping.list_items(2, self.home, actor_joined_at=_NOW)[0].status, ShoppingItemStatus.UNKNOWN)
        expected = ItemChange(1, ItemChangeStatus.UNKNOWN)
        bought = self.shopping.buy(2, self.home, (1, 2), actor_joined_at=_NOW)
        self.assertEqual(bought.changes, (expected, ItemChange(2, _CHANGED)))
        self.assertEqual(self.shopping.delete(2, self.home, (1,), actor_joined_at=_NOW).changes, (expected,))
        self.assertEqual(
            [(item.number, item.status) for item in self.shopping.clear_bought(2, self.home, actor_joined_at=_NOW)],
            [(1, ShoppingItemStatus.UNKNOWN), (3, ShoppingItemStatus.NEEDED)],
        )
        self.assertEqual(self.db.execute(
            "SELECT status FROM family_shopping_items WHERE family_id = ? AND number = 1", (self.home,),
        ).fetchone()[0], "future_state")

    def test_add_and_batch_buy_roll_back_if_second_item_fails(self) -> None:
        """Поздняя ошибка откатывает изменения предыдущего пункта и выделенные номера."""
        with self.db:
            self.db.execute(
                "CREATE TRIGGER fail_add BEFORE INSERT ON family_shopping_items WHEN NEW.number = 2 "
                "BEGIN SELECT RAISE(ABORT, 'test add failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test add failure"):
            self.shopping.add(1, self.home, "Хлеб\nМолоко", actor_joined_at=_NOW)
        self.assertEqual(self.shopping.list_items(1, self.home, actor_joined_at=_NOW), ())
        with self.db:
            self.db.execute("DROP TRIGGER fail_add")
            self.db.execute(
                "CREATE TRIGGER fail_buy BEFORE UPDATE ON family_shopping_items WHEN NEW.number = 2 "
                "BEGIN SELECT RAISE(ABORT, 'test buy failure'); END"
            )
        added = self.shopping.add(2, self.home, "Хлеб\nМолоко", actor_joined_at=_NOW)
        self.assertEqual([item.number for item in added], [1, 2])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test buy failure"):
            self.shopping.buy(1, self.home, (1, 2), actor_joined_at=_NOW)
        self.assertEqual(self.shopping.list_items(2, self.home, actor_joined_at=_NOW), added)

    def test_delete_and_clear_roll_back_an_earlier_deletion(self) -> None:
        """Сбой при удалении сохраняет все пункты пакета и очистки."""
        before = self.shopping.add(1, self.home, "Хлеб\nМолоко", actor_joined_at=_NOW)
        with self.db:
            self.db.execute(
                "CREATE TRIGGER fail_delete BEFORE DELETE ON family_shopping_items WHEN OLD.number = 2 "
                "BEGIN SELECT RAISE(ABORT, 'test delete failure'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test delete failure"):
            self.shopping.delete(1, self.home, (1, 2), actor_joined_at=_NOW)
        self.assertEqual(self.shopping.list_items(2, self.home, actor_joined_at=_NOW), before)
        bought = self.shopping.buy(2, self.home, (1, 2), actor_joined_at=_NOW).items
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test delete failure"):
            self.shopping.clear_bought(1, self.home, actor_joined_at=_NOW)
        self.assertEqual(self.shopping.list_items(2, self.home, actor_joined_at=_NOW), bought)
        self.assertFalse(self.db.in_transaction)

    def test_number_limit_rejects_unfittable_batch_without_using_last_number(self) -> None:
        """Неудачный пакет не переполняет счётчик и оставляет последний номер доступным."""
        with self.db:
            self.db.execute("UPDATE families SET next_item_number = ? WHERE id = ?", (MAX_ITEM_NUMBER, self.home))
        with self.assertRaises(ItemNumbersExhausted):
            self.shopping.add(1, self.home, "Хлеб\nМолоко", actor_joined_at=_NOW)
        added = self.shopping.add(1, self.home, "Хлеб", actor_joined_at=_NOW)
        self.assertEqual([item.number for item in added], [MAX_ITEM_NUMBER])
        with self.assertRaises(ItemNumbersExhausted):
            self.shopping.add(1, self.home, "Молоко", actor_joined_at=_NOW)

    def test_concurrent_writers_allocate_distinct_numbers_and_preserve_first_mark(self) -> None:
        """Два соединения не теряют покупки и не перезаписывают факт покупки."""
        ready = Barrier(2, timeout=5)

        def work(actor: int) -> tuple[int, ItemChangeStatus]:
            with connect_database(self.path) as db:
                shopping = ManageShopping(SQLiteFamilyRepository(db), clock=lambda: _NOW)
                ready.wait()
                shopping.add(actor, self.home, "Хлеб\nМолоко", actor_joined_at=_NOW)
                ready.wait()
                return actor, shopping.buy(actor, self.home, (1,), actor_joined_at=_NOW).changes[0].status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(work, (1, 2)))
        self.assertCountEqual(results.values(), (_CHANGED, ItemChangeStatus.ALREADY_BOUGHT))
        winner = next(actor for actor, status in results.items() if status is _CHANGED)
        items = self.shopping.list_items(1, self.home, actor_joined_at=_NOW)
        self.assertEqual([item.number for item in items], [1, 2, 3, 4])
        self.assertCountEqual([item.created_by_app_user_id for item in items], [1, 1, 2, 2])
        self.assertEqual(items[0].bought_by_app_user_id, winner)

    def test_competing_writes_report_the_result_of_the_transaction_that_won(self) -> None:
        """Два писателя не применяют действие повторно и учитывают состояние исходной цели."""
        for winner, contender, loser_status in (
            ("buy", "buy", ItemChangeStatus.ALREADY_BOUGHT),
            ("delete", "delete", _MISSING),
            ("buy", "delete", ItemChangeStatus.ALREADY_BOUGHT),
            ("delete", "buy", _MISSING),
        ):
            with self.subTest(winner=winner, contender=contender):
                self.shopping.clear_bought(1, self.home, actor_joined_at=_NOW)
                target, = self.shopping.add(1, self.home, "Хлеб", actor_joined_at=_NOW)
                winning_write_locked = Event()
                contender_started = Event()

                def mark_winning_write() -> int:
                    winning_write_locked.set()
                    if not contender_started.wait(timeout=5):
                        raise RuntimeError("Второе соединение не попыталось начать запись")
                    return 1

                def run_winner() -> ItemChangeStatus:
                    with connect_database(self.path) as db:
                        db.create_function("mark_winning_write", 0, mark_winning_write)
                        statement = "UPDATE" if winner == "buy" else "DELETE"
                        db.execute(
                            f"CREATE TEMP TRIGGER watch_write BEFORE {statement} ON family_shopping_items "
                            "BEGIN SELECT mark_winning_write(); END"
                        )
                        shopping = ManageShopping(SQLiteFamilyRepository(db), clock=lambda: _NOW)
                        operation = shopping.buy if winner == "buy" else shopping.delete
                        return operation(1, self.home, (target.number,), actor_joined_at=_NOW).changes[0].status

                def trace_begin(sql: str) -> None:
                    if sql == "BEGIN IMMEDIATE":
                        contender_started.set()

                def run_contender() -> ItemChangeStatus:
                    with connect_database(self.path) as db:
                        if not winning_write_locked.wait(timeout=5):
                            raise RuntimeError("Первое соединение не успело заблокировать запись")
                        db.set_trace_callback(trace_begin)
                        shopping = ManageShopping(SQLiteFamilyRepository(db), clock=lambda: _NOW)
                        operation = shopping.buy if contender == "buy" else shopping.delete
                        return operation(
                            2, self.home, (target.number,), actor_joined_at=_NOW, expected_position=1,
                        ).changes[0].status

                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(run_winner)
                    second = pool.submit(run_contender)
                    self.assertIs(first.result(), _CHANGED)
                    self.assertIs(second.result(), loser_status)
                items = self.shopping.list_items(1, self.home, actor_joined_at=_NOW)
                if winner == "buy":
                    item, = items
                    self.assertEqual((item.status, item.bought_by_app_user_id), (ShoppingItemStatus.BOUGHT, 1))
                else:
                    self.assertEqual(items, ())

    def test_visible_position_must_match_full_needed_list_when_transaction_writes(self) -> None:
        """Сдвиг от покупки в другом чате подтверждается по полной группе «Нужно купить»."""
        self.shopping.add(1, self.home, "Хлеб\nМолоко\nЯблоки\nСыр", actor_joined_at=_NOW)
        self.shopping.buy(1, self.home, (1,), actor_joined_at=_NOW)
        with connect_database(self.path) as other_db:
            other = ManageShopping(SQLiteFamilyRepository(other_db))
            self.assertEqual(other.delete(2, self.home, (2,), actor_joined_at=_NOW, expected_position=1).changes,
                             (ItemChange(2, _CHANGED),))

        for operation in (self.shopping.buy, self.shopping.delete):
            shifted = operation(1, self.home, (3,), actor_joined_at=_NOW, expected_position=2)
            self.assertEqual(shifted.changes, (ItemChange(3, ItemChangeStatus.POSITION_CHANGED),))
            self.assertEqual([item.status for item in shifted.items], [
                ShoppingItemStatus.BOUGHT, ShoppingItemStatus.NEEDED, ShoppingItemStatus.NEEDED,
            ])

        # Стабильная кнопка переживает сдвиг, а ручной ввод принимает обновлённую позицию.
        self.assertEqual(self.shopping.delete(1, self.home, (4,), actor_joined_at=_NOW).changes,
                         (ItemChange(4, _CHANGED),))
        self.assertEqual(self.shopping.buy(1, self.home, (3,), actor_joined_at=_NOW, expected_position=1).changes,
                         (ItemChange(3, _CHANGED),))

    def test_position_operations_respect_clear_and_membership_changes_from_other_connection(self) -> None:
        """После очистки или потери членства действует состояние, подтверждённое другой транзакцией."""
        self.shopping.add(1, self.home, "Хлеб\nМолоко", actor_joined_at=_NOW)
        with connect_database(self.path) as other_db:
            repository = SQLiteFamilyRepository(other_db)
            other = ManageShopping(repository)
            other.buy(2, self.home, (1,), actor_joined_at=_NOW, expected_position=1)
            self.assertEqual([item.number for item in self.shopping.clear_bought(1, self.home, actor_joined_at=_NOW)], [2])
            self.assertEqual(other.delete(2, self.home, (1,), actor_joined_at=_NOW, expected_position=1).changes,
                             (ItemChange(1, _MISSING),))
            self.repository.remove_member(1, self.home, 2, actor_joined_at=_NOW, target_joined_at=_NOW)
            with self.assertRaises(FamilyAccessEnded):
                other.buy(2, self.home, (2,), actor_joined_at=_NOW, expected_position=1)
        milk, = self.shopping.list_items(1, self.home, actor_joined_at=_NOW)
        self.assertIs(milk.status, ShoppingItemStatus.NEEDED)
