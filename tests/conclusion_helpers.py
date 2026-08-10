from __future__ import annotations

import asyncio
from pathlib import Path

from common.models.pmp import PMPMessage
from conclusion.manager import ConclusionManager
from conclusion.registry import ConclusionRegistry
from providers.mock_provider import MockModelProvider
from storage.conclusion_workflow_repository import ConclusionWorkflowRepository
from tests.deliberation_helpers import make_deliberation_handoff, make_manager as make_deliberation_manager


def make_conclusion_handoff(
    data_dir: Path,
    provider: MockModelProvider | None = None,
) -> PMPMessage:
    provider = provider or MockModelProvider()
    deliberation = make_deliberation_manager(data_dir, provider)
    state = asyncio.run(
        deliberation.start_from_message(make_deliberation_handoff())
    )
    if state.status != "COMPLETED":
        raise AssertionError(f"Failed to create Deliberation handoff: {state.error}")
    return PMPMessage.model_validate(
        deliberation.repository.read_json(
            deliberation.repository.conclusion_outbox_dir / f"{state.workflow_id}.json"
        )
    )


def make_conclusion_manager(
    data_dir: Path,
    provider: MockModelProvider | None = None,
    *,
    max_revisions: int = 2,
) -> ConclusionManager:
    provider = provider or MockModelProvider()
    registry = ConclusionRegistry(provider, {})
    return ConclusionManager(
        registry,
        ConclusionWorkflowRepository(data_dir),
        max_revisions=max_revisions,
    )
