import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from common.models.pmp import MessageType, PMPMessage
from deliberation.manager import DeliberationManager
from providers.mock_provider import MockModelProvider
from tests.deliberation_helpers import make_deliberation_handoff, make_manager, make_report


class _OpenRouterWorkflowProvider(MockModelProvider):
    """Network-free provider double proving routing is provider-independent."""

    provider_id = "openrouter"


class DeliberationManagerTests(unittest.TestCase):
    def test_normal_flow_writes_result_and_conclusion_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            handoff = make_deliberation_handoff()
            state = asyncio.run(manager.start_from_message(handoff))
            self.assertEqual(state.status, "COMPLETED")
            self.assertTrue(state.conclusion_sent)
            self.assertTrue((Path(temporary) / "artifacts" / "deliberation_results" / f"{state.workflow_id}.json").exists())
            self.assertTrue((Path(temporary) / "outbox" / "conclusion" / f"{state.workflow_id}.json").exists())

    def test_workflow_identifier_namespaces_are_disjoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            analysis_ids = {
                payload["analysis_id"] for payload in state.analysis_results.values()
            }
            analysis_ids.add(state.counterargument_analysis["analysis_id"])
            task_ids = {payload["task_id"] for payload in state.analysis_results.values()}
            task_ids.add(state.counterargument_analysis["task_id"])
            integration_ids = {
                state.initial_integration["integration_id"],
                state.final_integration["integration_id"],
            }
            self.assertFalse(analysis_ids & task_ids)
            self.assertFalse(analysis_ids & integration_ids)
            self.assertFalse(task_ids & integration_ids)
            self.assertTrue(
                state.counterargument_analysis["analysis_id"].startswith(
                    "counterargument_analysis_"
                )
            )

    def test_primary_analysts_run_concurrently(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(delay_seconds=0.03)
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            self.assertGreaterEqual(provider.max_active_deliberation_calls, 3)

    def test_one_primary_failure_can_complete_with_conditions(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_agent_ids={"deliberation.stakeholder_response_analyst"}
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.review_result["status"], "approved_with_conditions")
            self.assertIn("deliberation.stakeholder_response_analyst", state.failed_agents)

    def test_any_single_primary_failure_can_complete_with_conditions(self):
        agent_ids = (
            "deliberation.argument_analyst",
            "deliberation.causal_structural_analyst",
            "deliberation.stakeholder_response_analyst",
        )
        for agent_id in agent_ids:
            with self.subTest(agent_id=agent_id), tempfile.TemporaryDirectory() as temporary:
                provider = MockModelProvider(fail_agent_ids={agent_id})
                manager = make_manager(Path(temporary), provider)
                state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
                self.assertEqual(state.status, "COMPLETED")
                self.assertEqual(state.review_result["status"], "approved_with_conditions")
                self.assertIn(agent_id, state.failed_agents)

    def test_two_primary_failures_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_agent_ids={
                    "deliberation.causal_structural_analyst",
                    "deliberation.stakeholder_response_analyst",
                }
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "BLOCKED")
            self.assertFalse(state.conclusion_sent)

    def test_all_primary_failures_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_agent_ids={
                    "deliberation.argument_analyst",
                    "deliberation.causal_structural_analyst",
                    "deliberation.stakeholder_response_analyst",
                }
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "FAILED")

    def test_counterargument_failure_fails_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_agent_ids={"deliberation.counterargument_analyst"}
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "FAILED")
            self.assertFalse(state.conclusion_sent)

    def test_quality_reviewer_failure_fails_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(fail_schemas={"DeliberationQualityReviewOutput"})
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "FAILED")

    def test_recover_quality_failure_reuses_all_completed_checkpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(fail_schemas={"DeliberationQualityReviewOutput"})
            manager = make_manager(Path(temporary), provider)
            failed = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(failed.status, "FAILED")
            checkpoints = {
                "analysis_results": failed.analysis_results,
                "initial_integration": failed.initial_integration,
                "counterargument_analysis": failed.counterargument_analysis,
                "final_integration": failed.final_integration,
                "deterministic_validation": failed.deterministic_validation,
            }
            calls_before_recovery = len(provider.calls)

            provider.fail_schemas.clear()
            recovered = asyncio.run(manager.recover(failed.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(
                provider.calls[calls_before_recovery:],
                ["DeliberationQualityReviewOutput"],
            )
            for field, expected in checkpoints.items():
                self.assertEqual(getattr(recovered, field), expected, field)

    def test_recover_quality_failure_refreshes_validation_only_for_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(fail_schemas={"DeliberationQualityReviewOutput"})
            manager = make_manager(Path(temporary), provider)
            failed = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            legacy_validation = {
                "passed": True,
                "findings": [],
                "metrics": {
                    "primary_analysis_count": 3,
                    "analysis_id_count": 0,
                    "claim_id_count": 0,
                    "viewpoint_count": 0,
                    "referenced_evidence_count": 0,
                    "evidence_id_count": 3,
                    "source_id_count": 3,
                    "revision_count": 0,
                },
            }
            failed.deterministic_validation = legacy_validation
            manager.repository.save(failed)
            provider.fail_schemas.clear()

            recovered = asyncio.run(manager.recover(failed.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(recovered.deterministic_validation, legacy_validation)
            request = next(
                message
                for message in reversed(recovered.message_history)
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            )
            review_validation = request.payload["deterministic_validation"]
            self.assertEqual(review_validation["schema_version"], "2.0")
            self.assertGreater(review_validation["metrics"]["analysis_id_count"], 0)
            self.assertGreater(review_validation["metrics"]["integration_id_count"], 0)

    def test_recovery_trace_repairs_legacy_parents_and_review_attempts_in_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(fail_schemas={"DeliberationQualityReviewOutput"})
            manager = make_manager(Path(temporary), provider)
            failed = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            for message in failed.message_history:
                if message.message_type in {
                    MessageType.DELIBERATION_TASK_ASSIGNMENT.value,
                    MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value,
                }:
                    message.parent_message_id = None
                    message.metadata.retry_count = 0
            manager.repository.save(failed)
            provider.fail_schemas.clear()

            recovered = asyncio.run(manager.recover(failed.workflow_id))

            request = next(
                message
                for message in reversed(recovered.message_history)
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            )
            trace = request.payload["pmp_routing_trace"]
            primary_assignments = [
                item
                for item in trace
                if item["message_type"] == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
                and item["receiver_agent_id"].startswith("deliberation.")
                and item["receiver_agent_id"] != "deliberation.counterargument_analyst"
            ]
            self.assertTrue(all(item["parent_message_id"] for item in primary_assignments))
            counter_assignment = next(
                item
                for item in trace
                if item["receiver_agent_id"] == "deliberation.counterargument_analyst"
                and item["message_type"] == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            )
            self.assertIsNotNone(counter_assignment["parent_message_id"])
            review_assignments = [
                item
                for item in trace
                if item["message_type"]
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            ]
            self.assertEqual(
                [item["retry_count"] for item in review_assignments],
                [0, 1],
            )
            self.assertEqual(review_assignments[0]["status"], "failed")
            self.assertTrue(review_assignments[-1]["stage"].endswith(".current"))
            self.assertIsNotNone(review_assignments[-1]["parent_message_id"])

    def test_recover_blocked_quality_review_reruns_reviewer_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["blocked", "blocked"]
            )
            manager = make_manager(Path(temporary), provider)
            blocked = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            calls_before = len(provider.calls)

            recovered = asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(recovered.status, "BLOCKED")
            self.assertEqual(
                provider.calls[calls_before:],
                ["DeliberationQualityReviewOutput"],
            )

    def test_recover_legacy_blocked_with_repair_routes_becomes_researcher_return(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["blocked", "mixed_real_case"]
            )
            manager = make_manager(Path(temporary), provider)
            blocked = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            high_cost_checkpoints = {
                "analysis_results": blocked.analysis_results,
                "initial_integration": blocked.initial_integration,
                "counterargument_analysis": blocked.counterargument_analysis,
                "final_integration": blocked.final_integration,
                "deterministic_validation": blocked.deterministic_validation,
            }
            # Compatibility fixture for the observed legacy contradiction:
            # blocked status accompanied by executable repair routes.
            blocked.review_result["revision_scope"] = "researcher_return"
            blocked.review_result["revision_targets"] = [
                "deliberation.counterargument_analyst",
                "deliberation.manager",
            ]
            blocked.review_result["upstream_revision_requests"] = [
                {
                    "revision_request_id": "upstream_req_legacy_repairable",
                    "target_agent_id": "researcher.manager",
                }
            ]
            manager.repository.save(blocked)
            calls_before = len(provider.calls)

            recovered = asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(recovered.status, "WAITING_UPSTREAM_REVISION")
            self.assertEqual(recovered.revision_count, 0)
            self.assertEqual(recovered.upstream_revision_count, 1)
            self.assertTrue(recovered.awaiting_upstream_revision)
            self.assertEqual(
                recovered.pending_revision_targets,
                [
                    "deliberation.stakeholder_response_analyst",
                    "deliberation.counterargument_analyst",
                    "deliberation.manager",
                ],
            )
            self.assertEqual(
                provider.calls[calls_before:],
                ["DeliberationQualityReviewOutput"],
            )
            for field, expected in high_cost_checkpoints.items():
                self.assertEqual(getattr(recovered, field), expected, field)

    def test_recover_missing_final_integration_reruns_final_and_review_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider()
            manager = make_manager(Path(temporary), provider)
            completed = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            initial = completed.initial_integration
            counterargument = completed.counterargument_analysis
            completed.status = "FAILED"
            completed.final_integration = None
            completed.deterministic_validation = None
            completed.review_result = None
            completed.conclusion_sent = False
            completed.completed_at = None
            completed.error = {"message": "simulated crash before final integration checkpoint"}
            for checkpoint in (
                "final_integration",
                "deterministic_validation",
                "quality_review",
            ):
                completed.checkpoint_revisions.pop(checkpoint, None)
            manager.repository.save(completed)
            calls_before_recovery = len(provider.calls)

            recovered = asyncio.run(manager.recover(completed.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(recovered.initial_integration, initial)
            self.assertEqual(recovered.counterargument_analysis, counterargument)
            self.assertEqual(
                provider.calls[calls_before_recovery:],
                ["FinalIntegratedAnalysis", "DeliberationQualityReviewOutput"],
            )

    def test_recover_completed_workflow_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider()
            manager = make_manager(Path(temporary), provider)
            completed = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            calls_before_recovery = len(provider.calls)
            recovered = asyncio.run(manager.recover(completed.workflow_id))
            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(len(provider.calls), calls_before_recovery)

    def test_revision_reruns_argument_and_all_downstream_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required", "approved"]
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.revision_count, 1)
            self.assertEqual(provider.agent_calls.count("deliberation.argument_analyst"), 2)
            self.assertEqual(provider.agent_calls.count("deliberation.causal_structural_analyst"), 1)
            self.assertEqual(provider.agent_calls.count("deliberation.stakeholder_response_analyst"), 1)
            self.assertEqual(provider.agent_calls.count("deliberation.counterargument_analyst"), 2)

    def test_two_revision_required_reviews_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required", "revision_required"]
            )
            manager = make_manager(Path(temporary), provider, max_revisions=2)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 2)
            self.assertFalse(state.conclusion_sent)

    def test_upstream_evidence_request_waits_and_writes_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["upstream_evidence_required"]
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "WAITING_UPSTREAM_REVISION")
            path = Path(temporary) / "outbox" / "researcher_revision" / f"{state.workflow_id}.json"
            self.assertTrue(path.exists())
            message = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "research_revision_request")

    def test_researcher_return_precedes_internal_revision_when_both_are_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=[
                    "mixed_internal_and_upstream",
                    "approved",
                ]
            )
            manager = make_manager(Path(temporary), provider)
            waiting = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
            self.assertEqual(
                provider.agent_calls.count("deliberation.argument_analyst"),
                1,
            )
            self.assertEqual(waiting.revision_count, 0)
            self.assertEqual(waiting.upstream_revision_count, 1)
            self.assertTrue(waiting.awaiting_upstream_revision)
            self.assertEqual(
                waiting.pending_revision_targets,
                ["deliberation.argument_analyst"],
            )
            revised = make_deliberation_handoff(
                make_report(waiting.workflow_id),
                message_type=MessageType.RESEARCH_REVISION_RESULT,
            )
            manager.repository.write_json_atomic(
                manager.repository.researcher_outbox_dir / f"{waiting.workflow_id}.json",
                revised.model_dump(mode="json"),
            )
            completed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(completed.revision_count, 1)
            self.assertEqual(
                provider.agent_calls.count("deliberation.argument_analyst"),
                2,
            )
            self.assertFalse(completed.awaiting_upstream_revision)
            self.assertEqual(completed.pending_revision_targets, [])

    def test_mixed_revision_dependency_plans_resume_only_required_stages(self):
        cases = {
            "mixed_internal_and_upstream": {
                "argument": 2,
                "counterargument": 2,
                "initial": 2,
            },
            "mixed_upstream_counterargument": {
                "argument": 1,
                "counterargument": 2,
                "initial": 1,
            },
            "mixed_upstream_manager": {
                "argument": 1,
                "counterargument": 2,
                "initial": 2,
            },
            "mixed_upstream_all": {
                "argument": 2,
                "counterargument": 2,
                "initial": 2,
            },
        }
        for decision, expected in cases.items():
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as temporary:
                provider = MockModelProvider(
                    deliberation_review_decisions=[decision, "approved"]
                )
                manager = make_manager(Path(temporary), provider)
                waiting = asyncio.run(
                    manager.start_from_message(make_deliberation_handoff())
                )
                self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
                self.assertEqual(waiting.revision_count, 0)
                revised = make_deliberation_handoff(
                    make_report(waiting.workflow_id),
                    message_type=MessageType.RESEARCH_REVISION_RESULT,
                )
                manager.repository.write_json_atomic(
                    manager.repository.researcher_outbox_dir
                    / f"{waiting.workflow_id}.json",
                    revised.model_dump(mode="json"),
                )
                completed = asyncio.run(manager.resume(waiting.workflow_id))
                self.assertEqual(completed.status, "COMPLETED")
                self.assertEqual(completed.revision_count, 1)
                self.assertEqual(
                    provider.agent_calls.count("deliberation.argument_analyst"),
                    expected["argument"],
                )
                self.assertEqual(
                    provider.agent_calls.count("deliberation.counterargument_analyst"),
                    expected["counterargument"],
                )
                self.assertEqual(
                    provider.calls.count("InitialIntegratedAnalysis"),
                    expected["initial"],
                )

    def test_demo_safe_mode_stops_before_revision_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required"]
            )
            manager = make_manager(
                Path(temporary),
                provider,
                demo_safe_mode=True,
            )
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(
                provider.calls.count("DeliberationQualityReviewOutput"),
                1,
            )
            self.assertEqual(
                provider.agent_calls.count("deliberation.argument_analyst"),
                1,
            )
            self.assertEqual(provider.calls[-1], "DeliberationQualityReviewOutput")
            self.assertIn("automatic internal revision", state.error["message"])

    def test_demo_safe_mode_preserves_approved_and_true_blocked_decisions(self):
        for decision, expected_status in (
            ("approved", "COMPLETED"),
            ("blocked", "BLOCKED"),
        ):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as temporary:
                provider = MockModelProvider(
                    deliberation_review_decisions=[decision]
                )
                manager = make_manager(
                    Path(temporary),
                    provider,
                    demo_safe_mode=True,
                )
                state = asyncio.run(
                    manager.start_from_message(make_deliberation_handoff())
                )
                self.assertEqual(state.status, expected_status)
                self.assertEqual(
                    provider.calls.count("DeliberationQualityReviewOutput"),
                    1,
                )

    def test_demo_safe_mode_allows_researcher_return_without_agent_redispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["upstream_evidence_required"]
            )
            manager = make_manager(
                Path(temporary),
                provider,
                demo_safe_mode=True,
            )
            waiting = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
            self.assertTrue(waiting.awaiting_upstream_revision)
            self.assertEqual(waiting.revision_count, 0)
            self.assertEqual(waiting.upstream_revision_count, 1)
            self.assertEqual(waiting.pending_revision_targets, [])
            self.assertFalse(waiting.conclusion_sent)
            self.assertEqual(
                provider.agent_calls.count("deliberation.argument_analyst"),
                1,
            )
            self.assertEqual(
                provider.agent_calls.count("deliberation.counterargument_analyst"),
                1,
            )
            outbox = (
                Path(temporary)
                / "outbox"
                / "researcher_revision"
                / f"{waiting.workflow_id}.json"
            )
            self.assertTrue(outbox.exists())

    def test_recover_demo_safe_review_block_reuses_high_cost_checkpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=[
                    "revision_required",
                    "revision_required",
                ]
            )
            manager = make_manager(
                Path(temporary),
                provider,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            checkpoints = {
                "analysis_results": blocked.analysis_results,
                "initial_integration": blocked.initial_integration,
                "counterargument_analysis": blocked.counterargument_analysis,
                "final_integration": blocked.final_integration,
                "deterministic_validation": blocked.deterministic_validation,
            }
            call_count = len(provider.calls)
            recovered = asyncio.run(manager.recover(blocked.workflow_id))
            self.assertEqual(recovered.status, "BLOCKED")
            self.assertEqual(
                provider.calls[call_count:],
                ["DeliberationQualityReviewOutput"],
            )
            for field, expected in checkpoints.items():
                self.assertEqual(getattr(recovered, field), expected, field)

    def test_upstream_revision_can_resume_with_new_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["upstream_evidence_required", "approved"]
            )
            manager = make_manager(Path(temporary), provider)
            initial = make_deliberation_handoff()
            waiting = asyncio.run(manager.start_from_message(initial))
            revised = make_deliberation_handoff(
                make_report(waiting.workflow_id),
                message_type=MessageType.RESEARCH_REVISION_RESULT,
            )
            manager.repository.write_json_atomic(
                manager.repository.researcher_outbox_dir / f"{waiting.workflow_id}.json",
                revised.model_dump(mode="json"),
            )
            completed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(completed.upstream_revision_count, 1)
            self.assertEqual(completed.revision_count, 1)
            self.assertEqual(
                provider.agent_calls.count("deliberation.argument_analyst"),
                1,
            )
            self.assertEqual(
                provider.agent_calls.count("deliberation.causal_structural_analyst"),
                1,
            )
            self.assertEqual(
                provider.agent_calls.count("deliberation.stakeholder_response_analyst"),
                1,
            )

    def test_real_mixed_case_persists_and_restores_pending_plan_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["mixed_real_case", "approved"]
            )
            data_dir = Path(temporary)
            manager = make_manager(data_dir, provider)
            waiting = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
            self.assertEqual(waiting.revision_count, 0)
            self.assertEqual(waiting.upstream_revision_count, 1)
            self.assertEqual(
                waiting.pending_revision_targets,
                [
                    "deliberation.stakeholder_response_analyst",
                    "deliberation.counterargument_analyst",
                    "deliberation.manager",
                ],
            )
            self.assertEqual(len(waiting.pending_upstream_revision_request_ids), 2)
            self.assertEqual(waiting.pending_revision_iteration, 1)
            self.assertTrue(waiting.awaiting_upstream_revision)
            self.assertEqual(
                provider.agent_calls.count("deliberation.stakeholder_response_analyst"),
                1,
            )

            restarted = make_manager(data_dir, provider)
            restored = restarted.repository.load(waiting.workflow_id)
            self.assertEqual(restored.pending_revision_targets, waiting.pending_revision_targets)
            revised = make_deliberation_handoff(
                make_report(waiting.workflow_id),
                message_type=MessageType.RESEARCH_REVISION_RESULT,
            )
            restarted.repository.write_json_atomic(
                restarted.repository.researcher_outbox_dir
                / f"{waiting.workflow_id}.json",
                revised.model_dump(mode="json"),
            )
            completed = asyncio.run(restarted.resume(waiting.workflow_id))
            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(completed.revision_count, 1)
            self.assertEqual(
                provider.agent_calls.count("deliberation.stakeholder_response_analyst"),
                2,
            )
            self.assertEqual(
                provider.agent_calls.count("deliberation.counterargument_analyst"),
                2,
            )
            self.assertEqual(
                completed.revision_history[-1].target_agent_ids,
                waiting.pending_revision_targets,
            )
            calls_after_completion = len(provider.calls)
            with self.assertRaisesRegex(ValueError, "not waiting"):
                asyncio.run(restarted.resume(waiting.workflow_id))
            self.assertEqual(len(provider.calls), calls_after_completion)

    def test_mixed_revision_respects_safe_mode_and_max_revision_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            safe_provider = MockModelProvider(
                deliberation_review_decisions=["mixed_real_case"]
            )
            safe_manager = make_manager(
                Path(temporary) / "safe",
                safe_provider,
                demo_safe_mode=True,
            )
            waiting_safe = asyncio.run(
                safe_manager.start_from_message(make_deliberation_handoff())
            )
            self.assertEqual(waiting_safe.status, "WAITING_UPSTREAM_REVISION")
            self.assertEqual(waiting_safe.revision_count, 0)
            self.assertEqual(waiting_safe.upstream_revision_count, 1)
            self.assertTrue(waiting_safe.awaiting_upstream_revision)
            self.assertEqual(
                waiting_safe.pending_revision_targets,
                [
                    "deliberation.stakeholder_response_analyst",
                    "deliberation.counterargument_analyst",
                    "deliberation.manager",
                ],
            )
            self.assertEqual(
                len(waiting_safe.pending_upstream_revision_request_ids),
                2,
            )
            self.assertEqual(
                safe_provider.agent_calls.count(
                    "deliberation.stakeholder_response_analyst"
                ),
                1,
            )
            self.assertEqual(
                safe_provider.agent_calls.count(
                    "deliberation.counterargument_analyst"
                ),
                1,
            )

            boundary_provider = MockModelProvider(
                deliberation_review_decisions=["mixed_real_case", "approved"]
            )
            boundary_manager = make_manager(
                Path(temporary) / "boundary",
                boundary_provider,
                max_revisions=1,
            )
            waiting = asyncio.run(
                boundary_manager.start_from_message(make_deliberation_handoff())
            )
            boundary_manager.max_revisions = 1
            revised = make_deliberation_handoff(
                make_report(waiting.workflow_id),
                message_type=MessageType.RESEARCH_REVISION_RESULT,
            )
            boundary_manager.repository.write_json_atomic(
                boundary_manager.repository.researcher_outbox_dir
                / f"{waiting.workflow_id}.json",
                revised.model_dump(mode="json"),
            )
            stopped = asyncio.run(boundary_manager.resume(waiting.workflow_id))
            self.assertEqual(stopped.status, "BLOCKED")
            self.assertEqual(stopped.revision_count, 1)
            self.assertEqual(
                boundary_provider.agent_calls.count(
                    "deliberation.stakeholder_response_analyst"
                ),
                1,
            )

    def test_mixed_revision_routing_is_provider_independent(self):
        for provider_class in (MockModelProvider, _OpenRouterWorkflowProvider):
            with self.subTest(provider=provider_class.provider_id), tempfile.TemporaryDirectory() as temporary:
                provider = provider_class(
                    deliberation_review_decisions=["mixed_real_case"]
                )
                manager = make_manager(Path(temporary), provider)
                waiting = asyncio.run(
                    manager.start_from_message(make_deliberation_handoff())
                )
                self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
                self.assertEqual(waiting.revision_count, 0)
                self.assertTrue(waiting.awaiting_upstream_revision)

    def test_recover_continues_technical_failure_after_pending_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["mixed_real_case", "approved"]
            )
            manager = make_manager(Path(temporary), provider)
            waiting = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            revised = make_deliberation_handoff(
                make_report(waiting.workflow_id),
                message_type=MessageType.RESEARCH_REVISION_RESULT,
            )
            manager.repository.write_json_atomic(
                manager.repository.researcher_outbox_dir
                / f"{waiting.workflow_id}.json",
                revised.model_dump(mode="json"),
            )
            provider.fail_schemas.add("FinalIntegratedAnalysis")
            failed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.revision_count, 1)
            self.assertFalse(failed.awaiting_upstream_revision)
            stakeholder_calls = provider.agent_calls.count(
                "deliberation.stakeholder_response_analyst"
            )
            counterargument_calls = provider.agent_calls.count(
                "deliberation.counterargument_analyst"
            )

            provider.fail_schemas.clear()
            recovered = asyncio.run(manager.recover(waiting.workflow_id))
            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(recovered.revision_count, 1)
            self.assertEqual(
                provider.agent_calls.count("deliberation.stakeholder_response_analyst"),
                stakeholder_calls,
            )
            self.assertEqual(
                provider.agent_calls.count("deliberation.counterargument_analyst"),
                counterargument_calls,
            )

    def test_recover_does_not_replace_resume_for_upstream_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["upstream_evidence_required"]
            )
            manager = make_manager(Path(temporary), provider)
            waiting = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            with self.assertRaisesRegex(ValueError, r"use resume\(\) instead"):
                asyncio.run(manager.recover(waiting.workflow_id))

    def test_unapproved_research_report_is_rejected_before_state_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            handoff = make_deliberation_handoff(
                make_report(review_status="revision_required")
            )
            with self.assertRaises(ValueError):
                asyncio.run(manager.start_from_message(handoff))

    def test_invalid_researcher_routing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            handoff = make_deliberation_handoff()
            raw = handoff.model_dump(mode="json")
            raw["sender_agent_id"] = "producer.manager"
            invalid = PMPMessage.model_validate(raw)
            with self.assertRaises(ValueError):
                asyncio.run(manager.start_from_message(invalid))

    def test_large_evidence_set_is_bounded_per_task(self):
        report = make_report(evidence_count=51)
        tasks = DeliberationManager._create_analysis_tasks(report)
        self.assertEqual(len(tasks), 3)
        self.assertTrue(all(len(task.target_evidence_ids) <= 50 for task in tasks))

    def test_conclusion_handoff_contains_canonical_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            path = Path(temporary) / "outbox" / "conclusion" / f"{state.workflow_id}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
            required = {
                "problem_definition",
                "claim_structure",
                "key_assumptions",
                "evidence_relationships",
                "causal_model",
                "structural_factors",
                "stakeholder_structure",
                "existing_response_evaluation",
                "counterarguments",
                "alternative_interpretations",
                "trade_offs",
                "uncertainties",
                "analysis_perspectives",
                "unresolved_issues",
                "research_gaps",
                "source_traceability",
                "quality_review",
            }
            self.assertFalse(required - payload.keys())
            self.assertLessEqual(len(payload["analysis_perspectives"]), 3)

    def test_workflow_sequence_places_counterargument_after_initial_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider()
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            self.assertLess(provider.calls.index("InitialIntegratedAnalysis"), provider.calls.index("CounterargumentAnalysisResult"))
            self.assertLess(provider.calls.index("CounterargumentAnalysisResult"), provider.calls.index("FinalIntegratedAnalysis"))
            self.assertLess(provider.calls.index("FinalIntegratedAnalysis"), provider.calls.index("DeliberationQualityReviewOutput"))

    def test_quality_reviewer_receives_verifiable_pmp_and_checkpoint_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            request = next(
                message
                for message in state.message_history
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            )
            routing_trace = request.payload["pmp_routing_trace"]
            checkpoint_trace = request.payload["checkpoint_trace"]
            self.assertTrue(
                all(item["workflow_id"] == state.workflow_id for item in routing_trace)
            )
            self.assertEqual(routing_trace[-1]["message_id"], request.message_id)
            self.assertEqual(
                [item["stage"] for item in checkpoint_trace],
                [
                    "primary_analyses",
                    "initial_integration",
                    "counterargument",
                    "final_integration",
                    "deterministic_validation",
                    "quality_review_request",
                ],
            )
            counter_response = next(
                message
                for message in state.message_history
                if message.sender_agent_id == "deliberation.counterargument_analyst"
                and message.message_type == MessageType.DELIBERATION_TASK_RESULT.value
            )
            self.assertEqual(request.parent_message_id, counter_response.message_id)

    def test_start_is_idempotent_for_saved_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider()
            manager = make_manager(Path(temporary), provider)
            handoff = make_deliberation_handoff()
            manager.repository.write_json_atomic(
                manager.repository.researcher_outbox_dir / f"{handoff.workflow_id}.json",
                handoff.model_dump(mode="json"),
            )
            first = asyncio.run(manager.start(handoff.workflow_id))
            calls = len(provider.calls)
            second = asyncio.run(manager.start(handoff.workflow_id))
            self.assertEqual(first.status, second.status)
            self.assertEqual(len(provider.calls), calls)


if __name__ == "__main__":
    unittest.main()
