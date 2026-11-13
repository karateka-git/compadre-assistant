"""Проверки профиля, API adapter и жизненного цикла Ask без внешней сети."""

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import traceback
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from compadre_assistant.capabilities.ask.bootstrap import create_ask_runtime
from compadre_assistant.capabilities.ask.data.configuration import AskConfig, AskConfigError, load_ask_config
from compadre_assistant.capabilities.ask.data.llm.openai_ask_model import OpenAIAskModel
from compadre_assistant.capabilities.ask.domain.errors import (
    AskModelTimeoutError, AskModelUnavailableError, InvalidAskAnswerError, QuestionTooLongError,
)
from compadre_assistant.capabilities.ask.domain.use_cases.ask_question import ASK_INSTRUCTION, AskQuestion
from compadre_assistant.capabilities.ask.domain.usage import TokenUsage, UsageRecorder


def _config() -> AskConfig:
    return AskConfig(base_url="https://ask.example/v1", api_key="ask-secret", model="ask-model")


def _tariff_fields(**overrides):
    return {
        "provider": "test-gateway", "model": "actual-model", "source": "synthetic example",
        "effective_from": "2026-01-01T00:00:00Z", "prompt_rate": 11, "completion_rate": 13,
        **overrides,
    }


def _with_tariffs(raw: str) -> AskConfig:
    return load_ask_config({
        "ASK_BASE_URL": "https://ask.example/v1", "ASK_API_KEY": "ask-secret", "ASK_MODEL": "ask-model",
        "ASK_USAGE_PROVIDER": "test-gateway", "ASK_USAGE_TARIFFS_JSON": raw,
    })


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

    def test_loads_versioned_tariff_history_and_optional_rules(self) -> None:
        """Смежные интервалы принимаются с точными ставками и правилами детализации."""
        config = _with_tariffs(json.dumps({"version": 1, "tariffs": [
            _tariff_fields(effective_until="2026-02-01T00:00:00Z", image_rate=1, cached_rate=0,
                           unpriced_details=["reasoning"], image_cache_disjoint=True),
            _tariff_fields(effective_from="2026-02-01T00:00:00+00:00", prompt_rate=7),
        ]}))
        self.assertEqual(config.usage_provider, "test-gateway")
        earlier, later = config.usage_tariffs
        self.assertEqual(earlier.effective_until, later.effective_from)
        self.assertEqual((earlier.cached_rate, earlier.image_rate, later.prompt_rate), (0, 1, 7))
        self.assertEqual(earlier.unpriced_details, {"reasoning"})
        self.assertTrue(earlier.image_cache_disjoint)
        self.assertEqual(_with_tariffs("").usage_tariffs, ())

    def test_rejects_invalid_catalogs_without_logging_payload(self) -> None:
        """Предупреждает об ошибках схемы, правил, диапазонов и истории без значений JSON."""
        entries = [
            _tariff_fields(effective_from="2026-01-01T00:00:00+01:00"),
            _tariff_fields(effective_until="2025-01-01T00:00:00Z"),
            _tariff_fields(prompt_rate=True), _tariff_fields(prompt_rate=2**63),
            _tariff_fields(completion_rate=-1), _tariff_fields(image_cache_disjoint="true"),
            _tariff_fields(unpriced_details=["sensitive-value"]),
            _tariff_fields(unpriced_details=["cached", "cached"]),
            _tariff_fields(cached_rate=0, unpriced_details=["cached"]), _tariff_fields(unknown=0),
        ]
        catalogs = [json.dumps({"version": 1, "tariffs": [entry]}) for entry in entries]
        catalogs.extend((
            "sensitive-value", '{"version":1,"version":1,"tariffs":[]}',
            '{"version":true,"tariffs":[]}', '{"version":2,"tariffs":[]}',
            json.dumps({"version": 1, "tariffs": [_tariff_fields(), _tariff_fields()]}),
        ))
        for catalog in catalogs:
            with self.subTest(catalog=catalog), self.assertRaises(AskConfigError) as caught:
                _with_tariffs(catalog)
            self.assertNotIn("sensitive-value", "".join(traceback.format_exception(caught.exception)))


