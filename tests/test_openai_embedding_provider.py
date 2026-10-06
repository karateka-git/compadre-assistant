"""Тесты OpenAI-compatible embedding adapter Этапа 10.4."""

from types import SimpleNamespace
from decimal import Decimal
import unittest
import json

from obs_chat_bot.capabilities.obsidian.application.search.errors import EmbeddingProviderError
from obs_chat_bot.capabilities.obsidian.application.search.models import EmbeddingCallContext
from obs_chat_bot.capabilities.obsidian.data.embeddings.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)


class _FakeEmbeddingsResource:
    """Возвращает детерминированные SDK-подобные ответы."""

    def __init__(self, responder=None):
        self.calls = []
        self._responder = responder or self._default_response

    def create(self, **values):
        self.calls.append(values)
        response = self._responder(values)
        payload = json.loads(json.dumps(response, default=lambda value: vars(value)))
        return SimpleNamespace(http_response=SimpleNamespace(
            status_code=200,
            headers={"x-request-id": getattr(response, "_request_id", None)},
            json=lambda: payload,
        ))

    @property
    def with_raw_response(self):
        """Имитирует SDK raw-response resource без выполнения сети."""
        return self

    @staticmethod
    def _default_response(values):
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[float(index), 1.0, 2.0])
                for index, _text in enumerate(values["input"])
            ]
        )


