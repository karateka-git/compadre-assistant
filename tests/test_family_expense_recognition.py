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
        return self._recognize_content(value, json.dumps(result))

    def _recognize_content(self, value, content):
        choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))
        self.call.return_value = SimpleNamespace(choices=[choice])
        return self.recognizer.recognize(value, self.today)

    def test_receipt_in_single_markdown_block(self):
        payload = json.dumps({"items": [{"product": "Чай", "category": "Продукты", "amount": 5000}]})
        for marker in ("```json", "```"):
            with self.subTest(marker=marker):
                result = self._recognize_content(b"\xff\xd8\xfffixture", f"\n{marker}\r\n{payload}\r\n```\n")
                self.assertEqual(result.items[0].amount, 5000)

    def test_rejects_extra_prose_incomplete_blocks_and_invalid_fenced_payloads(self):
        payload = '{"items":[{"product":"Чай","category":"Продукты","amount":5000}]}'
        block = f"```json\n{payload}\n```"
        for content in (f"Чек:\n{block}", f"{block}\nГотово.", f"{block}\n{block}",
                        f"```json\n{payload}", f"```python\n{payload}\n```",
                        "```json\n{\n```", '```json\n{"items":[]}\n```'):
            with self.subTest(content=content), self.assertRaises(ExpenseRecognitionError):
                self._recognize_content("чай", content)

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

    def test_receipt_summary_discount_already_in_line_totals(self):
        """Повтор скидки внизу чека не меняет оплаченные суммы или категории."""
        items = [
            dict(product="Печенье", category="Продукты", amount=7199, eligible=True,
                 package_size="224 г", amount_before_discount=11399),
            dict(product="Губка для посуды", category="Дом и быт", amount=11599, eligible=True),
            dict(product="Соль", category="Продукты", amount=9999),
            dict(product="Бедро куриное", category="Продукты", amount=26879),
        ]
        purchase = self.recognize(b"\xff\xd8\xfffixture", items=items, discount=7600, receipt_total=55676)
        self.assertEqual([line.amount for line in purchase.reconciled_items()], [7199, 11599, 9999, 26879])
        self.assertEqual((purchase.items[0].package_size, purchase.items[0].amount_before_discount), ("224 г", 11399))

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
