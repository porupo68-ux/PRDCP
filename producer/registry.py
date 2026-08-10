from __future__ import annotations

from common.validation import PMPValidator, PayloadValidator
from common.role_definitions import RoleDefinitionLoader
from config.settings import BASE_DIR
from producer.agents import (
    GeneralOpinionAnalyst,
    QualityReviewer,
    ResearchPlanner,
    TopicScout,
    TopicSelector,
)
from providers.base import ModelProvider


class ProducerRegistry:
    def __init__(
        self,
        provider: ModelProvider,
        models: dict[str, str] | None = None,
        *,
        rd_loader: RoleDefinitionLoader | None = None,
    ) -> None:
        payload_validator = PayloadValidator()
        pmp_validator = PMPValidator()
        model_map = models or {}
        rd_loader = rd_loader or RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=BASE_DIR / "storage" / "data" / "logs" / "rd_access.jsonl",
        )
        self.rd_loader = rd_loader
        agent_types = [TopicScout, TopicSelector, GeneralOpinionAnalyst, ResearchPlanner, QualityReviewer]
        self._agents = {
            agent_type.agent_id: agent_type(
                provider,
                payload_validator,
                pmp_validator,
                model=model_map.get(agent_type.agent_id) or "mock",
                rd_loader=self.rd_loader,
            )
            for agent_type in agent_types
        }

    def get(self, agent_id: str):
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"Producer agent is not registered: {agent_id}") from exc

    @property
    def agent_ids(self) -> set[str]:
        return set(self._agents)
