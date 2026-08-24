from __future__ import annotations

from copy import deepcopy
from typing import Any


# Per-item cross-field correlation is useful for small responses, but copying
# a complete object branch once per paragraph makes Gemini's controlled-
# generation grammar grow linearly with script length. The global ID enums are
# still present and PlaywrightValidator enforces the exact paragraph-local
# relationship before Final Gate/Delivery.
MAX_STRICT_CORRELATED_VARIANTS = 8


def unique_strings(values: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            value for value in values if isinstance(value, str) and value
        )
    )


def bind_strict_reference_fields(
    schema: dict[str, Any],
    *,
    list_fields: dict[str, list[str]] | None = None,
    scalar_fields: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Bind explicit ID fields to the canonical values in one request.

    The function mutates only the per-request strict-schema copy produced at
    the Provider boundary.  Empty optional arrays are constrained to remain
    empty instead of receiving a fabricated reference.
    """

    list_fields = {
        name: unique_strings(values)
        for name, values in (list_fields or {}).items()
    }
    scalar_fields = {
        name: unique_strings(values)
        for name, values in (scalar_fields or {}).items()
    }

    def bind_array(node: dict[str, Any], values: list[str], field: str) -> None:
        branches = [node] if node.get("type") == "array" else [
            branch
            for branch in node.get("anyOf", [])
            if isinstance(branch, dict) and branch.get("type") == "array"
        ]
        if not branches:
            raise ValueError(f"strict schema field {field} is not an array")
        for branch in branches:
            items = branch.get("items")
            if not isinstance(items, dict) or items.get("type") != "string":
                raise ValueError(
                    f"strict schema field {field} does not contain string items"
                )
            if values:
                items["enum"] = list(values)
                branch.pop("maxItems", None)
            elif int(branch.get("minItems") or 0) > 0:
                raise ValueError(
                    f"strict schema field {field} requires a non-empty input allowlist"
                )
            else:
                branch["maxItems"] = 0

    def bind_scalar(node: dict[str, Any], values: list[str], field: str) -> None:
        branches = [node] if node.get("type") == "string" else [
            branch
            for branch in node.get("anyOf", [])
            if isinstance(branch, dict) and branch.get("type") == "string"
        ]
        if not branches:
            raise ValueError(f"strict schema field {field} is not a string")
        if not values:
            raise ValueError(
                f"strict schema field {field} requires a non-empty input allowlist"
            )
        for branch in branches:
            branch["enum"] = list(values)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                for field, child in properties.items():
                    if not isinstance(child, dict):
                        continue
                    if field in list_fields:
                        bind_array(child, list_fields[field], field)
                    if field in scalar_fields:
                        bind_scalar(child, scalar_fields[field], field)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(schema)
    return schema


def bind_array_item_variants(
    schema: dict[str, Any],
    *,
    array_field: str,
    variants: list[dict[str, dict[str, list[str]]]],
) -> dict[str, Any]:
    """Constrain bounded arrays to request-specific reference branches.

    For a long script, retain the globally bound item schema instead of
    expanding one structurally identical object per paragraph. This preserves
    the finite ID domain while the deterministic Playwright validator remains
    authoritative for cross-field paragraph correlation.
    """

    targets: list[dict[str, Any]] = []

    def find(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                child = properties.get(array_field)
                if isinstance(child, dict) and child.get("type") == "array":
                    targets.append(child)
            for child in node.values():
                find(child)
        elif isinstance(node, list):
            for child in node:
                find(child)

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        reference = node.get("$ref")
        if not isinstance(reference, str):
            return deepcopy(node)
        if not reference.startswith("#/"):
            raise ValueError(f"unsupported strict-schema reference: {reference}")
        current: Any = schema
        for part in reference[2:].split("/"):
            current = current[part.replace("~1", "/").replace("~0", "~")]
        if not isinstance(current, dict):
            raise ValueError(f"strict-schema reference is not an object: {reference}")
        return deepcopy(current)

    find(schema)
    if len(targets) != 1:
        raise ValueError(
            f"strict schema expected one {array_field} array, found {len(targets)}"
        )
    target = targets[0]
    items = target.get("items")
    if not isinstance(items, dict):
        raise ValueError(f"strict schema field {array_field} has no object items")
    if not variants:
        target["maxItems"] = 0
        return schema

    if len(variants) > MAX_STRICT_CORRELATED_VARIANTS:
        return schema

    template = resolve(items)
    branches = []
    for variant in variants:
        branch = deepcopy(template)
        bind_strict_reference_fields(
            branch,
            list_fields=variant.get("list_fields"),
            scalar_fields=variant.get("scalar_fields"),
        )
        branches.append(branch)
    target["items"] = {"anyOf": branches}
    return schema