class AskApiTest(unittest.TestCase):
    """Использует настоящий OpenAI SDK с подменным HTTP transport."""

    def _run(self, handler, *, config=None, questions=("Вопрос",), recorder=None):
        profile = config or _config()
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            with OpenAI(base_url=profile.base_url, api_key=profile.api_key, max_retries=0,
                        http_client=http_client) as client:
                adapter = OpenAIAskModel(profile, client=client,
                                         usage_recorder=recorder if recorder is not None else Mock(spec=UsageRecorder))
                use_case = AskQuestion(model=adapter, max_question_chars=profile.max_question_chars)
                return [use_case.execute(question) for question in questions]

    def test_requests_are_independent_and_use_ask_profile(self) -> None:
        """На проводе видны только инструкция и текущий вопрос, без tools."""
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Ответ"}}]})

        recorder = Mock(spec=UsageRecorder)
        self.assertEqual(self._run(handler, recorder=recorder, questions=("Первый", "https://example.com/второй")),
                         ["Ответ", "Ответ"])
        first, second = (call.args[0] for call in recorder.submit.call_args_list)
        self.assertNotEqual(first.call_id, second.call_id)
        self.assertEqual(first.provider, "ask")
        self.assertIsNone(first.actual_model)
        self.assertIsNone(first.tokens)
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
            recorder = Mock(spec=UsageRecorder)
            with self.subTest(timeout=timeout), self.assertRaises(expected) as caught:
                self._run(handler, recorder=recorder)
            formatted = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn("provider-secret", formatted)
            self.assertNotIn("ask-secret", formatted)
            self.assertEqual(len(calls), 1)
            observation = recorder.submit.call_args.args[0]
            self.assertEqual(recorder.submit.call_count, 1)
            self.assertIsNone(observation.tokens)
            self.assertIsNone(observation.actual_model)

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
        recorder = Mock(spec=UsageRecorder)
        with self.assertRaises(QuestionTooLongError):
            self._run(lambda request: self.fail("Неожиданный HTTP запрос"),
                      config=replace(_config(), max_question_chars=2), recorder=recorder)
        recorder.submit.assert_not_called()

    def test_reads_provider_extensions_and_keeps_valid_partial_usage(self) -> None:
        """Пустые choices не отменяют запись; дополнительные поля сохраняются даже при неполных счётчиках."""
        recorder = Mock(spec=UsageRecorder)
        payload = {"model": "fallback", "choices": [], "usage": {
            "prompt_tokens": 30, "completion_tokens": 7, "total_tokens": 37,
            "prompt_tokens_details": {"text_tokens": 19, "image_tokens": 11, "cached_tokens": 0},
            "completion_tokens_details": {"text_tokens": -1, "reasoning_tokens": 2},
        }}
        started = datetime.now(UTC)
        with self.assertRaises(InvalidAskAnswerError):
            self._run(lambda _: httpx.Response(200, json=payload), recorder=recorder)
        observation = recorder.submit.call_args.args[0]
        self.assertEqual(observation.actual_model, "fallback")
        self.assertGreaterEqual(observation.started_at, started)
        self.assertLessEqual(observation.started_at, datetime.now(UTC))
        self.assertEqual(observation.tokens, TokenUsage(
            prompt_tokens=30, completion_tokens=7, total_tokens=37, prompt_text_tokens=19,
            image_tokens=11, cached_tokens=0, reasoning_tokens=2,
        ))

    def test_submission_error_does_not_change_the_answer(self) -> None:
        """Сбой передачи наблюдения диагностируется отдельно от результата модели."""
        recorder = Mock(spec=UsageRecorder)
        recorder.submit.side_effect = RuntimeError("sensitive-value")
        with self.assertLogs("compadre_assistant.capabilities.ask.data.llm.openai_ask_model", "ERROR") as logs:
            self.assertEqual(self._run(lambda _: httpx.Response(200, json={
                "choices": [{"message": {"content": "Ответ"}}],
            }), recorder=recorder), ["Ответ"])
        self.assertEqual([record.getMessage() for record in logs.records], [
            "AI usage observation failed: error_type=RuntimeError",
        ])


class AskBootstrapTest(unittest.TestCase):
    """Проверяет отключение и освобождение принадлежащего Ask клиента."""

    def test_disabled_does_not_load_profile_or_construct_client(self) -> None:
        """Отключённая возможность не обращается даже к loader настроек."""
        with patch("compadre_assistant.capabilities.ask.bootstrap.load_ask_config") as loader, \
                patch("compadre_assistant.capabilities.ask.bootstrap.OpenAIAskModel") as model:
            with create_ask_runtime(enabled=False, database_path=Path("unused.db")) as runtime:
                self.assertIsNone(runtime)
            loader.assert_not_called()
            model.assert_not_called()

    def test_enabled_runtime_configures_sdk_and_closes_on_failure(self) -> None:
        """SDK получает отдельные настройки, без повторов и с гарантированным close."""
        with TemporaryDirectory() as temporary, \
                patch("compadre_assistant.capabilities.ask.data.llm.openai_ask_model.OpenAI") as sdk:
            with self.assertRaisesRegex(RuntimeError, "consumer"):
                with create_ask_runtime(enabled=True, config=_config(), database_path=Path(temporary) / "app.db") as runtime:
                    self.assertIsInstance(runtime, AskQuestion)
                    raise RuntimeError("consumer")
            sdk.assert_called_once_with(base_url="https://ask.example/v1", api_key="ask-secret",
                                        timeout=60.0, max_retries=0)
            sdk.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
