from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from pydantic import BaseModel

from common.models.errors import AgentExecutionError
from producer.schemas.general_opinion import GeneralOpinionOutput
from producer.schemas.research_plan import ResearchPlanOutput
from producer.schemas.review import QualityReviewOutput
from producer.schemas.topic_scout import TopicScoutOutput
from producer.schemas.topic_selector import TopicSelectorOutput
from providers.mock import deliberation_fixtures, playwright_fixtures, producer_fixtures, researcher_fixtures
from providers.mock import conclusion_fixtures
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult
from deliberation.schemas.causal_structural_analysis import CausalStructuralAnalysisResult
from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.integrated_analysis import FinalIntegratedAnalysis, InitialIntegratedAnalysis
from deliberation.schemas.review import DeliberationQualityReviewOutput
from deliberation.schemas.stakeholder_response_analysis import StakeholderResponseAnalysisResult
from researcher.schemas.research_result import ResearchResult
from researcher.schemas.review import ResearchQualityReviewOutput
from conclusion.schemas.decision_evaluation import DecisionEvaluationResult
from conclusion.schemas.decision_integration import DecisionIntegrationResult
from conclusion.schemas.position_candidate import PositionGenerationResult
from conclusion.schemas.review import ConclusionQualityReviewOutput
from playwright.schemas import CitationEditingResult, NarrativeBlueprint, ScriptDraft, VisualPlan


