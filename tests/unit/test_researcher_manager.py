import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.models.errors import RetryableAgentError
from common.models.pmp import MessageStatus, MessageType, PMPMessage, PMPMetadata
from common.provider_retry import ProviderRetryStatus
from deliberation.manager import DeliberationManager
from deliberation.registry import DeliberationRegistry
from providers.mock import researcher_fixtures
from providers.mock_provider import MockModelProvider
from researcher.manager import ResearcherManager
from researcher.schemas.human_evidence import (
    HumanActorSource,
    HumanEvidenceDecisionType,
)
from researcher.registry import ResearcherRegistry
from researcher.schemas.source import ResearchSource
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from storage.deliberation_workflow_repository import DeliberationWorkflowRepository
from tests.researcher_helpers import make_handoff, valid_source


class ResearcherManagerTests(unittest.TestCase):
    @staticmethod
    def make_external_revision_request(
        state,
        *,
        requests: list[dict] | None = None,
    ) -> PMPMessage:
        requests = requests or [
            {
                "revision_request_id": "revision_request_stakeholder",
                "target_agent_id": "researcher.manager",
                "research_question_id": "rq_employment",
                "affected_claim_ids": ["claim_1"],
                "missing_evidence_description": "Add traceable official evidence",
                "preferred_source_categories": ["GOVERNMENT"],
                "required_scope": {"research_scope": ["Japan"]},
                "acceptance_conditions": ["Provide a source-level citation"],
                "requesting_agent_id": "deliberation.stakeholder_response_analyst",
                "source_finding_ids": ["qf_001"],
            },
            {
                "revision_request_id": "revision_request_stance",
                "target_agent_id": "researcher.manager",
                "research_question_id": "rq_views",
                "affected_claim_ids": ["claim_2"],
                "missing_evidence_description": "Clarify stance and claim relationship",
                "preferred_source_categories": ["NEWS"],
                "required_scope": {"research_scope": ["Japan"]},
                "acceptance_conditions": ["Identify the evidence stance"],
                "requesting_agent_id": "deliberation.argument_analyst",
                "source_finding_ids": ["qf_003"],
            },
        ]
        return PMPMessage.create(
            workflow_id=state.workflow_id,
            sender_agent_id="deliberation.manager",
            receiver_agent_id="researcher.manager",
            message_type=MessageType.RESEARCH_REVISION_REQUEST,
            objective="Collect evidence required to continue Deliberation",
            payload={
                "research_report_id": state.research_report["research_report_id"],
                "revision_requests": requests,
                "quality_review_id": "review_external",
            },
            constraints={
                "preserve_research_plan_scope": True,
                "return_updated_research_report": True,
            },
            metadata=PMPMetadata(status=MessageStatus.REVISION_REQUIRED),
        )

    @staticmethod
    def save_external_revision_request(repository, request: PMPMessage) -> None:
        repository.write_json_atomic(
            repository.researcher_revision_inbox_dir / f"{request.workflow_id}.json",
            request.model_dump(mode="json"),
        )

    def run_manager(
        self,
        provider: MockModelProvider,
        *,
        max_revisions: int = 3,
        demo_safe_mode: bool = False,
        auto_human_decision: bool = True,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        if demo_safe_mode:
            data_dir_environment = patch.dict(
                os.environ,
                {"PRDCP_DATA_DIR": temporary.name},
            )
            data_dir_environment.start()
            self.addCleanup(data_dir_environment.stop)
        repository = ResearcherWorkflowRepository(Path(temporary.name))
        provider.reservation_root = (
            repository.data_dir / "provider_call_reservations"
        )
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=demo_safe_mode),
            repository,
            max_revisions=max_revisions,
            demo_safe_mode=demo_safe_mode,
        )
        state = asyncio.run(manager.start_from_message(make_handoff()))
        if state.status == "WAITING_HUMAN_EVIDENCE_REVIEW" and auto_human_decision:
            summary = manager.inspect_human_evidence_gate(state.workflow_id)
            state = manager.decide_human_evidence(
                state.workflow_id,
                (
                    HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
                    if summary.evidence_sufficiency_findings
                    else HumanEvidenceDecisionType.ACCEPT
                ),
                reason="Explicit unit-test Human Evidence Gate fixture",
                actor_source=HumanActorSource.MOCK_FIXTURE,
            )
        return state, repository, manager

    @staticmethod
    def finalize_human_gate(state, manager):
        if state.status != "WAITING_HUMAN_EVIDENCE_REVIEW":
            return state
        summary = manager.inspect_human_evidence_gate(state.workflow_id)
        return manager.decide_human_evidence(
            state.workflow_id,
            (
                HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
                if summary.evidence_sufficiency_findings
                else HumanEvidenceDecisionType.ACCEPT
            ),
            reason="Explicit unit-test Human Evidence Gate fixture",
            actor_source=HumanActorSource.MOCK_FIXTURE,
        )

    def test_normal_flow_writes_report_and_deliberation_outbox(self):
        state, repository, _manager = self.run_manager(MockModelProvider())
        self.assertEqual(state.status, "COMPLETED")
        self.assertTrue(state.deliberation_sent)
        self.assertEqual(len(state.research_tasks), 7)
        self.assertEqual(len(state.collected_sources), 7)
        self.assertTrue((repository.reports_dir / f"{state.workflow_id}.json").exists())
        outbox = repository.deliberation_outbox_dir / f"{state.workflow_id}.json"
        self.assertTrue(outbox.exists())
        payload = json.loads(outbox.read_text(encoding="utf-8"))
        self.assertEqual(payload["message_type"], "research_result")
        self.assertEqual(payload["receiver_agent_id"], "deliberation.manager")
        self.assertIn("evidence_items", payload["payload"])

    def test_specialists_run_concurrently(self):
        provider = MockModelProvider(delay_seconds=0.02)
        state, _repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertGreater(provider.max_active_research_calls, 1)

    def test_run_task_blocks_repeat_after_registry_recreation(self):
        provider = MockModelProvider()
        state, repository, manager = self.run_manager(
            provider,
            demo_safe_mode=True,
        )
        task = next(
            item
            for item in state.research_tasks
            if item["target_agent_id"] == "researcher.government_researcher"
        )
        message_count = len(state.message_history)
        provider.calls.clear()
        provider.agent_calls.clear()
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        result = asyncio.run(
            manager.run_task(state.workflow_id, task["task_id"])
        )

        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])
        self.assertEqual(result.status, "PARTIALLY_COMPLETED")
        self.assertEqual(len(result.message_history), message_count + 2)
        self.assertEqual(
            result.message_history[-1].payload["error_class"],
            "NonRetryableAgentError",
        )
        saved = repository.load(state.workflow_id)
        self.assertEqual(saved.status, "PARTIALLY_COMPLETED")
        self.assertEqual(
            saved.agent_results[task["target_agent_id"]][-1]["task_id"],
            task["task_id"],
        )

    def test_run_task_rejects_unknown_task_before_provider_call(self):
        provider = MockModelProvider()
        state, _repository, manager = self.run_manager(
            provider,
            demo_safe_mode=True,
        )
        provider.calls.clear()
        provider.agent_calls.clear()

        with self.assertRaisesRegex(ValueError, "Researcher task not found"):
            asyncio.run(manager.run_task(state.workflow_id, "task_missing"))

        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])

    def test_partial_failure_is_disclosed_and_can_complete_with_conditions(self):
        provider = MockModelProvider(
            fail_agent_ids={"researcher.public_opinion_researcher"}
        )
        state, repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertEqual(state.review_result["status"], "approved_with_conditions")
        self.assertTrue(state.limitations)
        self.assertTrue(
            (repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )

    def test_no_result_is_preserved_as_gap_and_limitation(self):
        provider = MockModelProvider(
            no_result_agent_ids={"researcher.government_researcher"},
            researcher_review_decisions=["approved_with_conditions"],
        )
        state, _repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        report = state.research_report
        self.assertTrue(report["evidence_gaps"])
        self.assertTrue(report["unresolved_questions"])
        self.assertTrue(report["research_limitations"])

    def test_all_specialists_failing_aborts_without_handoff(self):
        provider = MockModelProvider(
            fail_agent_ids={
                "researcher.expert_researcher",
                "researcher.academic_researcher",
                "researcher.government_researcher",
                "researcher.news_researcher",
                "researcher.public_opinion_researcher",
                "researcher.politician_researcher",
                "researcher.industry_researcher",
            }
        )
        state, repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "FAILED")
        self.assertFalse(state.deliberation_sent)
        self.assertFalse(
            (repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )

    def test_cross_category_provider_payload_is_rejected_before_result_save(self):
        provider = MockModelProvider()
        original_generate = provider.generate_structured

        async def inject_cross_category_source(**kwargs):
            input_data = kwargs["input_data"]
            if (
                kwargs["output_schema"].__name__ == "ResearchResult"
                and input_data.get("target_agent_id")
                == "researcher.academic_researcher"
            ):
                provider.calls.append("ResearchResult")
                provider.agent_calls.append("researcher.academic_researcher")
                return {
                    "task_id": input_data["task_id"],
                    "research_question_id": input_data["research_question_id"],
                    "agent_id": "researcher.academic_researcher",
                    "sources": [
                        valid_source(
                            "GOVERNMENT",
                            research_question_ids=[
                                input_data["research_question_id"]
                            ],
                        )
                    ],
                    "search_summary": "wrong category injected",
                    "coverage_status": "COMPLETE",
                    "limitations": [],
                }
            return await original_generate(**kwargs)

        provider.generate_structured = inject_cross_category_source
        state, _repository, _manager = self.run_manager(provider)

        self.assertIn("researcher.academic_researcher", state.failed_agents)
        self.assertEqual(
            state.agent_results.get("researcher.academic_researcher", []),
            [],
        )
        self.assertTrue(
            any(
                "may return only ACADEMIC" in item["message"]
                for item in state.limitations
            )
        )

    def test_revision_required_waits_for_human_without_rerunning_target_agent(self):
        provider = MockModelProvider(
            researcher_review_decisions=["revision_required", "approved"]
        )
        state, _repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertEqual(state.revision_count, 0)
        self.assertEqual(provider.agent_calls.count("researcher.government_researcher"), 1)
        self.assertEqual(provider.agent_calls.count("researcher.academic_researcher"), 1)
        self.assertFalse(
            any(message.message_type == "research_revision_request" for message in state.message_history)
        )
        self.assertEqual(
            state.human_evidence_decision.decision,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS.value,
        )

    def test_external_revision_runs_only_requested_in_plan_agents_and_replies(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        old_evidence_ids = {
            item["evidence_id"] for item in state.research_report["evidence_items"]
        }
        old_report_id = state.research_report["research_report_id"]
        old_internal_count = state.revision_count
        request = self.make_external_revision_request(state)
        self.save_external_revision_request(repository, request)
        provider.calls.clear()
        provider.agent_calls.clear()
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        resumed = self.finalize_human_gate(
            asyncio.run(manager.resume(state.workflow_id)), manager
        )

        self.assertEqual(resumed.status, "COMPLETED_REVISION")
        self.assertEqual(resumed.revision_count, old_internal_count)
        self.assertEqual(resumed.external_revision_count, 1)
        self.assertTrue(resumed.external_revision_reply_sent)
        self.assertEqual(
            set(provider.agent_calls),
            {
                "researcher.government_researcher",
                "researcher.news_researcher",
            },
        )
        self.assertEqual(provider.calls.count("ResearchQualityReviewOutput"), 1)
        # Exact source-level disclosures are no longer duplicated at Report level,
        # so the mock reviewer has no remaining global limitation in this cycle.
        self.assertEqual(resumed.review_result["status"], "approved")
        self.assertEqual(resumed.research_report["research_report_id"], old_report_id)
        new_evidence_ids = {
            item["evidence_id"] for item in resumed.research_report["evidence_items"]
        }
        self.assertTrue(old_evidence_ids < new_evidence_ids)
        outbox = PMPMessage.model_validate(
            repository.read_json(
                repository.deliberation_outbox_dir / f"{state.workflow_id}.json"
            )
        )
        self.assertEqual(outbox.message_type, "research_revision_result")
        self.assertEqual(outbox.parent_message_id, request.message_id)
        self.assertEqual(
            set(outbox.payload["resolved_revision_request_ids"]),
            {"revision_request_stakeholder", "revision_request_stance"},
        )
        external_tasks = [
            item for item in resumed.research_tasks if item["revision_context"] is not None
        ]
        self.assertEqual(len(external_tasks), 2)
        self.assertEqual(
            {
                item["revision_context"]["revision_request_id"]
                for item in external_tasks
            },
            {"revision_request_stakeholder", "revision_request_stance"},
        )
        self.assertTrue(
            all(
                item["revision_context"]["revision_source"] == "deliberation"
                and item["revision_context"]["acceptance_conditions"]
                and item["revision_context"]["required_scope"]
                for item in external_tasks
            )
        )
        self.assertEqual(resumed.external_revision_history[-1].status, "reply_sent")
        self.assertEqual(resumed.external_revision_history[-1].parent_message_id, request.message_id)
        deliberation = DeliberationManager(
            DeliberationRegistry(provider),
            DeliberationWorkflowRepository(repository.data_dir),
        )
        accepted = deliberation._validate_researcher_handoff(outbox, allow_revision=True)
        self.assertEqual(accepted.research_report_id, old_report_id)

        provider.calls.clear()
        provider.agent_calls.clear()
        repeated = asyncio.run(manager.resume(state.workflow_id))
        self.assertEqual(repeated.status, "COMPLETED_REVISION")
        self.assertEqual(repeated.external_revision_count, 1)
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])

    def test_external_revision_rejects_out_of_plan_categories_before_calls(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        request_data = self.make_external_revision_request(state).payload["revision_requests"][0]
        request_data = {**request_data, "preferred_source_categories": ["NEWS"]}
        request = self.make_external_revision_request(state, requests=[request_data])
        self.save_external_revision_request(repository, request)
        provider.calls.clear()
        provider.agent_calls.clear()
        manager = ResearcherManager(ResearcherRegistry(provider), repository)

        with self.assertRaisesRegex(ValueError, "outside the approved Research Plan"):
            asyncio.run(manager.resume(state.workflow_id))

        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])
        self.assertEqual(repository.load(state.workflow_id).external_revision_count, 0)

    def test_external_revision_rejects_duplicate_request_ids_before_calls(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        first = self.make_external_revision_request(state).payload["revision_requests"][0]
        request = self.make_external_revision_request(state, requests=[first, first])
        self.save_external_revision_request(repository, request)
        provider.calls.clear()
        provider.agent_calls.clear()
        manager = ResearcherManager(ResearcherRegistry(provider), repository)

        with self.assertRaisesRegex(ValueError, "IDs must be unique"):
            asyncio.run(manager.resume(state.workflow_id))

        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])
        self.assertEqual(repository.load(state.workflow_id).external_revision_count, 0)

    def test_external_revision_safe_mode_stops_before_internal_redispatch(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        request = self.make_external_revision_request(
            state,
            requests=[self.make_external_revision_request(state).payload["revision_requests"][0]],
        )
        self.save_external_revision_request(repository, request)
        provider.researcher_review_decisions.append("revision_required")
        provider.calls.clear()
        provider.agent_calls.clear()
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        blocked = asyncio.run(manager.resume(state.workflow_id))

        self.assertEqual(blocked.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(blocked.revision_count, 0)
        self.assertEqual(blocked.external_revision_count, 1)
        self.assertFalse(blocked.external_revision_reply_sent)
        self.assertEqual(
            provider.agent_calls,
            ["researcher.government_researcher"],
        )
        self.assertEqual(provider.calls.count("ResearchQualityReviewOutput"), 1)
        outbox = repository.read_json(
            repository.deliberation_outbox_dir / f"{state.workflow_id}.json"
        )
        self.assertEqual(outbox["message_type"], "research_result")

    def test_external_revision_recovers_after_results_without_redispatch(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        request = self.make_external_revision_request(state)
        self.save_external_revision_request(repository, request)
        provider.calls.clear()
        provider.agent_calls.clear()
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        with patch.object(
            manager,
            "_build_report",
            side_effect=ValueError("simulated integration interruption"),
        ):
            with self.assertRaisesRegex(ValueError, "simulated integration interruption"):
                asyncio.run(manager.resume(state.workflow_id))

        interrupted = repository.load(state.workflow_id)
        self.assertEqual(interrupted.external_revision_count, 1)
        self.assertEqual(interrupted.external_revision_status, "REPORT_INTEGRATING")
        external_task_count = len(interrupted.research_tasks) - len(state.research_tasks)
        self.assertEqual(external_task_count, 2)
        result_count = sum(len(items) for items in interrupted.agent_results.values())
        self.assertEqual(result_count, 9)
        message_count = len(interrupted.message_history)
        provider.calls.clear()
        provider.agent_calls.clear()
        restarted = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        completed = self.finalize_human_gate(
            asyncio.run(restarted.resume(state.workflow_id)), restarted
        )

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        self.assertEqual(completed.external_revision_count, 1)
        self.assertEqual(completed.external_revision_status, "COMPLETED_REVISION")
        self.assertEqual(provider.agent_calls, [])
        self.assertEqual(provider.calls.count("ResearchQualityReviewOutput"), 1)
        self.assertEqual(len(completed.research_tasks), len(interrupted.research_tasks))
        self.assertEqual(len(completed.message_history), message_count + 4)
        revision_results = [
            message
            for message in completed.message_history
            if message.message_type == "research_revision_result"
            and message.sender_agent_id == "researcher.manager"
        ]
        self.assertEqual(len(revision_results), 1)
        self.assertEqual(revision_results[0].parent_message_id, request.message_id)

    def test_external_revision_reuses_integrated_report_before_quality_review(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        request = self.make_external_revision_request(state)
        self.save_external_revision_request(repository, request)
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )
        provider.calls.clear()
        provider.agent_calls.clear()

        with patch.object(
            manager,
            "_request_review",
            side_effect=RuntimeError("simulated crash before quality review"),
        ):
            failed = asyncio.run(manager.resume(state.workflow_id))
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.external_revision_status, "QUALITY_REVIEWING")
        integrated_report = failed.research_report
        provider.calls.clear()
        provider.agent_calls.clear()
        restarted = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        with patch.object(
            restarted,
            "_build_report",
            side_effect=AssertionError("integrated report must be reused"),
        ):
            completed = self.finalize_human_gate(
                asyncio.run(restarted.resume(state.workflow_id)), restarted
            )

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        self.assertEqual(completed.research_report["research_report_id"], integrated_report["research_report_id"])
        self.assertEqual(provider.agent_calls, [])
        self.assertEqual(provider.calls, ["ResearchQualityReviewOutput"])

    def test_external_revision_reuses_saved_quality_review_response(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        request = self.make_external_revision_request(state)
        self.save_external_revision_request(repository, request)
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )
        original_request_review = manager._request_review

        async def crash_after_saved_response(*args, **kwargs):
            await original_request_review(*args, **kwargs)
            raise RuntimeError("simulated crash after quality review response")

        provider.calls.clear()
        provider.agent_calls.clear()
        with patch.object(
            manager,
            "_request_review",
            side_effect=crash_after_saved_response,
        ):
            failed = asyncio.run(manager.resume(state.workflow_id))

        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.external_revision_status, "QUALITY_REVIEWING")
        self.assertEqual(provider.calls.count("ResearchQualityReviewOutput"), 1)
        provider.calls.clear()
        provider.agent_calls.clear()
        restarted = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        completed = self.finalize_human_gate(
            asyncio.run(restarted.resume(state.workflow_id)), restarted
        )

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        self.assertEqual(provider.agent_calls, [])
        self.assertEqual(provider.calls, [])
        external_review_requests = [
            message
            for message in completed.message_history
            if message.receiver_agent_id == "researcher.quality_reviewer"
            and message.payload.get("task_id") == "research_quality_review_external_1"
        ]
        self.assertEqual(len(external_review_requests), 1)

    def test_external_revision_revalidates_persisted_provider_payload_without_call(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        request = self.make_external_revision_request(state)
        self.save_external_revision_request(repository, request)
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        async def save_old_contract_error(workflow_state, report, *, external_revision=None):
            review_task_id = manager._quality_review_task_id(
                workflow_state,
                external_revision,
            )
            review_request = PMPMessage.create(
                workflow_id=workflow_state.workflow_id,
                sender_agent_id="researcher.manager",
                receiver_agent_id="researcher.quality_reviewer",
                message_type=MessageType.TASK,
                objective="Review with the old local contract",
                payload={"task_id": review_task_id},
            )
            invalid_payload = researcher_fixtures.quality_review(
                {"research_report": report.model_dump(mode="json")},
                "approved_with_conditions",
            )
            invalid_payload["findings"] = [
                {
                    "finding_id": "finding_legacy_manager_target",
                    "finding_type": "EVIDENCE_SUFFICIENCY_FINDING",
                    "severity": "MINOR",
                    "research_question_id": None,
                    "target_agent_id": "researcher.manager",
                    "issue": "Legacy local contract rejected a Manager-owned limitation",
                    "required_action": "Retain the limitation without Provider execution",
                }
            ]
            error_response = PMPMessage.create(
                workflow_id=workflow_state.workflow_id,
                parent_message_id=review_request.message_id,
                sender_agent_id="researcher.quality_reviewer",
                receiver_agent_id="researcher.manager",
                message_type=MessageType.ERROR,
                objective="Old contract validation error",
                payload={
                    "message": "old schema rejected researcher.manager",
                    "error_class": "PayloadValidationError",
                    "task_id": review_task_id,
                    "invalid_payload": invalid_payload,
                },
            )
            workflow_state.message_history.extend([review_request, error_response])
            repository.save(workflow_state)
            raise ValueError("simulated old local contract rejection")

        provider.calls.clear()
        provider.agent_calls.clear()
        with patch.object(
            manager,
            "_request_review",
            side_effect=save_old_contract_error,
        ):
            failed = asyncio.run(manager.resume(state.workflow_id))
        self.assertEqual(failed.status, "FAILED")
        provider.calls.clear()
        provider.agent_calls.clear()
        restarted = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        completed = self.finalize_human_gate(
            asyncio.run(restarted.resume(state.workflow_id)), restarted
        )

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])
        recovered = [
            message
            for message in completed.message_history
            if message.message_type == "review"
            and message.metadata.notes
            == "Recovered from persisted invalid_payload without a provider call"
        ]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(
            recovered[0].payload["findings"][0]["target_agent_id"],
            "researcher.manager",
        )

    def test_legacy_validation_error_uses_one_deterministic_contract_repair_task(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        request = self.make_external_revision_request(state)
        self.save_external_revision_request(repository, request)
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        async def save_legacy_error(workflow_state, _report, *, external_revision=None):
            review_task_id = manager._quality_review_task_id(
                workflow_state,
                external_revision,
            )
            review_request = PMPMessage.create(
                workflow_id=workflow_state.workflow_id,
                sender_agent_id="researcher.manager",
                receiver_agent_id="researcher.quality_reviewer",
                message_type=MessageType.TASK,
                objective="Review with an unpersisted old provider payload",
                payload={"task_id": review_task_id},
            )
            error_response = PMPMessage.create(
                workflow_id=workflow_state.workflow_id,
                parent_message_id=review_request.message_id,
                sender_agent_id="researcher.quality_reviewer",
                receiver_agent_id="researcher.manager",
                message_type=MessageType.ERROR,
                objective="Legacy payload validation error",
                payload={
                    "message": "legacy payload was not persisted",
                    "error_class": "PayloadValidationError",
                    "task_id": review_task_id,
                },
            )
            workflow_state.message_history.extend([review_request, error_response])
            repository.save(workflow_state)
            raise ValueError("simulated legacy contract rejection")

        with patch.object(
            manager,
            "_request_review",
            side_effect=save_legacy_error,
        ):
            failed = asyncio.run(manager.resume(state.workflow_id))
        self.assertEqual(failed.status, "FAILED")
        provider.calls.clear()
        provider.agent_calls.clear()
        restarted = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        completed = self.finalize_human_gate(
            asyncio.run(restarted.resume(state.workflow_id)), restarted
        )

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        self.assertEqual(completed.external_revision_count, 1)
        self.assertEqual(provider.calls, ["ResearchQualityReviewOutput"])
        repair_task_ids = [
            message.payload.get("task_id")
            for message in completed.message_history
            if message.receiver_agent_id == "researcher.quality_reviewer"
            and message.message_type == "task"
            and message.payload.get("task_id", "").endswith("_contract_v2")
        ]
        self.assertEqual(
            repair_task_ids,
            ["research_quality_review_external_1_contract_v2"],
        )

    def test_external_revision_reconciles_reply_written_before_state_save(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider)
        request = self.make_external_revision_request(state)
        self.save_external_revision_request(repository, request)
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )
        original_save = repository.save
        interrupted = False

        def crash_on_first_completed_save(workflow_state):
            nonlocal interrupted
            if (
                not interrupted
                and workflow_state.external_revision_status == "COMPLETED_REVISION"
            ):
                interrupted = True
                raise RuntimeError("simulated crash after revision reply write")
            original_save(workflow_state)

        provider.calls.clear()
        provider.agent_calls.clear()
        with patch.object(repository, "save", side_effect=crash_on_first_completed_save):
            with self.assertRaisesRegex(
                RuntimeError,
                "simulated crash after revision reply write",
            ):
                waiting = asyncio.run(manager.resume(state.workflow_id))
                self.finalize_human_gate(waiting, manager)

        self.assertTrue(interrupted)
        outbox_path = repository.deliberation_outbox_dir / f"{state.workflow_id}.json"
        outbox_before = outbox_path.read_bytes()
        provider.calls.clear()
        provider.agent_calls.clear()
        restarted = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        completed = restarted.recover_human_evidence_gate(state.workflow_id)

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        self.assertTrue(completed.external_revision_reply_sent)
        self.assertEqual(provider.agent_calls, [])
        self.assertEqual(provider.calls, [])
        self.assertEqual(outbox_path.read_bytes(), outbox_before)
        revision_results = [
            message
            for message in completed.message_history
            if message.message_type == "research_revision_result"
            and message.parent_message_id == request.message_id
        ]
        self.assertEqual(len(revision_results), 1)

    def test_quality_review_task_ids_are_cycle_specific_and_stable(self):
        provider = MockModelProvider(
            researcher_review_decisions=["revision_required", "approved"]
        )
        state, _repository, _manager = self.run_manager(provider)
        review_requests = [
            message
            for message in state.message_history
            if message.receiver_agent_id == "researcher.quality_reviewer"
            and message.message_type == "task"
        ]
        self.assertEqual(
            [message.payload["task_id"] for message in review_requests],
            ["research_quality_review_initial"],
        )

    def test_safe_mode_allows_external_quality_review_with_distinct_reservation(self):
        provider = MockModelProvider()
        state, repository, manager = self.run_manager(provider, demo_safe_mode=True)
        request = self.make_external_revision_request(
            state,
            requests=[self.make_external_revision_request(state).payload["revision_requests"][0]],
        )
        self.save_external_revision_request(repository, request)
        provider.calls.clear()
        provider.agent_calls.clear()

        completed = self.finalize_human_gate(
            asyncio.run(manager.resume(state.workflow_id)), manager
        )

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        review_task_ids = [
            message.payload["task_id"]
            for message in completed.message_history
            if message.receiver_agent_id == "researcher.quality_reviewer"
            and message.message_type == "task"
        ]
        self.assertEqual(
            review_task_ids,
            ["research_quality_review_initial", "research_quality_review_external_1"],
        )
        reservations = (
            repository.data_dir
            / "provider_call_reservations"
            / "mock"
            / state.workflow_id
        )
        self.assertTrue((reservations / "research_quality_review_initial.json").exists())
        self.assertTrue((reservations / "research_quality_review_external_1.json").exists())

    def test_safe_mode_allows_a_later_external_quality_review_cycle(self):
        provider = MockModelProvider()
        state, repository, manager = self.run_manager(provider, demo_safe_mode=True)
        first_item = self.make_external_revision_request(state).payload[
            "revision_requests"
        ][0]
        first_request = self.make_external_revision_request(
            state,
            requests=[first_item],
        )
        self.save_external_revision_request(repository, first_request)
        first_completed = self.finalize_human_gate(
            asyncio.run(manager.resume(state.workflow_id)), manager
        )
        second_item = dict(first_item)
        second_item["revision_request_id"] = "revision_request_external_two"
        second_item["source_finding_ids"] = ["qf_external_two"]
        second_request = self.make_external_revision_request(
            first_completed,
            requests=[second_item],
        )
        self.save_external_revision_request(repository, second_request)
        provider.calls.clear()
        provider.agent_calls.clear()

        second_completed = self.finalize_human_gate(
            asyncio.run(manager.resume(state.workflow_id)), manager
        )

        self.assertEqual(second_completed.status, "COMPLETED_REVISION")
        self.assertEqual(second_completed.external_revision_count, 2)
        review_task_ids = [
            message.payload["task_id"]
            for message in second_completed.message_history
            if message.receiver_agent_id == "researcher.quality_reviewer"
            and message.message_type == "task"
        ]
        self.assertEqual(
            review_task_ids,
            [
                "research_quality_review_initial",
                "research_quality_review_external_1",
                "research_quality_review_external_2",
            ],
        )
        self.assertEqual(provider.calls.count("ResearchQualityReviewOutput"), 1)
        reservations = (
            repository.data_dir
            / "provider_call_reservations"
            / "mock"
            / state.workflow_id
        )
        self.assertTrue((reservations / "research_quality_review_external_1.json").exists())
        self.assertTrue((reservations / "research_quality_review_external_2.json").exists())

    def test_safe_mode_blocks_retry_of_the_same_external_quality_review(self):
        provider = MockModelProvider()
        state, repository, _manager = self.run_manager(provider, demo_safe_mode=True)
        request = self.make_external_revision_request(
            state,
            requests=[self.make_external_revision_request(state).payload["revision_requests"][0]],
        )
        self.save_external_revision_request(repository, request)
        provider.fail_schemas.add("ResearchQualityReviewOutput")
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        first_failed = asyncio.run(manager.resume(state.workflow_id))

        self.assertEqual(first_failed.status, "FAILED")
        self.assertEqual(first_failed.external_revision_count, 1)
        provider.fail_schemas.clear()
        provider.calls.clear()
        provider.agent_calls.clear()
        restarted = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        repeated = asyncio.run(restarted.resume(state.workflow_id))

        self.assertEqual(repeated.status, "FAILED")
        self.assertEqual(repeated.external_revision_count, 1)
        self.assertIn("repeated call", repeated.error["message"])
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])

    def test_explicit_human_revise_stops_before_provider_execution(self):
        provider = MockModelProvider(researcher_review_decisions=["revision_required"])
        state, _repository, manager = self.run_manager(
            provider,
            demo_safe_mode=True,
            auto_human_decision=False,
        )
        self.assertEqual(state.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        provider.calls.clear()
        provider.agent_calls.clear()

        blocked = asyncio.run(manager.revise(state.workflow_id))

        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(
            blocked.error["code"],
            "EVIDENCE_REVISION_PROVIDER_AUTHORIZATION_REQUIRED",
        )
        self.assertEqual(blocked.revision_count, 0)
        self.assertIsNotNone(blocked.evidence_revision_plan)
        self.assertFalse(blocked.human_evidence_decision.provider_calls_authorized)
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])

    def test_explicit_human_revise_is_durable_and_cannot_be_redecided(self):
        provider = MockModelProvider(researcher_review_decisions=["revision_required"])
        state, repository, manager = self.run_manager(
            provider,
            demo_safe_mode=True,
            auto_human_decision=False,
        )
        first = asyncio.run(manager.revise(state.workflow_id))
        provider.calls.clear()
        provider.agent_calls.clear()
        recreated = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        recovered = recreated.recover_human_evidence_gate(state.workflow_id)
        with self.assertRaisesRegex(ValueError, "decision already exists"):
            asyncio.run(recreated.revise(state.workflow_id))

        self.assertEqual(first.status, "BLOCKED")
        self.assertEqual(recovered.status, "BLOCKED")
        self.assertEqual(
            recovered.human_evidence_decision.decision,
            HumanEvidenceDecisionType.REVISE.value,
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(provider.agent_calls, [])

    def test_report_excludes_other_provider_and_cross_category_legacy_results(self):
        provider = MockModelProvider()
        state, repository, manager = self.run_manager(provider, demo_safe_mode=True)
        academic_result = dict(
            state.agent_results["researcher.academic_researcher"][0]
        )
        academic_result["task_id"] = "task_openrouter_academic"
        academic_source = valid_source(
            category="ACADEMIC",
            source_id="source_openrouter_academic",
            evidence_id="evidence_openrouter_academic",
            url="https://example.com/academic-real",
        )
        cross_category_source = valid_source(
            category="GOVERNMENT",
            source_id="source_wrong_category",
            evidence_id="evidence_wrong_category",
            url="https://example.com/government-wrong-category",
        )
        academic_result["sources"] = [academic_source, cross_category_source]
        state.agent_results["researcher.academic_researcher"].append(
            academic_result
        )
        repository.save(state)
        openrouter_reservation = (
            repository.data_dir
            / "provider_call_reservations"
            / "openrouter"
            / state.workflow_id
            / "task_openrouter_academic.json"
        )
        repository.write_json_atomic(
            openrouter_reservation,
            {
                "workflow_id": state.workflow_id,
                "task_id": "task_openrouter_academic",
                "agent_id": "researcher.academic_researcher",
                "provider": "OpenRouterModelProvider",
                "model_id": "test-model",
                "reserved_at": "2026-08-16T00:00:00+00:00",
            },
        )
        provider.provider_id = "openrouter"

        report = manager._build_report(repository.load(state.workflow_id))

        self.assertEqual(
            [source.source_id for source in report.sources],
            ["source_openrouter_academic"],
        )
        self.assertTrue(
            any(
                "Provider provenance filtering excluded 7" in limitation
                for limitation in report.research_limitations
            )
        )
        self.assertTrue(
            any(
                "category validation excluded 1" in limitation
                for limitation in report.research_limitations
            )
        )

    def test_operator_retry_replays_only_retryable_external_quality_review_once(self):
        provider = MockModelProvider()
        state, repository, manager = self.run_manager(provider, demo_safe_mode=True)
        request = self.make_external_revision_request(
            state,
            requests=[self.make_external_revision_request(state).payload["revision_requests"][0]],
        )
        self.save_external_revision_request(repository, request)
        original_generate = provider.generate_structured
        failed_once = False

        async def fail_quality_review_once(**kwargs):
            nonlocal failed_once
            if kwargs["output_schema"].__name__ == "ResearchQualityReviewOutput" and not failed_once:
                failed_once = True
                provider.calls.append("ResearchQualityReviewOutput")
                raise RetryableAgentError("injected connection reset")
            return await original_generate(**kwargs)

        provider.generate_structured = fail_quality_review_once
        first_failed = asyncio.run(manager.resume(state.workflow_id))
        self.assertEqual(first_failed.status, "FAILED")
        self.assertEqual(
            first_failed.message_history[-1].payload["error_class"],
            "RetryableAgentError",
        )
        specialist_calls_before_retry = list(provider.agent_calls)
        provider.generate_structured = original_generate

        completed = self.finalize_human_gate(
            asyncio.run(manager.retry_provider_call(state.workflow_id)), manager
        )

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        self.assertEqual(provider.agent_calls, specialist_calls_before_retry)
        review_task_ids = [
            message.payload["task_id"]
            for message in completed.message_history
            if message.receiver_agent_id == "researcher.quality_reviewer"
            and message.message_type == "task"
        ]
        self.assertEqual(
            review_task_ids[-2:],
            [
                "research_quality_review_external_1",
                "research_quality_review_external_1_operator_retry_1",
            ],
        )
        authorization = manager.provider_retry_store.for_original_task(
            workflow_id=state.workflow_id,
            provider_id="mock",
            original_task_id="research_quality_review_external_1",
        )
        self.assertIsNotNone(authorization)
        self.assertEqual(authorization.status, ProviderRetryStatus.CONSUMED.value)
        reservations = (
            repository.data_dir
            / "provider_call_reservations"
            / "mock"
            / state.workflow_id
        )
        self.assertTrue(
            (reservations / "research_quality_review_external_1.json").exists()
        )
        self.assertTrue(
            (
                reservations
                / "research_quality_review_external_1_operator_retry_1.json"
            ).exists()
        )
        calls_after_completion = len(provider.calls)
        with self.assertRaisesRegex(ValueError, "must be FAILED or resuming"):
            asyncio.run(manager.retry_provider_call(state.workflow_id))
        self.assertEqual(len(provider.calls), calls_after_completion)

    def test_pending_operator_retry_survives_manager_recreation(self):
        provider = MockModelProvider()
        state, repository, manager = self.run_manager(provider, demo_safe_mode=True)
        request = self.make_external_revision_request(
            state,
            requests=[self.make_external_revision_request(state).payload["revision_requests"][0]],
        )
        self.save_external_revision_request(repository, request)
        original_generate = provider.generate_structured

        async def fail_quality_review(**kwargs):
            if kwargs["output_schema"].__name__ == "ResearchQualityReviewOutput":
                provider.calls.append("ResearchQualityReviewOutput")
                raise RetryableAgentError("injected interruption")
            return await original_generate(**kwargs)

        provider.generate_structured = fail_quality_review
        failed = asyncio.run(manager.resume(state.workflow_id))
        self.assertEqual(failed.status, "FAILED")
        first_authorization = manager.authorize_provider_retry(state.workflow_id)
        interrupted = repository.load(state.workflow_id)
        interrupted.status = "REVIEWING"
        repository.save(interrupted)
        provider.generate_structured = original_generate
        recreated = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        completed = self.finalize_human_gate(
            asyncio.run(recreated.retry_provider_call(state.workflow_id)), recreated
        )

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        saved_authorization = recreated.provider_retry_store.for_original_task(
            workflow_id=state.workflow_id,
            provider_id="mock",
            original_task_id="research_quality_review_external_1",
        )
        self.assertEqual(
            saved_authorization.authorization_id,
            first_authorization.authorization_id,
        )
        self.assertEqual(saved_authorization.status, ProviderRetryStatus.CONSUMED.value)

    def test_failed_operator_retry_cannot_authorize_retry_of_retry(self):
        provider = MockModelProvider()
        state, repository, manager = self.run_manager(provider, demo_safe_mode=True)
        request = self.make_external_revision_request(
            state,
            requests=[self.make_external_revision_request(state).payload["revision_requests"][0]],
        )
        self.save_external_revision_request(repository, request)
        original_generate = provider.generate_structured

        async def always_fail_quality_review(**kwargs):
            if kwargs["output_schema"].__name__ == "ResearchQualityReviewOutput":
                provider.calls.append("ResearchQualityReviewOutput")
                raise RetryableAgentError("injected persistent outage")
            return await original_generate(**kwargs)

        provider.generate_structured = always_fail_quality_review
        first_failed = asyncio.run(manager.resume(state.workflow_id))
        self.assertEqual(first_failed.status, "FAILED")
        retry_failed = asyncio.run(manager.retry_provider_call(state.workflow_id))
        self.assertEqual(retry_failed.status, "FAILED")
        self.assertTrue(
            retry_failed.message_history[-1].payload["task_id"].endswith(
                "_operator_retry_1"
            )
        )
        calls_after_retry = len(provider.calls)

        with self.assertRaisesRegex(ValueError, "cannot authorize another retry"):
            asyncio.run(manager.retry_provider_call(state.workflow_id))

        self.assertEqual(len(provider.calls), calls_after_retry)

    def test_revision_required_review_never_auto_loops(self):
        provider = MockModelProvider(
            researcher_review_decisions=["revision_required"] * 3
        )
        state, repository, _manager = self.run_manager(
            provider, auto_human_decision=False
        )
        self.assertEqual(state.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(state.revision_count, 0)
        self.assertEqual(provider.calls.count("ResearchQualityReviewOutput"), 1)
        self.assertFalse(state.deliberation_sent)
        self.assertFalse(
            (repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )

    def test_quality_reviewer_error_fails_safely(self):
        provider = MockModelProvider(fail_schemas={"ResearchQualityReviewOutput"})
        state, repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "FAILED")
        self.assertIn("Quality Reviewer", state.error["message"])
        self.assertFalse(
            (repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )

    def test_duplicate_url_merges_question_ids(self):
        _state, _repository, manager = self.run_manager(MockModelProvider())
        first = ResearchSource.model_validate(valid_source())
        second_data = valid_source(
            source_id="source_2",
            evidence_id="evidence_2",
            research_question_ids=["rq_views"],
            url="https://example.invalid/source?tracking=1",
        )
        second = ResearchSource.model_validate(second_data)
        merged = manager._deduplicate_sources([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].research_question_ids), {"rq_employment", "rq_views"})
        self.assertEqual(
            merged[0].source_specific_metadata["merged_evidence_ids"],
            ["evidence_2"],
        )

    def test_cross_type_duplicate_keeps_representative_metadata_schema(self):
        _state, _repository, manager = self.run_manager(MockModelProvider())
        government = ResearchSource.model_validate(valid_source(category="GOVERNMENT"))
        academic = ResearchSource.model_validate(
            valid_source(
                category="ACADEMIC",
                source_id="source_academic",
                evidence_id="evidence_academic",
                research_question_ids=["rq_views"],
                url=str(government.url),
            )
        )

        merged = manager._deduplicate_sources([government, academic])

        self.assertEqual(len(merged), 1)
        representative = merged[0]
        self.assertEqual(representative.source_type, "GOVERNMENT")
        self.assertEqual(
            set(representative.source_specific_metadata),
            {"organization", "country", "document_type", "merged_evidence_ids"},
        )
        self.assertNotIn("doi", representative.source_specific_metadata)
        self.assertNotIn("peer_reviewed", representative.source_specific_metadata)
        self.assertEqual(
            representative.source_specific_metadata["merged_evidence_ids"],
            ["evidence_academic"],
        )
        manager._validate_source_metadata_contracts(merged)
        ResearchSource.model_validate(representative.model_dump(mode="json"))

    def test_invalid_parent_message_is_rejected(self):
        provider = MockModelProvider()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = ResearcherWorkflowRepository(Path(temporary.name))
        registry = ResearcherRegistry(provider, demo_safe_mode=False)
        original = registry.get("researcher.academic_researcher")

        class InvalidAgent:
            async def execute(self, request):
                response = await original.execute(request)
                data = response.model_dump()
                data["parent_message_id"] = str(__import__("uuid").uuid4())
                return PMPMessage.model_validate(data)

        registry._agents["researcher.academic_researcher"] = InvalidAgent()
        manager = ResearcherManager(registry, repository, demo_safe_mode=False)
        state = asyncio.run(manager.start_from_message(make_handoff()))
        state = self.finalize_human_gate(state, manager)
        self.assertEqual(state.status, "COMPLETED")
        self.assertIn("researcher.academic_researcher", state.failed_agents)
        self.assertEqual(state.review_result["status"], "approved_with_conditions")

    def test_invalid_producer_handoff_routing_is_rejected(self):
        provider = MockModelProvider()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = ResearcherWorkflowRepository(Path(temporary.name))
        manager = ResearcherManager(ResearcherRegistry(provider), repository)
        handoff = make_handoff()
        data = handoff.model_dump()
        data["receiver_agent_id"] = "producer.topic_scout"
        invalid = PMPMessage.model_validate(data)
        with self.assertRaises(ValueError):
            asyncio.run(manager.start_from_message(invalid))


if __name__ == "__main__":
    unittest.main()
