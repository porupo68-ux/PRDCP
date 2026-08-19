from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterator

from pydantic import BaseModel


_SCHEMA_MAP_CHILDREN = (
    "$defs",
    "definitions",
    "properties",
    "patternProperties",
    "dependentSchemas",
)
_SCHEMA_SINGLE_CHILDREN = (
    "items",
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
)
_SCHEMA_ARRAY_CHILDREN = ("anyOf", "oneOf", "allOf", "prefixItems")
_SAFE_REF_ANNOTATIONS = {
    "$comment",
    "default",
    "deprecated",
    "description",
    "examples",
    "readOnly",
    "title",
    "writeOnly",
}
_SCHEMA_ASSERTION_KEYWORDS = {
    "$ref",
    "allOf",
    "anyOf",
    "const",
    "contains",
    "else",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "if",
    "items",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "not",
    "oneOf",
    "pattern",
    "prefixItems",
    "properties",
    "then",
    "type",
}


@dataclass(frozen=True)
class StrictSchemaViolation:
    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


class StrictStructuredOutputSchemaError(ValueError):
    """Raised before a provider call when an output schema is not strict-safe."""

    def __init__(
        self,
        schema_name: str,
        violations: list[StrictSchemaViolation],
    ) -> None:
        self.schema_name = schema_name
        self.violations = tuple(violations)
        details = "\n".join(
            f"path: {violation.path}\nreason: {violation.reason}"
            for violation in violations
        )
        super().__init__(
            f"StrictStructuredOutputSchemaError: {schema_name}\n{details}"
        )


def strict_output_schema(
    output_model: type[BaseModel],
    *,
    input_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate the schema sent to strict Structured Output providers.

    Application models keep their Pydantic defaults. Only the API-boundary copy is
    normalized: defaults and safe annotations beside ``$ref`` are removed, every
    explicit object is closed, and all declared keys become required.
    """

    schema = normalize_strict_output_schema(output_model.model_json_schema())
    specializer = getattr(output_model, "specialize_strict_output_schema", None)
    if input_data is not None and callable(specializer):
        specialized = specializer(schema, input_data)
        if specialized is not None:
            schema = specialized
    validate_strict_output_schema(schema, schema_name=output_model.__name__)
    return schema


def normalize_strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a strict-provider copy without mutating Pydantic's JSON Schema."""

    normalized = deepcopy(schema)
    _normalize_schema_node(normalized, path="$")
    return normalized


def validate_strict_output_schema(
    schema: dict[str, Any],
    *,
    schema_name: str = "StructuredOutput",
) -> None:
    """Validate a final API schema independently from normalization."""

    violations = _find_strict_schema_violations(schema)
    if violations:
        raise StrictStructuredOutputSchemaError(schema_name, violations)


def strict_schema_violations(schema: dict[str, Any]) -> list[str]:
    """Return path-aware strict-schema violations without raising."""

    return [str(item) for item in _find_strict_schema_violations(schema)]


def _normalize_schema_node(node: dict[str, Any], *, path: str) -> None:
    node.pop("default", None)

    if "$ref" in node:
        for keyword in _SAFE_REF_ANNOTATIONS:
            if keyword != "$ref":
                node.pop(keyword, None)

    properties = node.get("properties")
    if isinstance(properties, dict):
        node["required"] = list(properties)
        node["additionalProperties"] = False

    for child_path, child in _iter_schema_children(node, path=path):
        _normalize_schema_node(child, path=child_path)


def _find_strict_schema_violations(
    schema: dict[str, Any],
) -> list[StrictSchemaViolation]:
    violations: list[StrictSchemaViolation] = []
    for path, node in _walk_schema(schema):
        if "default" in node:
            violations.append(
                StrictSchemaViolation(path, "'default' is not allowed in the API schema")
            )

        reference = node.get("$ref")
        if reference is not None:
            siblings = sorted(key for key in node if key != "$ref")
            if siblings:
                violations.append(
                    StrictSchemaViolation(
                        path,
                        f"'$ref' cannot coexist with keywords {siblings!r}",
                    )
                )
            if not isinstance(reference, str) or not reference.startswith("#/"):
                violations.append(
                    StrictSchemaViolation(path, "only local '#/' references are supported")
                )
            elif _resolve_local_ref(schema, reference) is None:
                violations.append(
                    StrictSchemaViolation(path, f"unresolved local reference {reference!r}")
                )

        if _is_unconstrained_schema(node):
            violations.append(
                StrictSchemaViolation(
                    path,
                    "unconstrained object/Any schemas are not allowed; replace the "
                    "field with an explicit Pydantic model",
                )
            )

        if _is_object_node(node):
            properties = node.get("properties")
            if not isinstance(properties, dict):
                violations.append(
                    StrictSchemaViolation(
                        path,
                        "object schemas must declare explicit properties",
                    )
                )
            else:
                property_names = list(properties)
                required = node.get("required")
                if required != property_names:
                    violations.append(
                        StrictSchemaViolation(
                            path,
                            f"required={required!r}, properties={property_names!r}",
                        )
                    )
            if node.get("additionalProperties") is not False:
                violations.append(
                    StrictSchemaViolation(path, "additionalProperties must be false")
                )

        if _has_type(node, "array") and not isinstance(node.get("items"), dict):
            violations.append(
                StrictSchemaViolation(path, "array schemas must declare an items schema")
            )

    return violations


def _walk_schema(
    schema: dict[str, Any],
    *,
    path: str = "$",
) -> Iterator[tuple[str, dict[str, Any]]]:
    yield path, schema
    for child_path, child in _iter_schema_children(schema, path=path):
        yield from _walk_schema(child, path=child_path)


def _iter_schema_children(
    node: dict[str, Any],
    *,
    path: str,
) -> Iterator[tuple[str, dict[str, Any]]]:
    for keyword in _SCHEMA_MAP_CHILDREN:
        children = node.get(keyword)
        if isinstance(children, dict):
            for name, child in children.items():
                if isinstance(child, dict):
                    yield f"{path}/{keyword}/{_escape_json_pointer(str(name))}", child

    for keyword in _SCHEMA_SINGLE_CHILDREN:
        child = node.get(keyword)
        if isinstance(child, dict):
            yield f"{path}/{keyword}", child
        elif keyword == "items" and isinstance(child, list):
            for index, item in enumerate(child):
                if isinstance(item, dict):
                    yield f"{path}/{keyword}/{index}", item

    for keyword in _SCHEMA_ARRAY_CHILDREN:
        children = node.get(keyword)
        if isinstance(children, list):
            for index, child in enumerate(children):
                if isinstance(child, dict):
                    yield f"{path}/{keyword}/{index}", child


def _resolve_local_ref(schema: dict[str, Any], reference: str) -> Any | None:
    current: Any = schema
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _has_type(node: dict[str, Any], expected: str) -> bool:
    node_type = node.get("type")
    return node_type == expected or (
        isinstance(node_type, list) and expected in node_type
    )


def _is_object_node(node: dict[str, Any]) -> bool:
    return (
        _has_type(node, "object")
        or "properties" in node
        or "additionalProperties" in node
    )


def _is_unconstrained_schema(node: dict[str, Any]) -> bool:
    return not any(keyword in node for keyword in _SCHEMA_ASSERTION_KEYWORDS)