class MockModelProvider:
    """Deterministic provider used to verify control flow without an API key."""

    provider_id = "mock"

    def __init__(
        self,
        *,
        review_decisions: list[str] | None = None,
        researcher_review_decisions: list[str] | None = None,
        deliberation_review_decisions: list[str] | None = None,
        conclusion_review_decisions: list[str] | None = None,
        conclusion_duplicate_candidates_once: bool = False,
        conclusion_blocking_candidate_id: str | None = None,
        playwright_unsupported_once: bool = False,
        playwright_missing_citation_once: bool = False,
        playwright_visual_mismatch_once: bool = False,
        playwright_missing_chart_source_once: bool = False,
        fail_schemas: set[str] | None = None,
        fail_agent_ids: set[str] | None = None,
        no_result_agent_ids: set[str] | None = None,
        delay_seconds: float = 0,
        reservation_root: Path | None = None,
    ) -> None:
        self.reservation_root = reservation_root
        self.review_decisions = deque(review_decisions or [])
        self.researcher_review_decisions = deque(researcher_review_decisions or [])
        self.deliberation_review_decisions = deque(deliberation_review_decisions or [])
        self.conclusion_review_decisions = deque(conclusion_review_decisions or [])
        self.conclusion_duplicate_candidates_once = conclusion_duplicate_candidates_once
        self.conclusion_blocking_candidate_id = conclusion_blocking_candidate_id
        self.playwright_unsupported_once = playwright_unsupported_once
        self.playwright_missing_citation_once = playwright_missing_citation_once
        self.playwright_visual_mismatch_once = playwright_visual_mismatch_once
        self.playwright_missing_chart_source_once = playwright_missing_chart_source_once
        self._conclusion_position_calls = 0
        self._playwright_calls: dict[str, int] = {}
        self.fail_schemas = fail_schemas or set()
        self.fail_agent_ids = fail_agent_ids or set()
        self.no_result_agent_ids = no_result_agent_ids or set()
        self.delay_seconds = delay_seconds
        self.calls: list[str] = []
        self.agent_calls: list[str] = []
        self._active_research_calls = 0
        self.max_active_research_calls = 0
        self._active_deliberation_calls = 0
        self.max_active_deliberation_calls = 0

    async def generate_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        input_data: dict,
        output_schema: type[BaseModel],
        timeout_seconds: int | None = None,
        invocation_reservation_path: Path | None = None,
    ) -> dict:
        del invocation_reservation_path
        schema_name = output_schema.__name__
        self.calls.append(schema_name)
        if schema_name in self.fail_schemas:
            raise AgentExecutionError(f"Mock failure requested for {schema_name}")
        if output_schema is TopicScoutOutput:
            return producer_fixtures.topic_candidates(input_data)
        if output_schema is TopicSelectorOutput:
            first = input_data["topic_candidates"][0]
            return {
                "selected_topic": {
                    "topic_id": first["topic_id"],
                    "title": first["title"],
                    "selection_reason": "一般論が存在し、情報源を用いた検証が可能で、社会的関心もあるため",
                }
            }
        if output_schema is GeneralOpinionOutput:
            return producer_fixtures.general_opinion(input_data)
        if output_schema is ResearchPlanOutput:
            return producer_fixtures.research_plan(input_data)
        if output_schema is QualityReviewOutput:
            decision = self.review_decisions.popleft() if self.review_decisions else "approved"
            return producer_fixtures.quality_review(input_data, decision)
        if output_schema is ResearchResult:
            return await self._research_result(input_data)
        if output_schema is ResearchQualityReviewOutput:
            decision = (
                self.researcher_review_decisions.popleft()
                if self.researcher_review_decisions
                else None
            )
            return researcher_fixtures.quality_review(input_data, decision)
        if output_schema is ArgumentAnalysisResult:
            return await self._deliberation_result(
                input_data,
                deliberation_fixtures.argument_analysis,
            )
        if output_schema is CausalStructuralAnalysisResult:
            return await self._deliberation_result(
                input_data,
                deliberation_fixtures.causal_analysis,
            )
        if output_schema is StakeholderResponseAnalysisResult:
            return await self._deliberation_result(
                input_data,
                deliberation_fixtures.stakeholder_analysis,
            )
        if output_schema is InitialIntegratedAnalysis:
            return deliberation_fixtures.initial_integration(input_data)
        if output_schema is CounterargumentAnalysisResult:
            return await self._deliberation_result(
                input_data,
                deliberation_fixtures.counterargument_analysis,
                track_parallel=False,
            )
        if output_schema is FinalIntegratedAnalysis:
            return deliberation_fixtures.final_integration(input_data)
        if output_schema is DeliberationQualityReviewOutput:
            decision = (
                self.deliberation_review_decisions.popleft()
                if self.deliberation_review_decisions
                else None
            )
            return deliberation_fixtures.quality_review(input_data, decision)
        if output_schema is PositionGenerationResult:
            self._conclusion_position_calls += 1
            return await self._conclusion_result(
                input_data,
                lambda data: conclusion_fixtures.position_generation(
                    data,
                    duplicate=(self.conclusion_duplicate_candidates_once and self._conclusion_position_calls == 1),
                ),
            )
        if output_schema is DecisionEvaluationResult:
            return await self._conclusion_result(
                input_data,
                lambda data: conclusion_fixtures.decision_evaluation(
                    data,
                    blocking_candidate_id=self.conclusion_blocking_candidate_id,
                ),
            )
        if output_schema is DecisionIntegrationResult:
            return await self._conclusion_result(input_data, conclusion_fixtures.decision_integration)
        if output_schema is ConclusionQualityReviewOutput:
            decision = self.conclusion_review_decisions.popleft() if self.conclusion_review_decisions else None
            return await self._conclusion_result(
                {**input_data, "target_agent_id": "conclusion.quality_reviewer"},
                lambda data: conclusion_fixtures.quality_review(data, decision),
            )
        if output_schema is NarrativeBlueprint:
            return await self._playwright_result(input_data, playwright_fixtures.narrative_blueprint)
        if output_schema is ScriptDraft:
            call = self._count_playwright_call(schema_name)
            return await self._playwright_result(
                input_data,
                lambda data: playwright_fixtures.script_draft(
                    data,
                    unsupported=self.playwright_unsupported_once and call == 1,
                ),
            )
        if output_schema is CitationEditingResult:
            call = self._count_playwright_call(schema_name)
            return await self._playwright_result(
                input_data,
                lambda data: playwright_fixtures.citation_editing(
                    data,
                    missing_mapping=self.playwright_missing_citation_once and call == 1,
                ),
            )
        if output_schema is VisualPlan:
            call = self._count_playwright_call(schema_name)
            return await self._playwright_result(
                input_data,
                lambda data: playwright_fixtures.visual_plan(
                    data,
                    mismatch=self.playwright_visual_mismatch_once and call == 1,
                    missing_chart_source=self.playwright_missing_chart_source_once and call == 1,
                ),
            )
        raise AgentExecutionError(f"Unsupported mock output schema: {schema_name}")

    async def _research_result(self, input_data: dict) -> dict:
        agent_id = input_data["target_agent_id"]
        self.agent_calls.append(agent_id)
        if agent_id in self.fail_agent_ids:
            raise AgentExecutionError(f"Mock failure requested for {agent_id}")
        self._active_research_calls += 1
        self.max_active_research_calls = max(
            self.max_active_research_calls,
            self._active_research_calls,
        )
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            return researcher_fixtures.research_result(
                input_data,
                no_result_agent_ids=self.no_result_agent_ids,
            )
        finally:
            self._active_research_calls -= 1

    async def _deliberation_result(
        self,
        input_data: dict,
        fixture,
        *,
        track_parallel: bool = True,
    ) -> dict:
        agent_id = input_data.get("target_agent_id", "deliberation.manager")
        self.agent_calls.append(agent_id)
        if agent_id in self.fail_agent_ids:
            raise AgentExecutionError(f"Mock failure requested for {agent_id}")
        if track_parallel:
            self._active_deliberation_calls += 1
            self.max_active_deliberation_calls = max(
                self.max_active_deliberation_calls,
                self._active_deliberation_calls,
            )
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            return fixture(input_data)
        finally:
            if track_parallel:
                self._active_deliberation_calls -= 1

    async def _conclusion_result(self, input_data: dict, fixture) -> dict:
        agent_id = input_data.get("target_agent_id", "conclusion.manager")
        self.agent_calls.append(agent_id)
        if agent_id in self.fail_agent_ids:
            raise AgentExecutionError(f"Mock failure requested for {agent_id}")
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return fixture(input_data)

    def _count_playwright_call(self, schema_name: str) -> int:
        self._playwright_calls[schema_name] = self._playwright_calls.get(schema_name, 0) + 1
        return self._playwright_calls[schema_name]

    async def _playwright_result(self, input_data: dict, fixture) -> dict:
        agent_id = input_data.get("target_agent_id", "playwright.manager")
        self.agent_calls.append(agent_id)
        if agent_id in self.fail_agent_ids:
            raise AgentExecutionError(f"Mock failure requested for {agent_id}")
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return fixture(input_data)
