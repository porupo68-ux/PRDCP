from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

from common.models.errors import ProviderRequestSchemaError


# Gemini compiles enum literals into its controlled-generation constraint.  The
# provider does not publish an exact state limit and may change it by endpoint,
# so PRDCP uses a deliberately bounded selector contract: enum is for compact
# identifiers/classifications, never retrieved titles, URLs, or document text.
GEMINI_MAX_ENUM_LITERAL_UTF8_BYTES = 512
GEMINI_MAX_ENUM_UTF8_BYTES = 8_192
GEMINI_MAX_SCHEMA_UTF8_BYTES = 48_000


@dataclass(frozen=True)
class ProviderSchemaViolation:
    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


def validate_provider_schema_compatibility(
    model: str,
    schema: dict[str, Any],
) -> None:
    """Reject known provider-specific constraint explosions before HTTP.

    Azure/OpenAI strict-shape validation remains authoritative and runs before
    this check.  This second gate only addresses endpoint compilation limits
    that a syntactically valid strict JSON Schema can still violate.
    """

    if not model.lower().startswith("google/gemini-"):
        return
    violations = gemini_schema_violations(schema)
    if not violations:
        return
    details = "\n".join(str(item) for item in violations)
    raise ProviderRequestSchemaError(
        "GEMINI_STRUCTURED_OUTPUT_SCHEMA_ERROR: final response schema is not "
        "bounded for Gemini controlled generation. No HTTP request was sent.\n"
        + details,
        provider="openrouter",
        model_id=model,
    )


def gemini_schema_violations(
    schema: dict[str, Any],
) -> list[ProviderSchemaViolation]:
    violations: list[ProviderSchemaViolation] = []
    serialized_size = len(
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if serialized_size > GEMINI_MAX_SCHEMA_UTF8_BYTES:
        violations.append(
            ProviderSchemaViolation(
                "$",
                f"schema is {serialized_size} UTF-8 bytes; PRDCP Gemini budget is "
                f"{GEMINI_MAX_SCHEMA_UTF8_BYTES}",
            )
        )

    for path, node in _walk(schema):
        values = node.get("enum")
        if not isinstance(values, list):
            continue
        encoded_sizes = [
            len(value.encode("utf-8"))
            for value in values
            if isinstance(value, str)
        ]
        if not encoded_sizes:
            continue
        largest = max(encoded_sizes)
        total = sum(encoded_sizes)
        if largest > GEMINI_MAX_ENUM_LITERAL_UTF8_BYTES:
            violations.append(
                ProviderSchemaViolation(
                    f"{path}/enum",
                    f"largest string literal is {largest} UTF-8 bytes; dynamic enums "
                    "must use compact stable identifiers",
                )
            )
        if total > GEMINI_MAX_ENUM_UTF8_BYTES:
            violations.append(
                ProviderSchemaViolation(
                    f"{path}/enum",
                    f"string literals total {total} UTF-8 bytes; dynamic enums must "
                    "not embed retrieved payload text",
                )
            )
    return violations


def _walk(
    value: Any,
    path: str = "$",
) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk(child, f"{path}/{_escape(str(key))}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}/{index}")


def _escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
