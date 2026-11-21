"""Проверяет сроки накопления и состав персональной сводки покупок."""

import unittest
from datetime import UTC, datetime, timedelta

from compadre_assistant.capabilities.family.domain.shopping import ShoppingItem, ShoppingItemStatus
from compadre_assistant.capabilities.family.domain.shopping_notifications import ShoppingBatchWindow, notification_items


class FamilyNotificationRulesTest(unittest.TestCase):
    """Проверяет примеры требований с фиксированным временем."""

    def test_quiet_period_and_exact_deadline(self) -> None:
        """Окно 18:00 и 18:01 закрывается ровно в 18:03."""
        start = datetime(2026, 11, 21, 18, tzinfo=UTC)
        window = ShoppingBatchWindow.start(start)
        self.assertEqual(window.due_at, start + timedelta(minutes=2))
        extended = window.extend(start + timedelta(minutes=1))
        self.assertIsNotNone(extended)
        assert extended is not None
        self.assertEqual(extended.due_at, start + timedelta(minutes=3))
        self.assertIsNone(extended.extend(extended.due_at))
        self.assertIsNone(extended.extend(start + timedelta(hours=1)))

    def test_continuous_additions_stop_at_ten_minutes(self) -> None:
        """Ежеминутное добавление сохраняет крайний срок первой подборки."""
        start = datetime(2026, 11, 21, 18, tzinfo=UTC)
        window = ShoppingBatchWindow.start(start)
        for minute in range(1, 10):
            extended = window.extend(start + timedelta(minutes=minute))
            self.assertIsNotNone(extended)
            assert extended is not None
            window = extended
        self.assertEqual(window.due_at, start + timedelta(minutes=10))
        self.assertIsNone(window.extend(window.due_at))
        self.assertEqual(ShoppingBatchWindow.start(window.due_at).due_at, start + timedelta(minutes=12))

    def test_each_recipient_sees_ordered_remaining_items_from_other_authors(self) -> None:
        """Сохраняет повтор названия, отбрасывает свои, купленные и незнакомые статусы."""
        now = datetime(2026, 11, 21, tzinfo=UTC)
        items = [
            ShoppingItem(number, "Хлеб", status, author, now, None, None)
            for number, author, status in (
                (3, 2, ShoppingItemStatus.NEEDED), (1, 3, ShoppingItemStatus.NEEDED),
                (2, 1, ShoppingItemStatus.NEEDED), (4, 3, ShoppingItemStatus.BOUGHT),
                (5, 2, ShoppingItemStatus.UNKNOWN),
            )
        ]
        self.assertEqual([item.number for item in notification_items(items, 1)], [1, 3])
        self.assertEqual([item.number for item in notification_items(items, 2)], [1, 2])
        self.assertEqual(notification_items(items[3:], 1), ())
