from __future__ import annotations

from typing import Any


def unique_strings(values: list[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            value for value in values if isinstance(value, str) and value
        )
    )


def decision_context_reference_values(
    input_data: dict[str, Any],
) -> dict[str, list[str]]:
    context = input_data.get("decision_context")
    if not isinstance(context, dict):
        return {}
    target_problem = context.get("target_problem")
    problem_ids = [context.get("deliberation_result_id")]
    if isinstance(target_problem, dict):
        problem_ids.append(target_problem.get("problem_id"))
    stakeholders = context.get("affected_stakeholders")
    stakeholder_ids = [
        item.get("stakeholder_id")
        for item in stakeholders
        if isinstance(item, dict)
    ] if isinstance(stakeholders, list) else []
    return {
        "claim": unique_strings(list(context.get("key_claim_ids") or [])),
        "evidence": unique_strings(list(context.get("evidence_ids") or [])),
        "analysis": unique_strings(list(context.get("analysis_ids") or [])),
        "source": unique_strings(list(context.get("source_ids") or [])),
        "problem": unique_strings(problem_ids),
        "stakeholder": unique_strings(stakeholder_ids),
    }


def candidate_reference_values(input_data: dict[str, Any]) -> list[str]:
    candidates = input_data.get("position_candidates")
    if not isinstance(candidates, list):
        generation = input_data.get("position_generation")
        candidates = (
            generation.get("position_candidates")
            if isinstance(generation, dict)
            else []
        )
    return unique_strings(
        [
            item.get("position_candidate_id")
            for item in candidates
            if isinstance(item, dict)
        ]
    )


def explicit_reference_values(value: Any, kind: str) -> list[str]:
    collected: list[Any] = []
    singular = f"{kind}_id"
    plural = f"{kind}_ids"

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            for field, child in current.items():
                if field == singular or field.endswith(f"_{singular}"):
                    collected.append(child)
                elif field == plural or field.endswith(f"_{plural}"):
                    if isinstance(child, list):
                        collected.extend(child)
                walk(child)
        elif isinstance(current, list):
            for child in current:
                walk(child)

    walk(value)
    return unique_strings(collected)


def bind_strict_reference_fields(
    schema: dict[str, Any],
    *,
    list_fields: dict[str, list[str]] | None = None,
    scalar_fields: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Bind explicit output reference fields to values present in one request.

    JSON Schema cannot derive these enums on its own.  The OpenRouter boundary
    already supplies ``input_data`` to schema specializers, so this mutates only
    the per-request schema copy and never the Pydantic model or saved payload.
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
