from __future__ import annotations

from deliberation.manager import DeliberationManager
from deliberation.schemas.deliberation_result import DeliberationResult
from deliberation.state import DeliberationWorkflowState
from producer.manager import ProducerManager
from producer.state import ProducerWorkflowState
from researcher.manager import ResearcherManager
from researcher.schemas.research_report import ResearchReport
from researcher.schemas.human_evidence import (
    HumanActorSource,
    HumanEvidenceDecisionType,
    HumanEvidenceGateSummary,
)
from researcher.state import ResearcherWorkflowState
from conclusion.manager import ConclusionManager
from conclusion.schemas.conclusion_package import ConclusionPackage
from conclusion.schemas.final_conclusion import FinalConclusion
from conclusion.state import ConclusionWorkflowState
from playwright.manager import PlaywrightManager
from playwright.schemas.final_script_package import FinalScriptPackage
from playwright.state import PlaywrightWorkflowState


async def run_producer(
    manager: ProducerManager,
    *,
    topic: str | None = None,
    progress_callback=None,
) -> ProducerWorkflowState:
    return await manager.start(user_topic=topic, progress_callback=progress_callback)


def load_producer_status(manager: ProducerManager, workflow_id: str) -> ProducerWorkflowState:
    return manager.repository.load(workflow_id)


async def run_researcher(
    manager: ResearcherManager,
    *,
    workflow_id: str,
    progress_callback=None,
) -> ResearcherWorkflowState:
    return await manager.start(workflow_id, progress_callback=progress_callback)


def load_researcher_status(
    manager: ResearcherManager,
    workflow_id: str,
) -> ResearcherWorkflowState:
    return manager.repository.load(workflow_id)


def load_researcher_result(
    manager: ResearcherManager,
    workflow_id: str,
) -> ResearchReport:
    return manager.repository.load_report(workflow_id)


def inspect_researcher_evidence(
    manager: ResearcherManager,
    workflow_id: str,
) -> HumanEvidenceGateSummary:
    return manager.inspect_human_evidence_gate(workflow_id)


def decide_researcher_evidence(
    manager: ResearcherManager,
    workflow_id: str,
    decision: HumanEvidenceDecisionType,
    *,
    reason: str,
) -> ResearcherWorkflowState:
    return manager.decide_human_evidence(
        workflow_id,
        decision,
        reason=reason,
        actor_source=HumanActorSource.DISCORD,
    )


def recover_researcher_evidence(
    manager: ResearcherManager,
    workflow_id: str,
) -> ResearcherWorkflowState:
    return manager.recover_human_evidence_gate(workflow_id)


async def run_deliberation(
    manager: DeliberationManager,
    *,
    workflow_id: str,
    progress_callback=None,
) -> DeliberationWorkflowState:
    return await manager.start(workflow_id, progress_callback=progress_callback)


async def resume_deliberation(
    manager: DeliberationManager,
    *,
    workflow_id: str,
    progress_callback=None,
) -> DeliberationWorkflowState:
    return await manager.resume(workflow_id, progress_callback=progress_callback)


async def recover_deliberation(
    manager: DeliberationManager,
    *,
    workflow_id: str,
    progress_callback=None,
) -> DeliberationWorkflowState:
    return await manager.recover(workflow_id, progress_callback=progress_callback)


def load_deliberation_status(
    manager: DeliberationManager,
    workflow_id: str,
) -> DeliberationWorkflowState:
    return manager.repository.load(workflow_id)


def load_deliberation_result(
    manager: DeliberationManager,
    workflow_id: str,
) -> DeliberationResult:
    return manager.repository.load_result(workflow_id)


async def run_conclusion(
    manager: ConclusionManager,
    *,
    workflow_id: str,
    progress_callback=None,
) -> ConclusionWorkflowState:
    return await manager.start(workflow_id, progress_callback=progress_callback)


async def resume_conclusion(
    manager: ConclusionManager,
    *,
    workflow_id: str,
    progress_callback=None,
) -> ConclusionWorkflowState:
    return await manager.resume(workflow_id, progress_callback=progress_callback)


async def integrate_conclusion_candidates(
    manager: ConclusionManager,
    *,
    workflow_id: str,
    candidate_ids: list[str],
    user_instruction: str | None = None,
    progress_callback=None,
) -> ConclusionWorkflowState:
    return await manager.integrate_candidates(
        workflow_id,
        candidate_ids,
        user_instruction=user_instruction,
        progress_callback=progress_callback,
    )


def select_conclusion(
    manager: ConclusionManager,
    *,
    workflow_id: str,
    candidate_id: str,
) -> ConclusionWorkflowState:
    return manager.select(workflow_id, [candidate_id])


def load_conclusion_status(
    manager: ConclusionManager,
    workflow_id: str,
) -> ConclusionWorkflowState:
    return manager.repository.load(workflow_id)


def load_conclusion_package(
    manager: ConclusionManager,
    workflow_id: str,
) -> ConclusionPackage:
    return manager.repository.load_package(workflow_id)


def load_final_conclusion(
    manager: ConclusionManager,
    workflow_id: str,
) -> FinalConclusion:
    return manager.repository.load_final_conclusion(workflow_id)


async def run_playwright(
    manager: PlaywrightManager,
    *,
    workflow_id: str,
    progress_callback=None,
) -> PlaywrightWorkflowState:
    return await manager.start(workflow_id, progress_callback=progress_callback)


async def resume_playwright(
    manager: PlaywrightManager,
    *,
    workflow_id: str,
    progress_callback=None,
) -> PlaywrightWorkflowState:
    return await manager.resume(workflow_id, progress_callback=progress_callback)


def load_playwright_status(
    manager: PlaywrightManager,
    workflow_id: str,
) -> PlaywrightWorkflowState:
    return manager.repository.load(workflow_id)


def load_playwright_result(
    manager: PlaywrightManager,
    workflow_id: str,
) -> FinalScriptPackage:
    return manager.repository.load_final_package(workflow_id)
