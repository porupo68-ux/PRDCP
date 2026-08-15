from __future__ import annotations

from pathlib import Path

from common.role_definitions import RoleDefinitionLoader
from common.validation import PMPValidator, PayloadValidator
from config.settings import BASE_DIR
from playwright.agents import EvidenceCitationEditor, NarrativeArchitect, Scriptwriter, VisualDirector
from providers.base import ModelProvider


class PlaywrightRegistry:
    def __init__(
        self,
        provider: ModelProvider,
        models: dict[str, str] | None = None,
        *,
        rd_loader: RoleDefinitionLoader | None = None,
        demo_safe_mode: bool = True,
    ) -> None:
        self.provider = provider
        self.models = models or {}
        self.rd_loader = rd_loader or RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=Path(BASE_DIR) / "storage" / "data" / "logs" / "rd_access.jsonl",
        )
        payload_validator = PayloadValidator()
        pmp_validator = PMPValidator()
        agent_types = [NarrativeArchitect, Scriptwriter, EvidenceCitationEditor, VisualDirector]
        self._agents = {
            agent_type.agent_id: agent_type(
                provider,
                payload_validator,
                pmp_validator,
                model=self.models.get(agent_type.agent_id) or "mock",
                rd_loader=self.rd_loader,
                demo_safe_mode=demo_safe_mode,
            )
            for agent_type in agent_types
        }

    def get(self, agent_id: str):
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"Playwright agent is not registered: {agent_id}") from exc

    @property
    def agent_ids(self) -> set[str]:
        return set(self._agents)
