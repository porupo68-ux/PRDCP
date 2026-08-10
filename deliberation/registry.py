from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from common.models.errors import AgentExecutionError, PayloadValidationError
from common.prompting import PRDCP_COMMON_RULES, PromptBuilder
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from common.validation import PMPValidator, PayloadValidator
from config.settings import BASE_DIR
from deliberation.agents import (
    ArgumentAnalyst,
    CausalStructuralAnalyst,
    CounterargumentAnalyst,
    DeliberationQualityReviewer,
    StakeholderResponseAnalyst,
)
from providers.base import ModelProvider


OutputT = TypeVar("OutputT", bound=BaseModel)


class DeliberationRegistry:
    def __init__(
        self,
        provider: ModelProvider,
        models: dict[str, str] | None = None,
        *,
        rd_loader: RoleDefinitionLoader | None = None,
    ) -> None:
        payload_validator = PayloadValidator()
        pmp_validator = PMPValidator()
        self.provider = provider
        self.payload_validator = payload_validator
        self.models = models or {}
        self.rd_loader = rd_loader or RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=Path(BASE_DIR) / "storage" / "data" / "logs" / "rd_access.jsonl",
        )
        agent_types = [
            ArgumentAnalyst,
            CausalStructuralAnalyst,
            StakeholderResponseAnalyst,
            CounterargumentAnalyst,
            DeliberationQualityReviewer,
        ]
        self._agents = {
            agent_type.agent_id: agent_type(
                provider,
                payload_validator,
                pmp_validator,
                model=self.models.get(agent_type.agent_id) or "mock",
                rd_loader=self.rd_loader,
            )
            for agent_type in agent_types
        }

    def get(self, agent_id: str):
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"Deliberation agent is not registered: {agent_id}") from exc

    async def integrate(
        self,
        *,
        input_data: dict,
        output_schema: type[OutputT],
        stage: str,
        max_technical_retries: int = 2,
    ) -> OutputT:
        agent_prompt = (Path(BASE_DIR) / "deliberation" / "prompts" / "manager.md").read_text(
            encoding="utf-8"
        )
        snapshot = self.rd_loader.load("deliberation.manager")
        extractor = RoleDefinitionExtractor()
        runtime_config = extractor.extract_runtime_config(snapshot)
        prompt = PromptBuilder().build(
            common_rules=PRDCP_COMMON_RULES,
            role_context=extractor.extract_llm_context(snapshot),
            agent_prompt=agent_prompt + f"\nCurrent integration stage: {stage}",
            task_constraints={"integration_stage": stage},
            output_schema=output_schema.model_json_schema(),
        )
        self.rd_loader.access_log.record(
            "role_context_built",
            agent_id="deliberation.manager",
            role_definition_version=snapshot.role_definition_version,
            role_definition_hash=snapshot.content_hash,
        )
        last_error: Exception | None = None
        retry_limit = runtime_config.technical_retry_limit
        for _attempt in range(retry_limit + 1):
            try:
                raw = await asyncio.wait_for(
                    self.provider.generate_structured(
                        model=self.models.get("deliberation.manager") or "mock",
                        system_prompt=prompt,
                        input_data=input_data,
                        output_schema=output_schema,
                    ),
                    timeout=runtime_config.timeout_seconds,
                )
                return self.payload_validator.validate(raw, output_schema)
            except (AgentExecutionError, PayloadValidationError, TimeoutError) as exc:
                last_error = exc
        raise AgentExecutionError(
            f"deliberation.manager exceeded technical retry limit during {stage}: {last_error}"
        ) from last_error

    @property
    def agent_ids(self) -> set[str]:
        return set(self._agents)
