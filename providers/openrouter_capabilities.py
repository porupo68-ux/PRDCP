from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from common.models.errors import ProviderCapabilityError


REQUIRED_STRUCTURED_OUTPUT_PARAMETERS = frozenset(
    {"response_format", "structured_outputs"}
)


class ModelCapabilityStatus(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ModelCapabilityResult:
    requested_model_id: str
    resolved_model_id: str | None
    status: ModelCapabilityStatus
    reason: str
    endpoint_count: int = 0
    compatible_endpoint_count: int = 0

    @property
    def compatible(self) -> bool:
        return self.status is ModelCapabilityStatus.COMPATIBLE


FetchJson = Callable[[str, int], dict]


class OpenRouterModelCapabilityClient:
    """Read-only OpenRouter metadata preflight for PRDCP's strict contract."""

    def __init__(
        self,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: int = 30,
        fetch_json: FetchJson | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("OpenRouter capability timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._fetch_json = fetch_json or self._default_fetch_json
        self._catalog_by_id: dict[str, dict] | None = None
        self._result_cache: dict[str, ModelCapabilityResult] = {}

    def inspect(self, model_id: str) -> ModelCapabilityResult:
        requested = model_id.strip()
        if not requested:
            return ModelCapabilityResult(
                requested_model_id=model_id,
                resolved_model_id=None,
                status=ModelCapabilityStatus.INCOMPATIBLE,
                reason="MODEL_ID_MISSING",
            )
        cached = self._result_cache.get(requested)
        if cached is not None:
            return cached
        try:
            result = self._inspect_uncached(requested)
        except Exception as exc:
            result = ModelCapabilityResult(
                requested_model_id=requested,
                resolved_model_id=None,
                status=ModelCapabilityStatus.UNKNOWN,
                reason=f"METADATA_UNAVAILABLE:{type(exc).__name__}",
            )
        self._result_cache[requested] = result
        return result

    def require_compatible(self, model_id: str) -> ModelCapabilityResult:
        result = self.inspect(model_id)
        if result.compatible:
            return result
        raise ProviderCapabilityError(
            "MODEL_CAPABILITY_ERROR: "
            f"model={model_id}; "
            "required=response_format=json_schema, json_schema.strict=true, "
            "provider.require_parameters=true; "
            f"reason={result.reason}; endpoints={result.endpoint_count}; "
            f"compatible_endpoints={result.compatible_endpoint_count}. "
            "No paid chat completion request was sent.",
            provider="openrouter",
            model_id=model_id or None,
        )

    def _inspect_uncached(self, requested: str) -> ModelCapabilityResult:
        catalog = self._catalog()
        entry = catalog.get(requested)
        if entry is None:
            return ModelCapabilityResult(
                requested_model_id=requested,
                resolved_model_id=None,
                status=ModelCapabilityStatus.INCOMPATIBLE,
                reason="MODEL_NOT_FOUND",
            )

        resolved = entry
        seen = {requested}
        while isinstance(resolved.get("alias_target"), dict):
            target_id = str(resolved["alias_target"].get("slug") or "").strip()
            if not target_id or target_id in seen:
                return ModelCapabilityResult(
                    requested_model_id=requested,
                    resolved_model_id=target_id or None,
                    status=ModelCapabilityStatus.UNKNOWN,
                    reason="ALIAS_TARGET_INVALID",
                )
            seen.add(target_id)
            target = catalog.get(target_id)
            if target is None:
                return ModelCapabilityResult(
                    requested_model_id=requested,
                    resolved_model_id=target_id,
                    status=ModelCapabilityStatus.UNKNOWN,
                    reason="ALIAS_TARGET_NOT_FOUND",
                )
            resolved = target

        resolved_id = str(resolved.get("id") or "").strip() or None
        detail_path = str((resolved.get("links") or {}).get("details") or "").strip()
        if not detail_path:
            return ModelCapabilityResult(
                requested_model_id=requested,
                resolved_model_id=resolved_id,
                status=ModelCapabilityStatus.UNKNOWN,
                reason="ENDPOINT_METADATA_LINK_MISSING",
            )
        endpoint_payload = self._fetch_json(self._absolute_url(detail_path), self.timeout)
        data = endpoint_payload.get("data")
        endpoints = data.get("endpoints") if isinstance(data, dict) else None
        if not isinstance(endpoints, list):
            return ModelCapabilityResult(
                requested_model_id=requested,
                resolved_model_id=resolved_id,
                status=ModelCapabilityStatus.UNKNOWN,
                reason="ENDPOINT_METADATA_INVALID",
            )
        compatible = [
            endpoint
            for endpoint in endpoints
            if self._endpoint_is_compatible(endpoint)
        ]
        if compatible:
            return ModelCapabilityResult(
                requested_model_id=requested,
                resolved_model_id=resolved_id,
                status=ModelCapabilityStatus.COMPATIBLE,
                reason="REQUIRED_PARAMETERS_SUPPORTED",
                endpoint_count=len(endpoints),
                compatible_endpoint_count=len(compatible),
            )
        return ModelCapabilityResult(
            requested_model_id=requested,
            resolved_model_id=resolved_id,
            status=ModelCapabilityStatus.INCOMPATIBLE,
            reason=(
                "NO_ENDPOINTS"
                if not endpoints
                else "REQUIRED_PARAMETERS_UNSUPPORTED"
            ),
            endpoint_count=len(endpoints),
            compatible_endpoint_count=0,
        )

    def _catalog(self) -> dict[str, dict]:
        if self._catalog_by_id is not None:
            return self._catalog_by_id
        payload = self._fetch_json(f"{self.base_url}/models", self.timeout)
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("OpenRouter model catalog has no data array")
        catalog: dict[str, dict] = {}
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                catalog[item["id"]] = item
        self._catalog_by_id = catalog
        return catalog

    @staticmethod
    def _endpoint_is_compatible(endpoint: object) -> bool:
        if not isinstance(endpoint, dict):
            return False
        if endpoint.get("status") not in (None, 0):
            return False
        parameters = endpoint.get("supported_parameters")
        return isinstance(parameters, list) and REQUIRED_STRUCTURED_OUTPUT_PARAMETERS.issubset(
            {str(value) for value in parameters}
        )

    def _absolute_url(self, path: str) -> str:
        parsed = urlsplit(self.base_url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        return path if path.startswith(("http://", "https://")) else origin + "/" + path.lstrip("/")

    @staticmethod
    def _default_fetch_json(url: str, timeout: int) -> dict:
        request = Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("OpenRouter metadata response must be an object")
        return payload
