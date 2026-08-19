from __future__ import annotations

from pathlib import Path

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
from retrieval import MockRetrievalProvider, RetrievalCoordinator


class ProducerRegistry:
    def __init__(
        self,
        provider: ModelProvider,
        models: dict[str, str] | None = None,
        *,
        rd_loader: RoleDefinitionLoader | None = None,
        demo_safe_mode: bool = True,
        retrieval_coordinator: RetrievalCoordinator | None = None,
    ) -> None:
        self.provider = provider
        payload_validator = PayloadValidator()
        pmp_validator = PMPValidator()
        model_map = models or {}
        self.models = dict(model_map)
        rd_loader = rd_loader or RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=BASE_DIR / "storage" / "data" / "logs" / "rd_access.jsonl",
        )
        self.rd_loader = rd_loader
        if retrieval_coordinator is None and getattr(provider, "provider_id", None) == "mock":
            llm_reservation_root = getattr(provider, "reservation_root", None)
            data_dir = (
                llm_reservation_root.parent
                if llm_reservation_root is not None
                else BASE_DIR / "storage" / "data"
            )
            retrieval_coordinator = RetrievalCoordinator(
                MockRetrievalProvider(
                    reservation_root=data_dir / "retrieval_call_reservations"
                ),
                data_dir=data_dir,
                demo_safe_mode=demo_safe_mode,
            )
        agent_types = [TopicScout, TopicSelector, GeneralOpinionAnalyst, ResearchPlanner, QualityReviewer]
        self._agents = {
            agent_type.agent_id: agent_type(
                provider,
                payload_validator,
                pmp_validator,
                model=model_map.get(agent_type.agent_id) or "mock",
                rd_loader=self.rd_loader,
                demo_safe_mode=demo_safe_mode,
                retrieval_coordinator=retrieval_coordinator,
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

    def bind_retrieval_data_dir(self, data_dir: Path) -> None:
        coordinator = self.get("producer.general_opinion_analyst").retrieval_coordinator
        if coordinator is None:
            return
        coordinator.data_dir = Path(data_dir)
        coordinator.provider.reservation_root = (
            Path(data_dir) / "retrieval_call_reservations"
        )
