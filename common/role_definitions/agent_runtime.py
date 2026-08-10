from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from pydantic import BaseModel

from common.models.pmp import PMPMessage
from common.prompting import PRDCP_COMMON_RULES, PromptBuilder
from common.role_definitions.boundary import RoleBoundaryValidator
from common.role_definitions.extractor import RoleDefinitionExtractor
from common.role_definitions.loader import RoleDefinitionLoader
from common.role_definitions.models import RoleDefinitionSnapshot, RoleRuntimeConfig


@dataclass(frozen=True)
class AgentExecutionContext:
    agent_run_id: str
    snapshot: RoleDefinitionSnapshot
    runtime_config: RoleRuntimeConfig
    system_prompt: str


def prepare_agent_execution(
    *,
    loader: RoleDefinitionLoader,
    agent_id: str,
    message: PMPMessage,
    agent_prompt: str,
    output_schema: type[BaseModel],
    expected_output_message_type: str,
) -> AgentExecutionContext:
    agent_run_id = f"run_{uuid4()}"
    snapshot = loader.load(agent_id, agent_run_id=agent_run_id)
    extractor = RoleDefinitionExtractor()
    runtime = extractor.extract_runtime_config(snapshot)
    RoleBoundaryValidator().validate(
        message=message,
        runtime_config=runtime,
        snapshot=snapshot,
        expected_output_message_type=expected_output_message_type,
    )
    role_context = extractor.extract_llm_context(snapshot)
    reviewer_context = _reviewer_context(
        loader,
        reviewer_agent_id=agent_id,
        agent_run_id=agent_run_id,
    )
    prompt = PromptBuilder().build(
        common_rules=PRDCP_COMMON_RULES,
        role_context=role_context,
        agent_prompt=agent_prompt,
        task_constraints=message.constraints,
        output_schema=output_schema.model_json_schema(),
        reviewer_context=reviewer_context,
    )
    loader.access_log.record(
        "role_context_built",
        agent_run_id=agent_run_id,
        agent_id=agent_id,
        role_definition_version=snapshot.role_definition_version,
        role_definition_hash=snapshot.content_hash,
    )
    return AgentExecutionContext(
        agent_run_id=agent_run_id,
        snapshot=snapshot,
        runtime_config=runtime,
        system_prompt=prompt,
    )


def _reviewer_context(
    loader: RoleDefinitionLoader,
    *,
    reviewer_agent_id: str,
    agent_run_id: str,
) -> dict | None:
    if not reviewer_agent_id.endswith(".quality_reviewer"):
        return None
    layer = reviewer_agent_id.split(".", 1)[0]
    extractor = RoleDefinitionExtractor()
    review_context: dict[str, dict] = {}
    section_names = [
        "responsibilities",
        "responsibility_boundaries",
        "constraints",
        "prohibited_actions",
        "output_requirements",
    ]
    for target_agent_id in sorted(loader.registry.agent_ids):
        if not target_agent_id.startswith(layer + ".") or target_agent_id == reviewer_agent_id:
            continue
        target_snapshot = loader.load(target_agent_id, agent_run_id=agent_run_id)
        loader.get_sections(
            target_agent_id,
            section_names,
            requester_agent_id=reviewer_agent_id,
            agent_run_id=agent_run_id,
            snapshot=target_snapshot,
        )
        target = extractor.extract_llm_context(target_snapshot)
        review_context[target_agent_id] = {
            "responsibilities": target.responsibilities,
            "responsibility_boundaries": target.responsibility_boundaries,
            "constraints": target.constraints,
            "prohibited_actions": target.prohibited_actions,
            "output_requirements": target.output_requirements,
        }
    return review_context


def role_definition_extensions(snapshot: RoleDefinitionSnapshot | None) -> dict:
    return {"role_definition": snapshot.trace()} if snapshot is not None else {}
