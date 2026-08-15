import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.models.pmp import MessageStatus, MessageType, PMPMessage, PMPMetadata
from deliberation.manager import DeliberationManager
from deliberation.registry import DeliberationRegistry
from providers.mock_provider import MockModelProvider
from researcher.manager import ResearcherManager
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
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=demo_safe_mode),
            repository,
            max_revisions=max_revisions,
            demo_safe_mode=demo_safe_mode,
        )
        state = asyncio.run(manager.start_from_message(make_handoff()))
        return state, repository, manager

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

    def test_revision_reruns_only_target_agent(self):
        provider = MockModelProvider(
            researcher_review_decisions=["revision_required", "approved"]
        )
        state, _repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertEqual(state.revision_count, 1)
        self.assertEqual(provider.agent_calls.count("researcher.government_researcher"), 2)
        self.assertEqual(provider.agent_calls.count("researcher.academic_researcher"), 1)
        self.assertTrue(
            any(
                message.message_type == "research_revision_request"
                for message in state.message_history
            )
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

        resumed = asyncio.run(manager.resume(state.workflow_id))

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
        self.assertEqual(resumed.review_result["status"], "approved_with_conditions")
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
        with self.assertRaisesRegex(ValueError, "already been sent"):
            asyncio.run(manager.resume(state.workflow_id))
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

        self.assertEqual(blocked.status, "BLOCKED")
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

        completed = asyncio.run(restarted.resume(state.workflow_id))

        self.assertEqual(completed.status, "COMPLETED_REVISION")
        self.assertEqual(completed.external_revision_count, 1)
        self.assertEqual(completed.external_revision_status, "COMPLETED_REVISION")
        self.assertEqual(provider.agent_calls, [])
        self.assertEqual(provider.calls, ["ResearchQualityReviewOutput"])
        self.assertEqual(len(completed.research_tasks), len(interrupted.research_tasks))
        self.assertEqual(len(completed.message_history), message_count + 3)
        revision_results = [
            message
            for message in completed.message_history
            if message.message_type == "research_revision_result"
            and message.sender_agent_id == "researcher.manager"
        ]
        self.assertEqual(len(revision_results), 1)
        self.assertEqual(revision_results[0].parent_message_id, request.message_id)

    def test_three_revision_required_reviews_block(self):
        provider = MockModelProvider(
            researcher_review_decisions=["revision_required"] * 3
        )
        state, repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.revision_count, 3)
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
