"""Проверки независимости, границ и ошибок сценария Ask."""

import ast
import importlib.util
from pathlib import Path
import unittest

from obs_chat_bot.capabilities.ask.domain.errors import (
    AskError,
    AskModelError,
    AskModelTimeoutError,
    AskModelUnavailableError,
    EmptyQuestionError,
    InvalidAskAnswerError,
    QuestionTooLongError,
)
from obs_chat_bot.capabilities.ask.domain.ports.model import AskModel
from obs_chat_bot.capabilities.ask.domain.use_cases.ask_question import (
    ASK_INSTRUCTION,
    AskQuestion,
)


class FakeAskModel(AskModel):
    """Фиксирует полное содержимое каждого обращения к port модели."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.response = "Ответ модели"
        self.error: AskModelError | None = None

    def answer(self, *, question: str, instruction: str) -> str:
        """Записывает запрос и возвращает подменный ответ либо ошибку."""
        self.calls.append({"question": question, "instruction": instruction})
        if self.error is not None:
            raise self.error
        return self.response


class AskQuestionTest(unittest.TestCase):
    """Проверяет один вопрос без обращения к API, БД или другим возможностям."""

    def setUp(self) -> None:
        """Создаёт изолированную модель и небольшой лимит длины."""
        self.model = FakeAskModel()
        self.ask = AskQuestion(model=self.model, max_question_chars=64)

    def test_consecutive_questions_have_no_history(self) -> None:
        """Второй запрос не включает первый вопрос и предыдущий ответ."""
        self.assertEqual(self.ask.execute("Первый вопрос"), "Ответ модели")
        self.model.response = "Второй ответ"
        self.assertEqual(self.ask.execute("Второй вопрос"), "Второй ответ")
        self.assertEqual(
            self.model.calls,
            [
                {"question": "Первый вопрос", "instruction": ASK_INSTRUCTION},
                {"question": "Второй вопрос", "instruction": ASK_INSTRUCTION},
            ],
        )

    def test_url_is_passed_as_plain_question_text(self) -> None:
        """Ссылка передаётся модели в вопросе без запуска обработки статьи."""
        question = "Что означает https://example.com/article?"
        self.ask.execute(question)
        self.assertEqual(self.model.calls, [{"question": question, "instruction": ASK_INSTRUCTION}])

    def test_empty_question_does_not_call_model(self) -> None:
        """Пустая строка и Unicode-пробелы отклоняются до вызова port."""
        for question in ("", " ", "\t\r\n", "\u2003\u00a0"):
            with self.subTest(question=question), self.assertRaises(EmptyQuestionError):
                self.ask.execute(question)
        self.assertEqual(self.model.calls, [])

    def test_limit_accepts_exact_length_after_trimming(self) -> None:
        """Граница учитывает Unicode-символы и не включает внешние пробелы."""
        question = "Я🙂" * 32
        self.ask.execute(f" \n{question}\t ")
        self.assertEqual(self.model.calls[0]["question"], question)

    def test_over_limit_does_not_call_model_or_include_question_in_error(self) -> None:
        """Первый лишний символ отклоняется без вызова port и раскрытия текста."""
        question = "Секрет" * 11
        with self.assertRaises(QuestionTooLongError) as raised:
            self.ask.execute(question)
        self.assertIn("64", str(raised.exception))
        self.assertNotIn(question, str(raised.exception))
        self.assertEqual(self.model.calls, [])
        self.ask.execute("Вопрос после ошибки")
        self.assertEqual(len(self.model.calls), 1)

    def test_internal_question_and_answer_formatting_is_preserved(self) -> None:
        """Внутренние переводы строк вопроса и формат ответа сохраняются."""
        self.model.response = "  код\n\n**ответ**\n"
        answer = self.ask.execute("  Вопрос:\n  код  ")
        self.assertEqual(self.model.calls[0]["question"], "Вопрос:\n  код")
        self.assertEqual(answer, self.model.response)

    def test_model_errors_keep_their_types_and_do_not_retry(self) -> None:
        """Ошибки port доступны presentation по типу; автоповтора запроса нет."""
        for error_type in (AskModelTimeoutError, AskModelUnavailableError, InvalidAskAnswerError):
            with self.subTest(error=error_type.__name__):
                model = FakeAskModel()
                model.error = error_type("Безопасное описание")
                ask = AskQuestion(model=model, max_question_chars=64)
                with self.assertRaises(error_type) as raised:
                    ask.execute("Вопрос до ошибки")
                self.assertIs(raised.exception, model.error)
                self.assertEqual(len(model.calls), 1)
                model.error = None
                ask.execute("Следующий вопрос")
                self.assertEqual(model.calls[1], {"question": "Следующий вопрос", "instruction": ASK_INSTRUCTION})

    def test_invalid_model_answer_has_typed_error(self) -> None:
        """Нарушение контракта port не выпускает наружу пустой или нетекстовый ответ."""
        for response in ("", " \n\t", None, 42, {"content": "text"}):
            with self.subTest(response=response):
                self.model.response = response
                with self.assertRaises(InvalidAskAnswerError):
                    self.ask.execute("Вопрос")

    def test_invalid_limit_fails_before_model_call(self) -> None:
        """Ошибочная настройка длины отвергается при сборке сценария."""
        for limit in (0, -1, True, 1.5, "64", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                AskQuestion(model=self.model, max_question_chars=limit)
        self.assertEqual(self.model.calls, [])

    def test_expected_errors_share_ask_base_type(self) -> None:
        """Все ожидаемые ошибки доступны через общий тип возможности."""
        for error_type in (EmptyQuestionError, QuestionTooLongError, AskModelTimeoutError, AskModelUnavailableError, InvalidAskAnswerError):
            with self.subTest(error=error_type.__name__):
                self.assertTrue(issubclass(error_type, AskError))


class AskDomainBoundaryTest(unittest.TestCase):
    """Защищает domain Ask от SDK, хранилищ и других модулей приложения."""

    def test_domain_imports_only_own_domain_and_pure_standard_modules(self) -> None:
        """Разрешены собственный domain и явный набор чистых стандартных модулей."""
        root = Path(__file__).resolve().parents[1]
        domain = root / "obs_chat_bot" / "capabilities" / "ask" / "domain"
        pure_modules = {"__future__", "typing", "dataclasses", "enum", "collections", "abc"}
        for path in domain.rglob("*.py"):
            package = ".".join(path.relative_to(root).parts[:-1])
            with self.subTest(path=str(path.relative_to(root))):
                syntax = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(syntax):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        if node.level:
                            module = importlib.util.resolve_name("." * node.level + module, package)
                        modules = [module]
                    for module in modules:
                        self.assertTrue(
                            module == "obs_chat_bot.capabilities.ask.domain"
                            or module.startswith("obs_chat_bot.capabilities.ask.domain.")
                            or module.split(".")[0] in pure_modules,
                            f"Запрещённая зависимость Ask domain: {module}",
                        )


if __name__ == "__main__":
    unittest.main()
