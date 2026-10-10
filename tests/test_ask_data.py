"""Проверки профиля, API adapter и жизненного цикла Ask без внешней сети."""

from dataclasses import replace
import json
import traceback
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from compadre_assistant.capabilities.ask.bootstrap import create_ask_runtime
from compadre_assistant.capabilities.ask.data.configuration import AskConfig, AskConfigError, load_ask_config
from compadre_assistant.capabilities.ask.data.llm.openai_ask_model import OpenAIAskModel
from compadre_assistant.capabilities.ask.domain.errors import (
    AskModelTimeoutError, AskModelUnavailableError, InvalidAskAnswerError, QuestionTooLongError,
)
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import ASK_INSTRUCTION, AskQuestion


def _config() -> AskConfig:
    return AskConfig(base_url="https://ask.example/v1", api_key="ask-secret", model="ask-model")


class AskConfigurationTest(unittest.TestCase):
    """Проверяет независимость профиля и безопасную валидацию."""

    def test_ask_ignores_obsidian_and_uses_own_settings(self) -> None:
        """Все параметры Ask берутся из собственного пространства имён."""
        config = load_ask_config({
            "ASK_BASE_URL": "https://ask.example/v1", "ASK_API_KEY": "ask-secret",
            "ASK_MODEL": "ask-model", "ASK_TIMEOUT_SECONDS": "9.5",
            "ASK_MAX_QUESTION_CHARS": "123", "ASK_MAX_RESPONSE_TOKENS": "45",
            "ASK_MAX_RESPONSE_CHARS": "678", "OPENAI_API_KEY": "other-secret",
            "OPENAI_MODEL": "other-model", "OBSIDIAN_EMBEDDING_DIMENSIONS": "broken",
        })
        self.assertEqual(config, replace(_config(), timeout_seconds=9.5,
                         max_question_chars=123, max_response_tokens=45, max_response_chars=678))
        self.assertNotIn("ask-secret", repr(config))

    def test_obsidian_credentials_cannot_supply_missing_ask_profile(self) -> None:
        """Глобальный ключ не заменяет обязательные настройки Ask."""
        with self.assertRaises(AskConfigError):
            load_ask_config({"OPENAI_API_KEY": "other-secret", "OPENAI_MODEL": "other"})

    def test_invalid_limits_and_endpoints_are_rejected_without_values(self) -> None:
        """Нечисловые, бесконечные и отрицательные лимиты не доходят до SDK."""
        for field, value in (("timeout_seconds", float("nan")), ("timeout_seconds", float("inf")),
                             ("timeout_seconds", 0), ("max_question_chars", True),
                             ("max_response_tokens", -1), ("max_response_chars", 1.5),
                             ("base_url", "https://user:secret@example.com"),
                             ("base_url", "file:///secret"), ("model", " "), ("api_key", "")):
            with self.subTest(field=field, value=value), self.assertRaises(AskConfigError):
                replace(_config(), **{field: value})
        with self.assertRaises(AskConfigError) as caught:
            load_ask_config({"ASK_TIMEOUT_SECONDS": "sensitive-value"})
        self.assertNotIn("sensitive-value", "".join(traceback.format_exception(caught.exception)))


class AskApiTest(unittest.TestCase):
    """Использует настоящий OpenAI SDK с подменным HTTP transport."""

    def _run(self, handler, *, config=None, questions=("Вопрос",)):
        profile = config or _config()
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            with OpenAI(base_url=profile.base_url, api_key=profile.api_key, max_retries=0,
                        http_client=http_client) as client:
                adapter = OpenAIAskModel(profile, client=client)
                use_case = AskQuestion(model=adapter, max_question_chars=profile.max_question_chars)
                return [use_case.execute(question) for question in questions]

    def test_requests_are_independent_and_use_ask_profile(self) -> None:
        """На проводе видны только инструкция и текущий вопрос, без tools."""
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Ответ"}}]})

        self.assertEqual(self._run(handler, questions=("Первый", "https://example.com/второй")),
                         ["Ответ", "Ответ"])
        for request, question in zip(requests, ("Первый", "https://example.com/второй")):
            body = json.loads(request.content)
            self.assertEqual(body, {"model": "ask-model", "max_tokens": 2000, "messages": [
                {"role": "system", "content": ASK_INSTRUCTION}, {"role": "user", "content": question}]})
            self.assertEqual(str(request.url), "https://ask.example/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer ask-secret")
            self.assertEqual(request.extensions["timeout"]["read"], 60)

    def test_timeout_and_api_errors_do_not_expose_provider_details_or_retry(self) -> None:
        """Тип ошибки сохранён, тело провайдера исключено из traceback."""
        for timeout in (True, False):
            calls = []

            def handler(request):
                calls.append(request)
                if timeout:
                    raise httpx.ReadTimeout("provider-secret", request=request)
                return httpx.Response(500, json={"error": {"message": "provider-secret"}})

            expected = AskModelTimeoutError if timeout else AskModelUnavailableError
            with self.subTest(timeout=timeout), self.assertRaises(expected) as caught:
                self._run(handler)
            formatted = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn("provider-secret", formatted)
            self.assertNotIn("ask-secret", formatted)
            self.assertEqual(len(calls), 1)

    def test_empty_malformed_and_oversized_answers_are_rejected(self) -> None:
        """Некорректный payload не передаётся вызывающему коду."""
        for payload in ({}, {"choices": []}, {"choices": None},
                        {"choices": [{"message": {"content": None}}]},
                        {"choices": [{"message": {"content": " "}}]},
                        {"choices": [{"message": {"content": "x" * 11}}]}):
            with self.subTest(payload=payload), self.assertRaises(InvalidAskAnswerError):
                self._run(lambda request: httpx.Response(200, json=payload),
                          config=replace(_config(), max_response_chars=10))

    def test_long_question_never_reaches_http(self) -> None:
        """Настроенный лимит вопроса действует до сетевого обращения."""
        with self.assertRaises(QuestionTooLongError):
            self._run(lambda request: self.fail("Неожиданный HTTP запрос"),
                      config=replace(_config(), max_question_chars=2))


class AskBootstrapTest(unittest.TestCase):
    """Проверяет отключение и освобождение принадлежащего Ask клиента."""

    def test_disabled_does_not_load_profile_or_construct_client(self) -> None:
        """Отключённая возможность не обращается даже к loader настроек."""
        with patch("compadre_assistant.capabilities.ask.bootstrap.load_ask_config") as loader, \
                patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model:
            with create_ask_runtime(enabled=False) as runtime:
                self.assertIsNone(runtime)
            loader.assert_not_called()
            model.assert_not_called()

    def test_enabled_runtime_configures_sdk_and_closes_on_failure(self) -> None:
        """SDK получает отдельные настройки, без повторов и с гарантированным close."""
        with patch("compadre_assistant.capabilities.ask.data.llm.openai_ask_model.OpenAI") as sdk:
            with self.assertRaisesRegex(RuntimeError, "consumer"):
                with create_ask_runtime(enabled=True, config=_config()) as runtime:
                    self.assertIsInstance(runtime, AskQuestion)
                    raise RuntimeError("consumer")
            sdk.assert_called_once_with(base_url="https://ask.example/v1", api_key="ask-secret",
                                        timeout=60.0, max_retries=0)
            sdk.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
