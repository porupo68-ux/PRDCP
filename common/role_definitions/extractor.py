from __future__ import annotations

from typing import Any

from common.role_definitions.models import (
    RoleContext,
    RoleDefinitionSnapshot,
    RoleRuntimeConfig,
    definition_body,
    definition_identity,
)


def _strings(value: Any) -> list[str]:
    result: list[str] = []
    if value is None:
        return result
    if isinstance(value, str):
        if value.strip():
            result.append(value.strip())
        return result
    if isinstance(value, list):
        for item in value:
            result.extend(_strings(item))
        return result
    if isinstance(value, dict):
        preferred = ("rule", "description", "condition", "requirement", "name", "reason")
        selected = next((value.get(key) for key in preferred if isinstance(value.get(key), str)), None)
        if selected:
            result.append(selected.strip())
        else:
            for child in value.values():
                result.extend(_strings(child))
    return result


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


class RoleDefinitionExtractor:
    def extract_llm_context(self, snapshot: RoleDefinitionSnapshot) -> RoleContext:
        body = definition_body(snapshot.content)
        identity = definition_identity(snapshot.content)
        mission = body.get("mission")
        if isinstance(mission, dict):
            primary_objective = str(mission.get("primary_objective") or body.get("purpose") or "")
            success_definition = str(mission.get("success_definition") or "")
        else:
            primary_objective = str(mission or body.get("purpose") or "")
            success_definition = ""
        if not success_definition:
            success_definition = " / ".join(
                _strings(body.get("completion_criteria") or body.get("completion_conditions"))
            )
        decision_authority = body.get("decision_authority") or {}
        synthesis_policy = body.get("synthesis_policy") or {}
        prohibited = _strings(body.get("prohibited_actions"))
        prohibited += _strings(body.get("non_responsibilities"))
        if isinstance(decision_authority, dict):
            prohibited += _strings(decision_authority.get("prohibited"))
        if isinstance(synthesis_policy, dict):
            prohibited += _strings(synthesis_policy.get("prohibited_actions"))
        responsibilities = _strings(body.get("responsibilities"))
        boundaries = _strings(body.get("responsibility_boundaries"))
        boundaries += _strings(body.get("non_responsibilities"))
        constraints = _strings(body.get("constraints"))
        constraints += _strings(body.get("security_and_integrity_constraints"))
        output_requirements = _strings(body.get("completion_criteria"))
        output_requirements += _strings(body.get("completion_conditions"))
        output_requirements += _strings(body.get("quality_requirements"))
        return RoleContext(
            agent_id=snapshot.agent_id,
            display_name=str(identity.get("display_name") or body.get("agent_name") or snapshot.agent_id),
            description=str(identity.get("description") or body.get("role") or ""),
            mission=primary_objective,
            responsibilities=_unique(responsibilities),
            responsibility_boundaries=_unique(boundaries),
            decision_rules=_unique(_strings(body.get("decision_rules")) + _strings(body.get("core_principles"))),
            constraints=_unique(constraints),
            prohibited_actions=_unique(prohibited),
            success_definition=success_definition,
            failure_conditions=_unique(_strings(body.get("failure_conditions"))),
            output_requirements=_unique(output_requirements),
            revision_rules=_unique(_strings(body.get("revision_policy"))),
            uncertainty_rules=_unique(_strings(body.get("uncertainty_policy"))),
        )

    def extract_runtime_config(self, snapshot: RoleDefinitionSnapshot) -> RoleRuntimeConfig:
        body = definition_body(snapshot.content)
        contract = body.get("runtime_contract") or {}
        boundary = body.get("runtime_boundary") or {}
        return RoleRuntimeConfig(
            agent_id=snapshot.agent_id,
            layer_id=snapshot.layer_id,
            accepted_message_types=list(contract.get("accepted_message_types") or []),
            generated_message_types=list(contract.get("generated_message_types") or []),
            input_schema_id=contract.get("input_schema_id"),
            output_schema_id=contract.get("output_schema_id"),
            timeout_seconds=int(contract["timeout_seconds"]),
            technical_retry_limit=int(contract.get("technical_retry_limit", 2)),
            revision_limit=contract.get("revision_limit"),
            parallel_execution_allowed=bool(contract.get("parallel_execution_allowed", False)),
            allowed_tools=list(contract.get("allowed_tools") or []),
            prohibited_tools=list(contract.get("prohibited_tools") or []),
            prohibited_requested_actions=list(boundary.get("prohibited_requested_actions") or []),
        )
