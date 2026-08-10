from __future__ import annotations

import asyncio
from pathlib import Path

from common.models.pmp import PMPMessage
from playwright.manager import PlaywrightManager
from playwright.registry import PlaywrightRegistry
from providers.mock_provider import MockModelProvider
from storage.playwright_workflow_repository import PlaywrightWorkflowRepository
from tests.conclusion_helpers import make_conclusion_handoff, make_conclusion_manager


def make_playwright_handoff(
    data_dir: Path,
    provider: MockModelProvider | None = None,
) -> PMPMessage:
    provider = provider or MockModelProvider()
    conclusion = make_conclusion_manager(data_dir, provider)
    waiting = asyncio.run(
        conclusion.start_from_message(make_conclusion_handoff(data_dir, provider))
    )
    if waiting.status != "WAITING_HUMAN_SELECTION":
        raise AssertionError(f"Failed to create Conclusion options: {waiting.error}")
    selected_id = waiting.position_candidates[0]["position_candidate_id"]
    completed = conclusion.select(waiting.workflow_id, [selected_id])
    if completed.status != "COMPLETED":
        raise AssertionError(f"Failed to finalize Conclusion: {completed.error}")
    return PMPMessage.model_validate(
        conclusion.repository.read_json(
            conclusion.repository.playwright_outbox_dir / f"{completed.workflow_id}.json"
        )
    )


def make_playwright_manager(
    data_dir: Path,
    provider: MockModelProvider | None = None,
    *,
    max_revisions: int = 2,
) -> PlaywrightManager:
    provider = provider or MockModelProvider()
    registry = PlaywrightRegistry(provider, {})
    return PlaywrightManager(
        registry,
        PlaywrightWorkflowRepository(data_dir),
        max_revisions=max_revisions,
    )
