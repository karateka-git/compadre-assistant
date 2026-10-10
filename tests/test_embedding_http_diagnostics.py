"""Проверки реального OpenAI SDK через HTTP mock без сети и платных запросов."""

import json
import unittest
from unittest.mock import patch

try:
    import httpx
    from openai import OpenAI
except ImportError:
    httpx = None
    OpenAI = None

from compadre_assistant.capabilities.obsidian.domain.search.errors import EmbeddingProviderError
from compadre_assistant.capabilities.obsidian.data.embeddings.openai_compatible import OpenAICompatibleEmbeddingProvider

LOGGER = "compadre_assistant.capabilities.obsidian.data.embeddings.openai_compatible"


@unittest.skipIf(OpenAI is None, "SDK проверяется в Docker с runtime-зависимостями")
class EmbeddingHttpDiagnosticsTest(unittest.TestCase):
    """Отличает malformed 200, HTTP и transport failures с сохранением SDK retries."""

    def _provider(self, handler, *, batch_size=2):
        """Создаёт настоящий SDK с контролируемым HTTP transport."""
        client = OpenAI(base_url="https://provider.example/v1", api_key="private-api-key",
                        max_retries=2, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        return OpenAICompatibleEmbeddingProvider(
            base_url="https://provider.example/v1", api_key="private-api-key",
            document_model="doc-model", query_model="query-model", batch_size=batch_size,
            client=client,
        )

    def test_batches_are_lazy_and_malformed_last_response_preserves_prior_results(self):
        """SDK raw path даёт status/request ID и не запрашивает следующий batch заранее."""
        calls = []
        def handler(request):
            data = json.loads(request.content)
            calls.append(data["input"])
            if len(calls) == 3:
                return httpx.Response(200, json={"data": None, "secret": "private-provider-body"},
                                      headers={"x-request-id": "malformed-42"})
            return httpx.Response(200, json={"data": [
                {"index": i, "embedding": [1.0, 2.0]} for i in range(len(data["input"]))
            ]})
        provider = self._provider(handler)
        batches = provider.iter_document_batches(("one", "two", "three", "four", "private-chunk"))
        self.assertEqual(calls, [])
        first = next(batches)
        self.assertEqual(len(calls), 1)
        second = next(batches)
        self.assertEqual(len(calls), 2)
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            with self.assertRaises(EmbeddingProviderError) as raised:
                next(batches)
        self.assertEqual(len(first) + len(second), 4)
        self.assertIsInstance(raised.exception.__cause__, ValueError)
        logs = "\n".join(captured.output)
        for field in ("status=invalid_response", "http_status=200", "request_id=malformed-42",
                      "batch_index=3", "batch_size=1", "expected_vectors=1",
                      "actual_vectors=unknown", "data_present=1", "data_type=NoneType",
                      "completed_batches=2"):
            self.assertIn(field, logs)
        for secret in ("private-chunk", "private-api-key", "private-provider-body"):
            self.assertNotIn(secret, logs)

    def test_malformed_json_shapes_are_rejected_without_sdk_coercion(self):
        """Не принимаются отсутствующие поля, bool/string числа, NaN и неверный порядок."""
        cases = [
            ({}, "data_present=0"),
            ([], "response_type=list"),
            ({"data": "private-provider-body"}, "data_type=str"),
            ({"data": []}, "actual_vectors=0"),
            ({"data": [None]}, "item_types=NoneType"),
            ({"data": [{"index": 0}]}, "embedding_present_count=0"),
            ({"data": [{"embedding": [1.0]}]}, "index_present_count=0"),
            ({"data": [{"index": True, "embedding": [1.0]}]}, "index_types=bool"),
            ({"data": [{"index": "0", "embedding": [1.0]}]}, "index_types=str"),
            ({"data": [{"index": 0, "embedding": ["private-provider-body"]}]}, "value_types=str"),
            ({"data": [{"index": 0, "embedding": [True]}]}, "value_types=bool"),
            ({"data": [{"index": 0, "embedding": []}]}, "value_types=none"),
            ({"data": [{"index": 3, "embedding": [1.0]}]}, "index_types=int"),
            ({"data": [{"index": 0, "embedding": [float("nan")]}]}, "value_types=float"),
            ({"data": [{"index": 0, "embedding": [10**400]}]}, "value_types=int"),
        ]
        for payload, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                provider = self._provider(lambda request: httpx.Response(
                    200, content=json.dumps(payload), headers={"x-request-id": "shape-42"}))
                with self.assertLogs(LOGGER, level="ERROR") as captured:
                    with self.assertRaises(EmbeddingProviderError):
                        provider.embed_query("private-chunk")
                logs = "\n".join(captured.output)
                self.assertIn(diagnostic, logs)
                self.assertIn("http_status=200", logs)
                self.assertIn("request_id=shape-42", logs)
                for secret in ("private-provider-body", "private-chunk", "private-api-key"):
                    self.assertNotIn(secret, logs)

    def test_invalid_json_retains_http_metadata_and_parse_cause(self):
        """Обрезанный JSON — malformed 200, а не потерянная транспортная ошибка."""
        provider = self._provider(lambda request: httpx.Response(
            200, content="private-provider-body", headers={"x-request-id": "parse-42"}))
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            with self.assertRaises(EmbeddingProviderError) as raised:
                provider.embed_query("private-chunk")
        logs = "\n".join(captured.output)
        self.assertIn("http_status=200", logs)
        self.assertIn("request_id=parse-42", logs)
        self.assertIn("cause_type=JSONDecodeError", logs)
        self.assertIsInstance(raised.exception.__cause__, ValueError)
        self.assertNotIn("private-provider-body", logs)

    def test_http_and_transport_failures_keep_sdk_retries_and_distinct_diagnostics(self):
        """Три HTTP-попытки SDK дают один batch failure с глубинной причиной."""
        for kind in ("http", "timeout", "connection"):
            with self.subTest(kind=kind):
                calls = []
                def handler(request):
                    calls.append(request)
                    if kind == "http":
                        return httpx.Response(503, json={"error": {"message": "private-provider-body"}},
                                              headers={"x-request-id": "failed-42"})
                    if kind == "timeout":
                        raise httpx.ReadTimeout("private-provider-body", request=request)
                    raise httpx.ConnectError("private-provider-body", request=request)
                provider = self._provider(handler)
                with patch("time.sleep"), self.assertLogs(LOGGER, level="ERROR") as captured:
                    with self.assertRaises(EmbeddingProviderError):
                        provider.embed_query("private-chunk")
                self.assertEqual(len(calls), 3)
                logs = "\n".join(captured.output)
                if kind == "http":
                    self.assertIn("status=http_error", logs)
                    self.assertIn("http_status=503", logs)
                    self.assertIn("request_id=failed-42", logs)
                else:
                    self.assertIn("status=transport_error", logs)
                    self.assertIn("cause_type=" + ("ReadTimeout" if kind == "timeout" else "ConnectError"), logs)
                for secret in ("private-provider-body", "private-chunk", "private-api-key"):
                    self.assertNotIn(secret, logs)
