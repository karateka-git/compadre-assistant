"""Проверки распознавания фото и текста через подменный ответ SDK."""

import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from compadre_assistant.capabilities.family.data.expense_recognition import ReceiptRecognizer
from compadre_assistant.capabilities.family.domain.expenses import ExpenseSource
from compadre_assistant.capabilities.family.domain.recognition import ExpenseRecognitionError


class ExpenseRecognitionTests(unittest.TestCase):
    """Проверяет доступные источники, точную арифметику и отказ провайдера."""

    def setUp(self):
        self.mock = self.enterContext(patch("compadre_assistant.capabilities.family.data.expense_recognition.OpenAI"))
        self.call = self.mock.return_value.__enter__.return_value.chat.completions.create
        self.recognizer = ReceiptRecognizer(lambda: ("https://example.invalid", "fixture", "test", 30))
        self.today = date(2026, 11, 23)

    def recognize(self, value, **result):
        """Подставляет один JSON-ответ в публичную операцию распознавания."""
        choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(result)))
        self.call.return_value = SimpleNamespace(choices=[choice])
        return self.recognizer.recognize(value, self.today)

    def test_receipt_allocates_discount_exactly(self):
        item = dict(product="Молоко", category="Продукты", eligible=True, amount=100)
        result = self.recognize(b"\xff\xd8\xfffixture", items=[item] * 3, discount=1, receipt_total=305)
        self.assertEqual(result.source, ExpenseSource.PHOTO)
        self.assertEqual([line.amount for line in result.reconciled_items()], [99, 100, 100, 6])
        self.assertIn("data:image/jpeg;base64,", str(self.call.call_args.kwargs["messages"]))

    def test_text_calculates_line_and_handles_unknown_category(self):
        result = self.recognize("яблоки", items=[dict(product="Яблоки", category="???", quantity="0.375",
                                                     unit_price=20000, amount=None)])
        self.assertEqual(result.purchase_date, self.today)
        self.assertEqual(result.source, ExpenseSource.TEXT)
        self.assertEqual((result.items[0].amount, result.items[0].category), (7500, "Прочее"))

    def test_malformed_and_oversized_numbers(self):
        for item in ({"amount": 100}, {"product": "чай", "category": "Продукты", "amount": 10**99},
                     {"product": "чай", "category": "Продукты", "amount": None, "quantity": "1e99999999"}):
            with self.subTest(item=item), self.assertRaises(ExpenseRecognitionError):
                self.recognize("чай", items=[item])
        with self.assertRaises(ExpenseRecognitionError):
            self.recognize(b"invalid", items=[])

    def test_provider_error_does_not_include_original_input(self):
        self.call.side_effect = RuntimeError("private content")
        with self.assertRaises(ExpenseRecognitionError) as caught:
            self.recognizer.recognize("чай", self.today)
        self.assertNotIn("private", str(caught.exception))