class OpenAICompatibleEmbeddingProviderTest(unittest.TestCase):
    """Проверяет batching, порядок, размерность и ошибки provider."""

    def test_default_batches_multiple_inputs_in_one_request(self) -> None:
        """Default отправляет небольшой corpus одним пакетным запросом."""
        resource = _FakeEmbeddingsResource()
        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://embeddings.example/v1",
            api_key="secret",
            document_model="test/doc-model",
            query_model="test/query-model",
            client=SimpleNamespace(embeddings=resource),
        )

        _collect(provider, ("one", "two"))

        self.assertEqual(
            [call["input"] for call in resource.calls],
            [["one", "two"]],
        )

    def test_embed_documents_batches_and_preserves_input_order(self) -> None:
        """Большой corpus делится на запросы без перестановки vectors."""
        resource = _FakeEmbeddingsResource(
            responder=lambda values: SimpleNamespace(
                data=list(
                    reversed(
                        [
                            SimpleNamespace(
                                index=index,
                                embedding=[float(len(text)), 1.0],
                            )
                            for index, text in enumerate(values["input"])
                        ]
                    )
                )
            )
        )
        provider = _provider(resource, batch_size=2)

        vectors = _collect(provider, ("one", "four", "five"))

        self.assertEqual(len(resource.calls), 2)
        self.assertEqual([vector.values[0] for vector in vectors], [3.0, 4.0, 4.0])
        self.assertEqual({vector.model for vector in vectors}, {"test/doc-model"})
        self.assertEqual({vector.dimension for vector in vectors}, {2})

    def test_embed_query_uses_query_model_and_validates_vector(self) -> None:
        """Query векторизуется отдельной моделью парного vector space."""
        resource = _FakeEmbeddingsResource()
        provider = _provider(resource)

        vector = provider.embed_query("semantic query")

        self.assertEqual(vector.model, "test/query-model")
        self.assertEqual(vector.dimension, 3)
        self.assertEqual(resource.calls[0]["model"], "test/query-model")
        self.assertEqual(resource.calls[0]["input"], ["semantic query"])
        self.assertEqual(resource.calls[0]["encoding_format"], "float")

    def test_document_and_query_dimensions_must_be_compatible(self) -> None:
        """Парные модели не могут возвращать vectors разной dimension."""
        resource = _FakeEmbeddingsResource(
            responder=lambda values: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        index=0,
                        embedding=(
                            [1.0, 2.0]
                            if values["model"] == "test/doc-model"
                            else [1.0, 2.0, 3.0]
                        ),
                    )
                ]
            )
        )
        provider = _provider(resource)

        _collect(provider, ("document",))

        with self.assertRaisesRegex(EmbeddingProviderError, "incompatible"):
            provider.embed_query("query")

    def test_empty_document_tuple_does_not_call_provider(self) -> None:
        """Отсутствие новых chunks не создаёт платный внешний запрос."""
        resource = _FakeEmbeddingsResource()
        provider = _provider(resource)

        self.assertEqual(_collect(provider, ()), ())
        self.assertEqual(resource.calls, [])

    def test_empty_text_is_rejected_before_request(self) -> None:
        """Пустой chunk или query считается ошибкой caller."""
        resource = _FakeEmbeddingsResource()
        provider = _provider(resource)

        with self.assertRaises(ValueError):
            _collect(provider, ("valid", "  "))
        with self.assertRaises(ValueError):
            provider.embed_query("")

        self.assertEqual(resource.calls, [])

    def test_unexpected_indices_are_rejected(self) -> None:
        """Нельзя связать vectors с chunks при неполных response indices."""
        resource = _FakeEmbeddingsResource(
            responder=lambda _values: SimpleNamespace(
                data=[SimpleNamespace(index=1, embedding=[1.0])]
            )
        )

        with self.assertRaisesRegex(EmbeddingProviderError, "unexpected format"):
            _provider(resource).embed_query("query")

    def test_inconsistent_dimensions_are_rejected(self) -> None:
        """Один batch не может содержать несовместимые vector dimensions."""
        resource = _FakeEmbeddingsResource(
            responder=lambda _values: SimpleNamespace(
                data=[
                    SimpleNamespace(index=0, embedding=[1.0]),
                    SimpleNamespace(index=1, embedding=[1.0, 2.0]),
                ]
            )
        )

        with self.assertRaisesRegex(EmbeddingProviderError, "inconsistent"):
            _collect(_provider(resource), ("one", "two"))

    def test_network_error_exposes_only_error_type(self) -> None:
        """Application error не содержит исходные тексты или credentials."""
        def fail(_values):
            raise RuntimeError("provider detail")

        provider = _provider(_FakeEmbeddingsResource(responder=fail))

        with self.assertRaises(EmbeddingProviderError) as raised:
            provider.embed_query("private vault text")

        self.assertEqual(
            str(raised.exception),
            "Embedding request failed: RuntimeError",
        )
        self.assertNotIn("private vault text", str(raised.exception))

    def test_logs_scoped_usage_and_estimated_cost_without_input_text(self) -> None:
        """Успешный SDK response даёт анализируемые usage/cost логи без содержимого."""
        private_text = "private vault text that must not reach logs"
        resource = _FakeEmbeddingsResource(
            responder=lambda _values: SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[1.0, 2.0])],
                usage=SimpleNamespace(prompt_tokens=100, total_tokens=100),
                _request_id="request-42",
            )
        )
        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://embeddings.example/v1",
            api_key="secret",
            document_model="test/doc-model",
            query_model="test/query-model",
            price_per_million_tokens=Decimal("3"),
            price_currency="RUB",
            tariff_version="2026-08-04",
            client=SimpleNamespace(embeddings=resource),
        )
        context = EmbeddingCallContext(
            app_user_id=7,
            vault_id=9,
            article_id=11,
            operation_id="operation-42",
        )

        with self.assertLogs(
            "obs_chat_bot.capabilities.obsidian.data.embeddings.openai_compatible",
            level="INFO",
        ) as captured:
            provider.embed_query(private_text, context=context)

        logs = "\n".join(captured.output)
        self.assertIn("event=embedding_operation_started", logs)
        self.assertIn("event=embedding_request_completed", logs)
        self.assertIn("event=embedding_operation_completed", logs)
        self.assertIn("operation_id=operation-42", logs)
        self.assertIn("app_user_id=7 vault_id=9 article_id=11", logs)
        self.assertIn("input_tokens=100 total_tokens=100", logs)
        self.assertIn("estimated_cost=0.0003 currency=RUB", logs)
        self.assertIn("tariff_version=2026-08-04", logs)
        self.assertIn("request_id=request-42", logs)
        self.assertNotIn(private_text, logs)
        self.assertNotIn("secret", logs)

    def test_document_operation_aggregates_usage_across_batches(self) -> None:
        """Operation completion суммирует usage всех SDK batches один раз."""
        resource = _FakeEmbeddingsResource(
            responder=lambda values: SimpleNamespace(
                data=[
                    SimpleNamespace(index=index, embedding=[1.0, 2.0])
                    for index, _text in enumerate(values["input"])
                ],
                usage=SimpleNamespace(
                    prompt_tokens=10 * len(values["input"]),
                    total_tokens=10 * len(values["input"]),
                ),
            )
        )
        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://embeddings.example/v1",
            api_key="secret",
            document_model="test/doc-model",
            query_model="test/query-model",
            batch_size=2,
            price_per_million_tokens=Decimal("3"),
            price_currency="RUB",
            tariff_version="current",
            client=SimpleNamespace(embeddings=resource),
        )

        with self.assertLogs(
            "obs_chat_bot.capabilities.obsidian.data.embeddings.openai_compatible",
            level="INFO",
        ) as captured:
            _collect(provider, ("one", "two", "three"))

        completed = next(
            line
            for line in captured.output
            if "event=embedding_operation_completed" in line
        )
        self.assertIn("batches=2 usage_batches=2", completed)
        self.assertIn("input_tokens=30 total_tokens=30", completed)
        self.assertIn("estimated_cost=0.00009", completed)

    def test_missing_usage_keeps_cost_unknown(self) -> None:
        """Provider без usage не ломает запрос и не создаёт вымышленную цену."""
        provider = _provider(_FakeEmbeddingsResource())

        with self.assertLogs(
            "obs_chat_bot.capabilities.obsidian.data.embeddings.openai_compatible",
            level="INFO",
        ) as captured:
            provider.embed_query("semantic query")

        completed = next(
            line
            for line in captured.output
            if "event=embedding_operation_completed" in line
        )
        self.assertIn("usage_batches=0", completed)
        self.assertIn("input_tokens=none total_tokens=none", completed)
        self.assertIn("estimated_cost=none", completed)

    def test_failure_logs_types_but_not_provider_detail_or_input(self) -> None:
        """Финальный сбой остаётся диагностируемым без утечки exception message."""
        private_text = "private note body"
        provider_detail = "upstream leaked key api-key-123"

        def fail(_values):
            error = RuntimeError(provider_detail)
            error.status_code = 503
            error.request_id = "failed-request-42"
            raise error

        provider = _provider(_FakeEmbeddingsResource(responder=fail))

        with self.assertLogs(
            "obs_chat_bot.capabilities.obsidian.data.embeddings.openai_compatible",
            level="ERROR",
        ) as captured:
            with self.assertRaises(EmbeddingProviderError):
                provider.embed_query(private_text)

        logs = "\n".join(captured.output)
        self.assertIn("event=embedding_request_failed", logs)
        self.assertIn("event=embedding_operation_failed", logs)
        self.assertIn("error_type=RuntimeError", logs)
        self.assertIn("cause_type=RuntimeError", logs)
        self.assertIn("sdk_max_retries=2", logs)
        self.assertIn("http_status=503", logs)
        self.assertIn("request_id=failed-request-42", logs)
        self.assertNotIn(private_text, logs)
        self.assertNotIn(provider_detail, logs)


def _collect(provider, texts):
    """Собирает пакетный результат только для проверок всего corpus в тестах."""
    return tuple(vector for batch in provider.iter_document_batches(texts) for vector in batch)


def _provider(
    resource: _FakeEmbeddingsResource,
    *,
    batch_size: int = 64,
) -> OpenAICompatibleEmbeddingProvider:
    """Собирает adapter с fake SDK-клиентом."""
    return OpenAICompatibleEmbeddingProvider(
        base_url="https://embeddings.example/v1",
        api_key="secret",
        document_model="test/doc-model",
        query_model="test/query-model",
        batch_size=batch_size,
        client=SimpleNamespace(embeddings=resource),
    )


if __name__ == "__main__":
    unittest.main()
