from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterator

from common.models.errors import ProviderRequestSchemaError


# Gemini compiles enum literals into its controlled-generation constraint.  The
# provider does not publish an exact state limit and may change it by endpoint,
# so PRDCP uses a deliberately bounded selector contract: enum is for compact
# identifiers/classifications, never retrieved titles, URLs, or document text.
GEMINI_MAX_ENUM_LITERAL_UTF8_BYTES = 512
GEMINI_MAX_ENUM_UTF8_BYTES = 8_192
GEMINI_MAX_ENUM_VALUES_PER_NODE = 96
GEMINI_MAX_REPEATED_DYNAMIC_ENUM_VALUES = 96
GEMINI_MAX_SCHEMA_UTF8_BYTES = 48_000
GEMINI_MAX_ANY_OF_BRANCHES_PER_NODE = 8
GEMINI_WIRE_SCHEMA_REVISION = "gemini_wire_schema_v2"

# Gemini's native JSON Schema endpoint accepts only this documented subset.
# OpenRouter's synchronous and asynchronous endpoints ultimately compile the
# response schema with Gemini controlled generation. Keep this list explicit:
# a future Pydantic keyword must fail locally instead of consuming a request.
GEMINI_BATCH_SCHEMA_KEYWORDS = frozenset(
    {
        "$id",
        "$defs",
        "$ref",
        "$anchor",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "additionalProperties",
        "required",
    }
)
GEMINI_BATCH_STRING_FORMATS = frozenset({"date-time", "date", "time"})
_GEMINI_BATCH_LOCAL_ONLY_STRING_KEYWORDS = frozenset(
    {"minLength", "maxLength", "pattern"}
)
_GEMINI_LOCAL_ONLY_COMPLEXITY_KEYWORDS = frozenset(
    {
        # Pydantic validates these after generation. Omitting them from the
        # Gemini wire schema reduces the provider's opaque constraint-state
        # complexity without changing keys, types, enums, or strict closure.
        "minItems",
        "maxItems",
    }
)


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
    violations = gemini_schema_violations(
        schema,
        batch=model.lower().endswith(":batch"),
    )
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
    *,
    batch: bool = False,
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

    for path, node in _walk_schema_nodes(schema):
        branches = node.get("anyOf")
        if (
            isinstance(branches, list)
            and len(branches) > GEMINI_MAX_ANY_OF_BRANCHES_PER_NODE
        ):
            violations.append(
                ProviderSchemaViolation(
                    f"{path}/anyOf",
                    f"union contains {len(branches)} branches; PRDCP Gemini "
                    "per-node complexity budget is "
                    f"{GEMINI_MAX_ANY_OF_BRANCHES_PER_NODE}",
                )
            )

    for path, node in _walk_schema_nodes(schema):
        for keyword in node:
            if keyword not in GEMINI_BATCH_SCHEMA_KEYWORDS:
                violations.append(
                    ProviderSchemaViolation(
                        path,
                        f"unsupported Gemini JSON Schema keyword {keyword!r}",
                    )
                )
        schema_format = node.get("format")
        if (
            schema_format is not None
            and schema_format not in GEMINI_BATCH_STRING_FORMATS
        ):
            violations.append(
                ProviderSchemaViolation(
                    f"{path}/format",
                    f"unsupported Gemini string format {schema_format!r}",
                )
            )

    repeated_dynamic_enums: dict[str, tuple[int, int]] = {}
    for path, node in _walk(schema):
        values = node.get("enum")
        if not isinstance(values, list):
            continue
        if len(values) > GEMINI_MAX_ENUM_VALUES_PER_NODE:
            violations.append(
                ProviderSchemaViolation(
                    f"{path}/enum",
                    f"enum contains {len(values)} values; PRDCP Gemini complexity "
                    f"budget is {GEMINI_MAX_ENUM_VALUES_PER_NODE}",
                )
            )
        if len(values) >= 16:
            signature = json.dumps(
                values,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            count, width = repeated_dynamic_enums.get(
                signature,
                (0, len(values)),
            )
            repeated_dynamic_enums[signature] = (count + 1, width)
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
    for occurrence_count, value_count in repeated_dynamic_enums.values():
        compiled_values = occurrence_count * value_count
        if compiled_values > GEMINI_MAX_REPEATED_DYNAMIC_ENUM_VALUES:
            violations.append(
                ProviderSchemaViolation(
                    "$",
                    f"the same {value_count}-value dynamic enum is repeated "
                    f"{occurrence_count} times ({compiled_values} compiled values); "
                    "reuse a shared $defs selector instead of duplicating dynamic enums",
                )
            )
    return violations


def specialize_provider_output_schema(
    model: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Return the exact provider-bound schema without changing app contracts.

    Gemini compiles response schemas into an opaque controlled-generation
    grammar. Constraints removed here remain authoritative in the Pydantic
    validation performed on the returned payload. ``const`` is preserved
    losslessly as a singleton ``enum``. Titles and list cardinality bounds are
    wire-only removals: they do not change the strict object shape or ID enums,
    while avoiding valid-but-too-complex schemas being rejected with HTTP 400.
    """

    specialized = deepcopy(schema)
    is_gemini = model.lower().startswith("google/gemini-")
    is_batch = model.lower().endswith(":batch")
    if not is_gemini:
        return specialized

    for _path, node in _walk_schema_nodes(specialized):
        if "const" in node:
            const_value = node.pop("const")
            existing_enum = node.get("enum")
            if existing_enum is None:
                node["enum"] = [const_value]
            elif const_value not in existing_enum:
                raise ProviderRequestSchemaError(
                    "GEMINI_STRUCTURED_OUTPUT_SCHEMA_ERROR: const conflicts with enum. "
                    "No HTTP request was sent.",
                    provider="openrouter",
                    model_id=model,
                )
        for keyword in _GEMINI_BATCH_LOCAL_ONLY_STRING_KEYWORDS:
            node.pop(keyword, None)
        for keyword in _GEMINI_LOCAL_ONLY_COMPLEXITY_KEYWORDS:
            node.pop(keyword, None)
        node.pop("title", None)
        if (
            "format" in node
            and node["format"] not in GEMINI_BATCH_STRING_FORMATS
        ):
            node.pop("format", None)

    if is_batch:
        return _inline_gemini_batch_local_refs(specialized, model=model)
    return specialized


def _inline_gemini_batch_local_refs(
    schema: dict[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    """Inline acyclic local refs for the Google Vertex Batch compiler.

    Google documents ``$defs``/``$ref`` support, but the OpenRouter-to-Vertex
    Batch path has rejected multi-field referenced objects after accepting the
    same object inline.  PRDCP schemas are acyclic and remain below the local
    Gemini size budget after expansion.  A future recursive schema fails before
    HTTP rather than being expanded without a bound.
    """

    root = deepcopy(schema)

    def resolve(reference: str) -> Any:
        if not reference.startswith("#/"):
            raise ProviderRequestSchemaError(
                "GEMINI_STRUCTURED_OUTPUT_SCHEMA_ERROR: only local references can "
                "be inlined for Gemini Batch. No HTTP request was sent.",
                provider="openrouter",
                model_id=model,
            )
        current: Any = root
        for raw_part in reference[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or part not in current:
                raise ProviderRequestSchemaError(
                    "GEMINI_STRUCTURED_OUTPUT_SCHEMA_ERROR: unresolved local "
                    f"reference {reference!r}. No HTTP request was sent.",
                    provider="openrouter",
                    model_id=model,
                )
            current = current[part]
        return current

    def expand(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str):
                if reference in stack:
                    raise ProviderRequestSchemaError(
                        "GEMINI_STRUCTURED_OUTPUT_SCHEMA_ERROR: cyclic local reference "
                        f"{reference!r} cannot be bounded for Gemini Batch. No HTTP "
                        "request was sent.",
                        provider="openrouter",
                        model_id=model,
                    )
                return expand(deepcopy(resolve(reference)), stack + (reference,))
            return {
                key: expand(child, stack)
                for key, child in value.items()
                if key not in {"$defs", "definitions"}
            }
        if isinstance(value, list):
            return [expand(child, stack) for child in value]
        return value

    expanded = expand(root)
    if not isinstance(expanded, dict):
        raise ProviderRequestSchemaError(
            "GEMINI_STRUCTURED_OUTPUT_SCHEMA_ERROR: expanded root is not an object. "
            "No HTTP request was sent.",
            provider="openrouter",
            model_id=model,
        )
    return expanded


def _walk_schema_nodes(
    schema: dict[str, Any],
    path: str = "$",
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Walk schemas while treating property/definition names as map keys."""

    yield path, schema
    for keyword in ("$defs", "definitions", "properties"):
        children = schema.get(keyword)
        if isinstance(children, dict):
            for name, child in children.items():
                if isinstance(child, dict):
                    yield from _walk_schema_nodes(
                        child,
                        f"{path}/{keyword}/{_escape(str(name))}",
                    )
    for keyword in ("items", "additionalProperties"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            yield from _walk_schema_nodes(child, f"{path}/{keyword}")
    for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
        children = schema.get(keyword)
        if isinstance(children, list):
            for index, child in enumerate(children):
                if isinstance(child, dict):
                    yield from _walk_schema_nodes(
                        child,
                        f"{path}/{keyword}/{index}",
                    )


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
