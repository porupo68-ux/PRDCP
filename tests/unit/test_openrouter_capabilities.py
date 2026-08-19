from __future__ import annotations

import unittest

from common.models.errors import ProviderCapabilityError
from providers.openrouter_capabilities import (
    ModelCapabilityStatus,
    OpenRouterModelCapabilityClient,
)


class OpenRouterCapabilityTests(unittest.TestCase):
    def _client(self) -> OpenRouterModelCapabilityClient:
        responses = {
            "https://openrouter.ai/api/v1/models": {
                "data": [
                    {
                        "id": "good/model",
                        "links": {"details": "/api/v1/models/good/model/endpoints"},
                    },
                    {
                        "id": "bad/model",
                        "links": {"details": "/api/v1/models/bad/model/endpoints"},
                    },
                    {
                        "id": "~vendor/latest",
                        "alias_target": {"slug": "good/model"},
                        "links": {"details": "/api/v1/models/~vendor/latest/endpoints"},
                    },
                ]
            },
            "https://openrouter.ai/api/v1/models/good/model/endpoints": {
                "data": {
                    "endpoints": [
                        {
                            "status": 0,
                            "supported_parameters": [
                                "response_format",
                                "structured_outputs",
                            ],
                        }
                    ]
                }
            },
            "https://openrouter.ai/api/v1/models/bad/model/endpoints": {
                "data": {
                    "endpoints": [
                        {
                            "status": 0,
                            "supported_parameters": ["temperature"],
                        }
                    ]
                }
            },
        }

        def fetch(url: str, _timeout: int) -> dict:
            return responses[url]

        return OpenRouterModelCapabilityClient(fetch_json=fetch)

    def test_direct_model_requires_one_live_endpoint_with_both_parameters(self) -> None:
        result = self._client().inspect("good/model")
        self.assertIs(result.status, ModelCapabilityStatus.COMPATIBLE)
        self.assertEqual(result.compatible_endpoint_count, 1)

    def test_alias_is_resolved_to_target_endpoint_metadata(self) -> None:
        result = self._client().inspect("~vendor/latest")
        self.assertIs(result.status, ModelCapabilityStatus.COMPATIBLE)
        self.assertEqual(result.resolved_model_id, "good/model")

    def test_unsupported_and_missing_models_fail_closed(self) -> None:
        client = self._client()
        unsupported = client.inspect("bad/model")
        missing = client.inspect("missing/model")
        self.assertIs(unsupported.status, ModelCapabilityStatus.INCOMPATIBLE)
        self.assertEqual(unsupported.reason, "REQUIRED_PARAMETERS_UNSUPPORTED")
        self.assertIs(missing.status, ModelCapabilityStatus.INCOMPATIBLE)
        self.assertEqual(missing.reason, "MODEL_NOT_FOUND")
        with self.assertRaisesRegex(ProviderCapabilityError, "No paid chat completion"):
            client.require_compatible("bad/model")

    def test_metadata_failure_is_unknown_and_not_passed(self) -> None:
        def fail(_url: str, _timeout: int) -> dict:
            raise TimeoutError("metadata timeout")

        client = OpenRouterModelCapabilityClient(fetch_json=fail)
        result = client.inspect("unknown/model")
        self.assertIs(result.status, ModelCapabilityStatus.UNKNOWN)
        with self.assertRaises(ProviderCapabilityError):
            client.require_compatible("unknown/model")


if __name__ == "__main__":
    unittest.main()
