import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from common.models.errors import NonRetryableAgentError, RetryableAgentError
from common.models.pmp import MessageType, PMPMessage
from deliberation.manager import DeliberationManager
from deliberation.schemas.analysis_task import CounterargumentTask, DeliberationAnalysisTask
from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.integrated_analysis import (
    FinalIntegratedAnalysis,
    InitialIntegratedAnalysis,
)
from deliberation.schemas.review import DeterministicValidationResult
from providers.mock_provider import MockModelProvider
from researcher.schemas.research_report import ResearchReport
from tests.deliberation_helpers import make_deliberation_handoff, make_manager, make_report


class _OpenRouterWorkflowProvider(MockModelProvider):
    """Network-free provider double proving routing is provider-independent."""

    provider_id = "openrouter"


class _CaptureInputsProvider(MockModelProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.input_payloads = []

    async def generate_structured(self, **kwargs):
        self.input_payloads.append(
            (
                kwargs["output_schema"].__name__,
                json.loads(json.dumps(kwargs["input_data"])),
            )
        )
        return await super().generate_structured(**kwargs)


class _BudgetRejectingProvider(MockModelProvider):
    def validate_request_budget(self, **_kwargs):
        raise NonRetryableAgentError("fault-injected local context budget exceeded")


class _InvalidStakeholderOnceProvider(MockModelProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.invalidate_next_stakeholder = False

    async def generate_structured(self, **kwargs):
        payload = await super().generate_structured(**kwargs)
        if (
            kwargs["output_schema"].__name__ == "StakeholderResponseAnalysisResult"
            and self.invalidate_next_stakeholder
        ):
            self.invalidate_next_stakeholder = False
            payload["authority_and_capacity"][0]["stakeholder_id"] = "st_missing"
        return payload


class _RejectedStakeholderRevisionOnceProvider(MockModelProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.stakeholder_rejections_remaining = 0

    async def generate_structured(self, **kwargs):
        schema_name = kwargs["output_schema"].__name__
        if (
            schema_name == "StakeholderResponseAnalysisResult"
            and self.stakeholder_rejections_remaining > 0
        ):
            self.stakeholder_rejections_remaining -= 1
            self.calls.append(schema_name)
            raise NonRetryableAgentError(
                "OpenRouter HTTP 400: Request contains an invalid argument.",
                http_status=400,
                provider=self.provider_id,
                model_id=kwargs["model"],
            )
        payload = await super().generate_structured(**kwargs)
        if (
            schema_name == "DeliberationQualityReviewOutput"
            and payload.get("status") == "revision_required"
        ):
            payload["reason"] = "Stakeholder analysis requires a canonical Evidence rerun"
            payload["revision_scope"] = "targeted"
            payload["revision_targets"] = [
                "deliberation.stakeholder_response_analyst"
            ]
            payload["findings"][0]["affected_agent_ids"] = [
                "deliberation.stakeholder_response_analyst"
            ]
        return payload


class _InvalidInitialIntegrationOnceProvider(MockModelProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.invalidate_next_initial = True

    async def generate_structured(self, **kwargs):
        payload = await super().generate_structured(**kwargs)
        if (
            kwargs["output_schema"].__name__ == "InitialIntegratedAnalysis"
            and self.invalidate_next_initial
        ):
            self.invalidate_next_initial = False
            payload["traceability_index"][0]["causal_item_ids"] = ["cc_1"]
        return payload


class _WrongStakeholderProvenanceOnceProvider(MockModelProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.invalidate_next_initial = True

    async def generate_structured(self, **kwargs):
        payload = await super().generate_structured(**kwargs)
        if (
            kwargs["output_schema"].__name__ == "InitialIntegratedAnalysis"
            and self.invalidate_next_initial
        ):
            self.invalidate_next_initial = False
            payload["stakeholder_structure"]["source_analysis_id"] = kwargs[
                "input_data"
            ]["primary_analyses"]["deliberation.causal_structural_analyst"][
                "analysis_id"
            ]
        return payload


class _AmbiguousFinalOnceProvider(MockModelProvider):
    def __init__(self, *, interrupt_final_attempts: int = 1, **kwargs):
        super().__init__(**kwargs)
        self.interrupt_final_attempts = interrupt_final_attempts

    async def generate_structured(self, **kwargs):
        if (
            kwargs["output_schema"].__name__ == "FinalIntegratedAnalysis"
            and self.interrupt_final_attempts > 0
        ):
            self.interrupt_final_attempts -= 1
            self.calls.append("FinalIntegratedAnalysis")
            error = RetryableAgentError(
                "OpenRouter response body was interrupted before completion",
                provider=self.provider_id,
                model_id=kwargs["model"],
                automatic_retry_allowed=False,
            )
            error.__cause__ = IncompleteReadForTest()
            raise error
        return await super().generate_structured(**kwargs)


class _AmbiguousQualityOnceProvider(MockModelProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.interrupt_quality_once = True

    async def generate_structured(self, **kwargs):
        if (
            kwargs["output_schema"].__name__ == "DeliberationQualityReviewOutput"
            and self.interrupt_quality_once
        ):
            self.interrupt_quality_once = False
            self.calls.append("DeliberationQualityReviewOutput")
            raise RetryableAgentError(
                "OpenRouter response body was interrupted before completion",
                provider=self.provider_id,
                model_id=kwargs["model"],
                automatic_retry_allowed=False,
            )
        return await super().generate_structured(**kwargs)


class IncompleteReadForTest(Exception):
    pass


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

    def test_provenance_persists_through_review_result_and_conclusion_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = make_manager(root)
            completed = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            reloaded = manager.repository.load(completed.workflow_id)
            expected = {
                agent_id: payload["analysis_id"]
                for agent_id, payload in reloaded.analysis_results.items()
            }
            for artifact in (
                reloaded.initial_integration,
                reloaded.final_integration,
            ):
                self.assertEqual(
                    artifact["stakeholder_structure"]["source_analysis_id"],
                    expected["deliberation.stakeholder_response_analyst"],
                )
                self.assertEqual(
                    artifact["causal_structure"]["source_analysis_id"],
                    expected["deliberation.causal_structural_analyst"],
                )
                self.assertTrue(
                    all(
                        claim["source_analysis_id"]
                        == expected["deliberation.argument_analyst"]
                        for claim in artifact["key_claims"]
                    )
                )
                self.assertEqual(
                    set(artifact["problem_definition"]["source_analysis_ids"]),
                    set(expected.values()),
                )

            review_request = next(
                message
                for message in reloaded.message_history
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            )
            self.assertEqual(
                review_request.payload["initial_integration"][
                    "stakeholder_structure"
                ]["source_analysis_id"],
                expected["deliberation.stakeholder_response_analyst"],
            )
            self.assertEqual(
                reloaded.deliberation_result["stakeholder_structure"][
                    "source_analysis_id"
                ],
                expected["deliberation.stakeholder_response_analyst"],
            )
            conclusion_message = json.loads(
                (
                    root
                    / "outbox"
                    / "conclusion"
                    / f"{reloaded.workflow_id}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                conclusion_message["payload"]["stakeholder_structure"][
                    "source_analysis_id"
                ],
                expected["deliberation.stakeholder_response_analyst"],
            )
            final_interpretation_ids = {
                identifier
                for entry in reloaded.final_integration["traceability_index"]
                for identifier in entry["causal_item_ids"]
                if identifier.startswith("alt_interp_")
            }
            self.assertTrue(final_interpretation_ids)
            self.assertEqual(
                final_interpretation_ids,
                {
                    identifier
                    for entry in review_request.payload["final_integration"][
                        "traceability_index"
                    ]
                    for identifier in entry["causal_item_ids"]
                    if identifier.startswith("alt_interp_")
                },
            )
            self.assertEqual(
                final_interpretation_ids,
                {
                    identifier
                    for entry in reloaded.deliberation_result["claim_traceability"]
                    for identifier in entry["causal_item_ids"]
                    if identifier.startswith("alt_interp_")
                },
            )
            self.assertEqual(
                final_interpretation_ids,
                {
                    identifier
                    for entry in conclusion_message["payload"]["claim_traceability"]
                    for identifier in entry["causal_item_ids"]
                    if identifier.startswith("alt_interp_")
                },
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

    def test_downstream_recovery_keeps_tolerated_primary_input_set_fixed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                fail_agent_ids={"deliberation.stakeholder_response_analyst"},
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            self.assertEqual(state.status, "COMPLETED")
            raw = json.loads(json.dumps(state.final_integration))
            target = raw["counterargument_dispositions"][0]
            target["resolution"] = "revised_with_research_gap_retained"
            target["research_gap_required"] = True
            target["remaining_uncertainty"] = "Retained research gap"
            state.status = "FAILED"
            state.error = {"message": "saved final resolution contract failure"}
            state.final_integration = None
            state.deterministic_validation = None
            state.deliberation_result = None
            state.review_result = None
            state.conclusion_sent = False
            state.completed_at = None
            for checkpoint in (
                "final_integration",
                "deterministic_validation",
                "quality_review",
            ):
                state.checkpoint_revisions.pop(checkpoint, None)
            state.manager_invalid_payloads[
                "deliberation_manager_final_integration_revision_0"
            ] = {
                "stage": "final_integration",
                "output_schema": "FinalIntegratedAnalysis",
                "invalid_payload": raw,
                "validation_errors": [],
                "recorded_at": "2026-08-24T00:00:00+00:00",
            }
            manager.repository.save(state)
            outbox = manager.repository.conclusion_outbox_dir / f"{state.workflow_id}.json"
            if outbox.exists():
                outbox.unlink()
            calls_before = len(provider.calls)
            initial_before = provider.calls.count("InitialIntegratedAnalysis")
            counter_before = provider.calls.count("CounterargumentAnalysisResult")
            final_before = provider.calls.count("FinalIntegratedAnalysis")

            recovered = asyncio.run(manager.recover(state.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertIn(
                "deliberation.stakeholder_response_analyst",
                recovered.failed_agents,
            )
            self.assertNotIn(
                "deliberation.stakeholder_response_analyst",
                recovered.analysis_results,
            )
            self.assertEqual(
                provider.calls.count("InitialIntegratedAnalysis"), initial_before
            )
            self.assertEqual(
                provider.calls.count("CounterargumentAnalysisResult"), counter_before
            )
            self.assertEqual(
                provider.calls.count("FinalIntegratedAnalysis"), final_before
            )
            self.assertEqual(len(provider.calls), calls_before)
            self.assertEqual(
                recovered.final_integration["counterargument_dispositions"][0][
                    "resolution"
                ],
                "revised",
            )

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
            review_requests = [
                message
                for message in recovered.message_history
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            ]
            self.assertEqual(
                [message.payload["task_id"] for message in review_requests[-2:]],
                [
                    "delib_review_task_revision_0",
                    "delib_review_task_revision_0_recovery_1",
                ],
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

    def test_recover_safe_mode_revision_reuses_saved_review_before_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required", "approved"],
                reservation_root=root / "provider_call_reservations",
            )
            safe_manager = make_manager(root, provider, demo_safe_mode=True)
            blocked = asyncio.run(
                safe_manager.start_from_message(make_deliberation_handoff())
            )
            self.assertEqual(blocked.status, "BLOCKED")
            self.assertEqual(blocked.review_result["status"], "revision_required")
            revision_zero_requests_before = [
                message.message_id
                for message in blocked.message_history
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
                and message.payload.get("task_id") == "delib_review_task_revision_0"
            ]
            calls_before = len(provider.calls)

            active_manager = make_manager(root, provider, demo_safe_mode=False)
            recovered = asyncio.run(active_manager.recover(blocked.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(recovered.revision_count, 1)
            self.assertNotEqual(
                provider.calls[calls_before],
                "DeliberationQualityReviewOutput",
            )
            revision_zero_requests_after = [
                message.message_id
                for message in recovered.message_history
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
                and message.payload.get("task_id") == "delib_review_task_revision_0"
            ]
            self.assertEqual(
                revision_zero_requests_after,
                revision_zero_requests_before,
            )

    def test_safe_mode_internal_revision_requires_explicit_common_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required", "approved"],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider, demo_safe_mode=True)
            blocked = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            self.assertEqual(blocked.status, "BLOCKED")
            self.assertEqual(
                blocked.revision_control.phase,
                "authorization_required",
            )
            self.assertFalse(blocked.awaiting_upstream_revision)
            request_id = blocked.revision_control.active_request_id
            request_path = (
                root
                / "artifacts"
                / "revision_requests"
                / "internal"
                / "deliberation"
                / blocked.workflow_id
                / f"{request_id}.json"
            )
            self.assertTrue(request_path.exists())
            calls_before = len(provider.calls)

            completed = asyncio.run(
                manager.revise(
                    blocked.workflow_id,
                    actor_id="test.operator",
                    actor_source="CLI",
                    reason="Explicitly authorize the saved Deliberation plan",
                )
            )
            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(completed.revision_control.phase, "completed")
            self.assertGreater(len(provider.calls), calls_before)
            result_path = (
                root
                / "artifacts"
                / "revision_results"
                / "internal"
                / "deliberation"
                / blocked.workflow_id
                / f"{request_id}.json"
            )
            self.assertTrue(result_path.exists())

            calls_after_completion = len(provider.calls)
            replayed = asyncio.run(
                manager.revise(
                    blocked.workflow_id,
                    actor_id="test.operator",
                    actor_source="CLI",
                    reason="Explicitly authorize the saved Deliberation plan",
                )
            )
            self.assertEqual(replayed.status, "COMPLETED")
            self.assertEqual(len(provider.calls), calls_after_completion)

    def test_rejected_primary_schema_gets_one_operator_retry_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _RejectedStakeholderRevisionOnceProvider(
                deliberation_review_decisions=["revision_required", "approved"],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider, demo_safe_mode=True)
            blocked = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            self.assertEqual(blocked.status, "BLOCKED")

            provider.stakeholder_rejections_remaining = 1
            failed = asyncio.run(
                manager.revise(
                    blocked.workflow_id,
                    reason="Authorize targeted Stakeholder revision",
                )
            )
            self.assertEqual(failed.status, "FAILED")
            original_task = next(
                message.payload["task_id"]
                for message in reversed(failed.message_history)
                if message.message_type
                == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
                and message.receiver_agent_id
                == "deliberation.stakeholder_response_analyst"
            )

            authorization = manager.authorize_provider_retry(failed.workflow_id)
            self.assertEqual(
                authorization.source_error_class,
                "ProviderRequestSchemaError",
            )
            completed = asyncio.run(
                manager.retry_provider_call(failed.workflow_id)
            )

            self.assertEqual(completed.status, "COMPLETED")
            saved_authorization = manager.provider_retry_store.for_original_task(
                workflow_id=failed.workflow_id,
                provider_id="mock",
                original_task_id=original_task,
            )
            self.assertEqual(saved_authorization.status, "CONSUMED")
            retry_requests = [
                message
                for message in completed.message_history
                if message.message_type
                == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
                and message.payload.get("task_id")
                == f"{original_task}_operator_retry_1"
            ]
            self.assertEqual(len(retry_requests), 1)

    def test_primary_contract_repair_uses_a_distinct_model_after_retry_400(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _RejectedStakeholderRevisionOnceProvider(
                deliberation_review_decisions=["revision_required", "approved"],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider, demo_safe_mode=True)
            blocked = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            provider.stakeholder_rejections_remaining = 2
            first_failure = asyncio.run(
                manager.revise(
                    blocked.workflow_id,
                    reason="Authorize targeted Stakeholder revision",
                )
            )
            self.assertEqual(first_failure.status, "FAILED")
            second_failure = asyncio.run(
                manager.retry_provider_call(first_failure.workflow_id)
            )
            self.assertEqual(second_failure.status, "FAILED")

            completed = asyncio.run(
                manager.repair_provider_contract(
                    second_failure.workflow_id,
                    repair_model_id="vendor/mock-contract-repair",
                )
            )

            self.assertEqual(completed.status, "COMPLETED")
            original_task_id = next(
                message.payload["task_id"]
                for message in second_failure.message_history
                if message.message_type
                == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
                and message.receiver_agent_id
                == "deliberation.stakeholder_response_analyst"
                and message.payload["task_id"].startswith("delib_task_revision_1_")
                and not message.payload["task_id"].endswith("_operator_retry_1")
            )
            authorization = (
                manager.provider_contract_repair_store.for_original_task(
                    workflow_id=second_failure.workflow_id,
                    provider_id="mock",
                    original_task_id=original_task_id,
                )
            )
            self.assertEqual(authorization.status, "CONSUMED")
            repair_reservation = json.loads(
                Path(authorization.reservation_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                repair_reservation["model_id"],
                "vendor/mock-contract-repair",
            )

    def test_recover_reuses_revision_review_after_checkpoint_was_cleared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required", "approved"],
                reservation_root=root / "provider_call_reservations",
            )
            safe_manager = make_manager(root, provider, demo_safe_mode=True)
            blocked = asyncio.run(
                safe_manager.start_from_message(make_deliberation_handoff())
            )
            blocked.status = "FAILED"
            blocked.review_result = None
            blocked.checkpoint_revisions.pop("quality_review", None)
            blocked.error = {
                "message": "Persistent reservation blocked a repeated call"
            }
            safe_manager.repository.save(blocked)
            calls_before = len(provider.calls)

            active_manager = make_manager(root, provider, demo_safe_mode=False)
            recovered = asyncio.run(active_manager.recover(blocked.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(recovered.revision_count, 1)
            self.assertNotEqual(
                provider.calls[calls_before],
                "DeliberationQualityReviewOutput",
            )
            revision_zero_requests = [
                message
                for message in recovered.message_history
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
                and message.payload.get("task_id") == "delib_review_task_revision_0"
            ]
            self.assertEqual(len(revision_zero_requests), 1)

    def test_recover_segregates_saved_challenge_trace_without_manager_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                deliberation_review_decisions=["blocked", "approved"],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider)
            blocked = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            challenge_id = blocked.counterargument_analysis["steelman_arguments"][0][
                "challenge_id"
            ]
            counterargument_id = blocked.counterargument_analysis["counterarguments"][
                0
            ]["counterargument_id"]
            blocked.final_integration["traceability_index"][0][
                "counterargument_ids"
            ] = [challenge_id, counterargument_id]
            blocked.final_integration["traceability_index"][0].pop(
                "challenge_ids", None
            )
            manager.repository.save(blocked)
            calls_before = list(provider.calls)

            recovered = asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(
                provider.calls[len(calls_before) :],
                ["DeliberationQualityReviewOutput"],
            )
            trace = recovered.final_integration["traceability_index"][0]
            self.assertEqual(trace["counterargument_ids"], [counterargument_id])
            self.assertEqual(trace["challenge_ids"], [challenge_id])
            self.assertTrue(recovered.deterministic_validation["passed"])
            audit = recovered.manager_payload_recoveries[-1]
            self.assertEqual(
                audit["compatibility_adapter"],
                "saved_final_challenge_reference_segregation",
            )
            self.assertEqual(audit["provider_call_count"], 0)
            review_requests = [
                message
                for message in recovered.message_history
                if message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            ]
            self.assertTrue(
                review_requests[-1].payload["task_id"].endswith(
                    "_recovery_1_traceability_contract_repair_1"
                )
            )

    def test_saved_challenge_repair_does_not_guess_unknown_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                deliberation_review_decisions=["blocked"],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider)
            blocked = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            blocked.final_integration["traceability_index"][0][
                "counterargument_ids"
            ] = ["challenge_not_in_saved_counterargument"]
            before = json.loads(json.dumps(blocked.final_integration))
            counterargument = CounterargumentAnalysisResult.model_validate(
                blocked.counterargument_analysis
            )
            repaired = manager._recover_saved_final_challenge_traceability(
                blocked, counterargument
            )

            self.assertFalse(repaired)
            self.assertEqual(blocked.final_integration, before)
            prefixed_but_unknown = json.loads(json.dumps(before))
            prefixed_but_unknown["traceability_index"][0][
                "counterargument_ids"
            ] = []
            prefixed_but_unknown["traceability_index"][0]["challenge_ids"] = [
                "challenge_not_in_saved_counterargument"
            ]
            final = FinalIntegratedAnalysis.model_validate(prefixed_but_unknown)
            with self.assertRaisesRegex(ValueError, "unknown challenges"):
                manager._validate_final_counterargument_dispositions(
                    counterargument,
                    final,
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
            expected = {
                agent_id: payload["analysis_id"]
                for agent_id, payload in state.analysis_results.items()
            }
            self.assertEqual(
                state.final_integration["stakeholder_structure"][
                    "source_analysis_id"
                ],
                expected["deliberation.stakeholder_response_analyst"],
            )
            self.assertEqual(
                state.final_integration["causal_structure"]["source_analysis_id"],
                expected["deliberation.causal_structural_analyst"],
            )
            final_interpretation_ids = {
                identifier
                for entry in state.final_integration["traceability_index"]
                for identifier in entry["causal_item_ids"]
                if identifier.startswith("alt_interp_")
            }
            self.assertTrue(final_interpretation_ids)
            self.assertEqual(
                final_interpretation_ids,
                {
                    identifier
                    for entry in state.deliberation_result["claim_traceability"]
                    for identifier in entry["causal_item_ids"]
                    if identifier.startswith("alt_interp_")
                },
            )

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

    def test_resume_expands_revision_targets_when_upstream_evidence_set_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                deliberation_review_decisions=["mixed_real_case", "approved"],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider)
            waiting = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            revised_data = make_report(waiting.workflow_id).model_dump(mode="json")
            replacements = {}
            for index, source in enumerate(revised_data["sources"]):
                replacements[source["source_id"]] = f"source_revision_{index}"
                replacements[source["evidence_id"]] = f"evidence_revision_{index}"

            def replace_ids(value):
                if isinstance(value, dict):
                    return {key: replace_ids(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [replace_ids(item) for item in value]
                return replacements.get(value, value) if isinstance(value, str) else value

            revised_report = type(make_report()).model_validate(replace_ids(revised_data))
            revised = make_deliberation_handoff(
                revised_report,
                message_type=MessageType.RESEARCH_REVISION_RESULT,
            )
            manager.repository.write_json_atomic(
                manager.repository.researcher_outbox_dir
                / f"{waiting.workflow_id}.json",
                revised.model_dump(mode="json"),
            )

            completed = asyncio.run(manager.resume(waiting.workflow_id))

            self.assertEqual(completed.status, "COMPLETED")
            for agent_id in (
                "deliberation.argument_analyst",
                "deliberation.causal_structural_analyst",
                "deliberation.stakeholder_response_analyst",
            ):
                self.assertEqual(provider.agent_calls.count(agent_id), 2, agent_id)
                self.assertIn(
                    agent_id,
                    completed.revision_history[-1].target_agent_ids,
                )

    def test_recover_uses_one_contract_repair_task_for_saved_invalid_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _InvalidStakeholderOnceProvider(
                deliberation_review_decisions=["mixed_real_case", "approved"],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider)
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
            provider.invalidate_next_stakeholder = True
            failed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.revision_count, 1)
            calls_before_recovery = provider.agent_calls.count(
                "deliberation.stakeholder_response_analyst"
            )

            recovered = asyncio.run(manager.recover(waiting.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(recovered.revision_count, 1)
            self.assertEqual(
                provider.agent_calls.count("deliberation.stakeholder_response_analyst"),
                calls_before_recovery + 1,
            )
            repair_tasks = [
                item
                for item in recovered.analysis_tasks
                if (item.get("revision_context") or {}).get("contract_repair")
            ]
            self.assertEqual(len(repair_tasks), 1)
            repair_context = repair_tasks[0]["revision_context"]
            self.assertTrue(repair_context["validation_failures"])
            self.assertTrue(repair_context["repair_requirements"])
            reservation = (
                root
                / "provider_call_reservations"
                / "mock"
                / waiting.workflow_id
                / f"{repair_tasks[0]['task_id']}.json"
            )
            self.assertTrue(reservation.exists())
            calls_after_completion = len(provider.calls)
            self.assertEqual(
                asyncio.run(manager.recover(waiting.workflow_id)).status,
                "COMPLETED",
            )
            self.assertEqual(len(provider.calls), calls_after_completion)

    def test_saved_stakeholder_error_rebinds_sources_from_report_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            task = DeliberationAnalysisTask.model_validate(
                next(
                    raw
                    for raw in state.analysis_tasks
                    if raw["target_agent_id"]
                    == "deliberation.stakeholder_response_analyst"
                )
            )
            request = next(
                message
                for message in state.message_history
                if message.receiver_agent_id == task.target_agent_id
                and message.payload.get("task_id") == task.task_id
            )
            payload = json.loads(
                json.dumps(state.analysis_results[task.target_agent_id])
            )
            payload["specific_facts"] = [
                {
                    "fact_id": "specific_saved",
                    "statement": "A ministry reported 25 percent",
                    "verification_status": "verified",
                    "evidence_ids": ["evidence_0"],
                    "source_ids": ["source_provider_alias"],
                    "research_gap": "",
                }
            ]
            self.assertIn(
                "outside its ResearchReport",
                manager._validate_analysis_source_bindings(state, payload),
            )
            state.message_history.append(
                PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=request.message_id,
                    sender_agent_id=task.target_agent_id,
                    receiver_agent_id=manager.agent_id,
                    message_type=MessageType.ERROR,
                    objective="Persist invalid provider payload",
                    payload={
                        "message": "legacy local schema rejection",
                        "invalid_payload": payload,
                    },
                )
            )
            calls_before = len(provider.calls)

            recovered = manager._recover_saved_invalid_analysis(state, task)

            self.assertIsNotNone(recovered)
            self.assertEqual(
                recovered["specific_facts"][0]["source_ids"], ["source_0"]
            )
            self.assertEqual(len(provider.calls), calls_before)
            self.assertIn(
                "source_ids were rebound",
                state.message_history[-1].metadata.notes,
            )
            self.assertTrue(manager._saved_analysis_is_valid(state, task, recovered))

    def test_saved_stakeholder_result_rebinds_sources_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            task = DeliberationAnalysisTask.model_validate(
                next(
                    raw
                    for raw in state.analysis_tasks
                    if raw["target_agent_id"]
                    == "deliberation.stakeholder_response_analyst"
                )
            )
            request = next(
                message
                for message in state.message_history
                if message.receiver_agent_id == task.target_agent_id
                and message.payload.get("task_id") == task.task_id
            )
            response = next(
                message
                for message in state.message_history
                if message.parent_message_id == request.message_id
                and message.message_type
                == MessageType.DELIBERATION_TASK_RESULT.value
            )
            payload = json.loads(json.dumps(response.payload))
            payload["specific_facts"] = [
                {
                    "fact_id": "specific_saved_result",
                    "statement": "A ministry reported 25 percent",
                    "verification_status": "verified",
                    "evidence_ids": ["evidence_0"],
                    "source_ids": ["source_provider_alias"],
                    "research_gap": "",
                }
            ]
            response.payload = payload
            state.analysis_results.pop(task.target_agent_id)
            calls_before = len(provider.calls)

            recovered = manager._recover_saved_invalid_analysis(state, task)

            self.assertIsNotNone(recovered)
            self.assertEqual(
                recovered["specific_facts"][0]["source_ids"], ["source_0"]
            )
            self.assertEqual(len(provider.calls), calls_before)
            self.assertIn(
                "source_ids were rebound",
                state.message_history[-1].metadata.notes,
            )
            recovered_again = manager._recover_saved_invalid_analysis(state, task)
            self.assertEqual(recovered_again, recovered)
            self.assertEqual(len(provider.calls), calls_before)

    def test_manager_validation_payload_is_persisted_and_gets_one_contract_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _InvalidInitialIntegrationOnceProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)

            failed = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )

            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(provider.calls.count("InitialIntegratedAnalysis"), 1)
            saved = next(iter(failed.manager_invalid_payloads.values()))
            self.assertEqual(saved["stage"], "initial_integration")
            self.assertEqual(
                saved["invalid_payload"]["traceability_index"][0][
                    "causal_item_ids"
                ],
                ["cc_1"],
            )

            recovered = asyncio.run(manager.recover(failed.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(provider.calls.count("InitialIntegratedAnalysis"), 2)
            repair_id = (
                "deliberation_manager_initial_integration_revision_0_"
                "recovery_1_contract_repair_1"
            )
            self.assertTrue(
                (
                    root
                    / "provider_call_reservations"
                    / "mock"
                    / failed.workflow_id
                    / f"{repair_id}.json"
                ).exists()
            )

    def test_wrong_valid_role_provenance_fails_closed_then_uses_distinct_repair_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _WrongStakeholderProvenanceOnceProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)

            failed = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )

            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(provider.calls.count("InitialIntegratedAnalysis"), 1)
            saved = next(iter(failed.manager_invalid_payloads.values()))
            self.assertEqual(
                saved["validation_errors"][0]["loc"],
                ("stakeholder_structure", "source_analysis_id"),
            )
            calls_before = len(provider.calls)

            recovered = asyncio.run(manager.recover(failed.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(
                provider.calls[calls_before:][0],
                "InitialIntegratedAnalysis",
            )
            self.assertEqual(provider.calls.count("InitialIntegratedAnalysis"), 2)
            self.assertEqual(
                recovered.initial_integration["stakeholder_structure"][
                    "source_analysis_id"
                ],
                recovered.analysis_results[
                    "deliberation.stakeholder_response_analyst"
                ]["analysis_id"],
            )
            repair_id = (
                "deliberation_manager_initial_integration_revision_0_"
                "recovery_1_contract_repair_1"
            )
            self.assertTrue(
                (
                    root
                    / "provider_call_reservations"
                    / "mock"
                    / recovered.workflow_id
                    / f"{repair_id}.json"
                ).exists()
            )

    def test_saved_manager_payload_reuses_legacy_causal_map_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            causal = json.loads(
                json.dumps(
                    state.analysis_results[
                        "deliberation.causal_structural_analyst"
                    ]
                )
            )
            replacements = {
                "causal_claims": "cc_1",
                "mechanisms": "mech_1",
                "structural_factors": "sf_1",
                "alternative_explanations": "alt_1",
            }
            old_by_new = {}
            for field_name, old_id in replacements.items():
                new_id = causal[field_name][0]["item_id"]
                old_by_new[new_id] = old_id
                causal[field_name][0]["item_id"] = old_id
            for mapping in causal["evidence_mappings"]:
                mapping["mapped_item_ids"] = [
                    old_by_new.get(item, item)
                    for item in mapping.get("mapped_item_ids", [])
                ]
            state.analysis_results[
                "deliberation.causal_structural_analyst"
            ] = causal
            raw_initial = json.loads(json.dumps(state.initial_integration))
            raw_initial["traceability_index"][0]["causal_item_ids"] = list(
                replacements.values()
            )
            raw_initial["traceability_index"][0]["integration_change_ids"] = [
                "ichg_legacy_dangling"
            ]
            with self.assertRaises(ValueError):
                InitialIntegratedAnalysis.model_validate(raw_initial)
            logical_task_id = "deliberation_manager_initial_integration_revision_0_recovery_1"
            state.manager_invalid_payloads[logical_task_id] = {
                "stage": "initial_integration",
                "output_schema": "InitialIntegratedAnalysis",
                "invalid_payload": raw_initial,
                "validation_errors": [],
                "recorded_at": "2026-08-17T00:00:00+00:00",
            }
            calls_before = len(provider.calls)

            recovered = manager._recover_saved_manager_payload(
                state,
                output_schema=InitialIntegratedAnalysis,
                stage="initial_integration",
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(len(provider.calls), calls_before)
            self.assertEqual(
                recovered.traceability_index[0].integration_change_ids,
                [],
            )
            self.assertEqual(
                state.manager_payload_recoveries[-1][
                    "removed_dangling_integration_change_ids"
                ],
                ["ichg_legacy_dangling"],
            )
            self.assertTrue(
                all(
                    item.startswith(
                        (
                            "causal_",
                            "mechanism_",
                            "structural_",
                            "alternative_",
                        )
                    )
                    for item in recovered.traceability_index[0].causal_item_ids
                )
            )
            self.assertEqual(
                state.analysis_results[
                    "deliberation.causal_structural_analyst"
                ]["causal_claims"][0]["item_id"],
                "cc_1",
            )

    def test_saved_complete_final_payload_rebinds_placeholder_ids_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            raw = json.loads(json.dumps(state.final_integration))
            generated_interpretation_id = state.counterargument_analysis[
                "alternative_interpretations"
            ][0]["interpretation_id"]
            production_interpretation_id = (
                "alt_interp_task_fragmentation_and_busyness"
            )
            state.counterargument_analysis["alternative_interpretations"][0][
                "interpretation_id"
            ] = production_interpretation_id
            raw = manager._replace_exact_identifiers(
                raw,
                {generated_interpretation_id: production_interpretation_id},
            )
            raw["integration_id"] = "integration_final_"
            raw["previous_integration_id"] = "integration_initial_"

            actual_analysis_ids = {
                payload["analysis_id"]: alias
                for agent_id, payload in state.analysis_results.items()
                for alias in (
                    "arg_001"
                    if agent_id == "deliberation.argument_analyst"
                    else "causal_001"
                    if agent_id == "deliberation.causal_structural_analyst"
                    else payload["analysis_id"],
                )
            }
            actual_analysis_ids[state.counterargument_analysis["analysis_id"]] = (
                "counter_001"
            )
            raw = manager._replace_exact_identifiers(raw, actual_analysis_ids)

            def corrupt_evidence(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if (
                            isinstance(item, list)
                            and (key.endswith("evidence_ids") or key == "evidence_linked")
                            and item
                        ):
                            value[key] = ["ev_001"]
                        else:
                            corrupt_evidence(item)
                elif isinstance(value, list):
                    for item in value:
                        corrupt_evidence(item)

            corrupt_evidence(raw)
            for index, viewpoint in enumerate(raw["major_viewpoints"], start=1):
                viewpoint["viewpoint_id"] = f"view_{index}"
            for change in raw["integration_changes"]:
                change["change_id"] = "change_"
                change["source_counterargument_ids"] = ["counterargument_"]
            for disposition in raw["counterargument_dispositions"]:
                disposition["counterargument_id"] = "counterargument_"
                disposition["integration_change_ids"] = [
                    "change_" for _ in disposition["integration_change_ids"]
                ]
            asymmetry = raw["causal_structure"].get(
                "evidence_strength_asymmetry"
            )
            if isinstance(asymmetry, dict):
                asymmetry["source_counterargument_ids"] = ["counterargument_"]
            raw["traceability_index"] = [
                {
                    "schema_version": "1.0.0",
                    "claim_ids": [],
                    "viewpoint_ids": [],
                    "causal_item_ids": [],
                    "integration_change_ids": ["change_"],
                    "evidence_ids": ["ev_001"],
                    "source_ids": ["src_001"],
                    "analysis_ids": ["arg_001", "causal_001", "counter_001"],
                    "counterargument_ids": ["counterargument_"],
                    "challenge_ids": ["steelman_"],
                    "integration_ids": [
                        "integration_initial_",
                        "integration_final_",
                    ],
                    "task_ids": ["task_001"],
                }
            ]
            logical_task_id = (
                "deliberation_manager_final_integration_revision_0_"
                "recovery_1_contract_repair_1"
            )
            state.manager_invalid_payloads[logical_task_id] = {
                "stage": "final_integration",
                "output_schema": "FinalIntegratedAnalysis",
                "invalid_payload": raw,
                "validation_errors": [],
                "recorded_at": "2026-08-18T00:00:00+00:00",
            }
            calls_before = len(provider.calls)

            recovered = manager._recover_saved_manager_payload(
                state,
                output_schema=FinalIntegratedAnalysis,
                stage="final_integration",
            )

            self.assertIsNotNone(recovered)
            self.assertEqual(len(provider.calls), calls_before)
            self.assertTrue(recovered.integration_id.startswith("integration_final_"))
            self.assertNotEqual(recovered.integration_id, "integration_final_")
            self.assertEqual(
                recovered.previous_integration_id,
                state.initial_integration["integration_id"],
            )
            self.assertEqual(
                {item.counterargument_id for item in recovered.counterargument_dispositions},
                {
                    item["counterargument_id"]
                    for item in state.counterargument_analysis["counterarguments"]
                },
            )
            known_evidence_ids = {
                item["evidence_id"] for item in state.research_report["evidence_items"]
            }
            self.assertTrue(
                set(recovered.traceability_index[0].evidence_ids)
                <= known_evidence_ids
            )
            self.assertEqual(
                state.manager_payload_recoveries[-1]["provider_call_count"],
                0,
            )
            self.assertIn(
                production_interpretation_id,
                recovered.traceability_index[0].causal_item_ids,
            )
            self.assertEqual(
                state.manager_payload_recoveries[-1][
                    "preserved_counterargument_interpretation_ids"
                ],
                [production_interpretation_id],
            )
            self.assertIn(
                "saved_final_identifier_rebinding",
                state.manager_payload_recoveries[-1]["compatibility_adapter"],
            )

    def test_saved_final_compound_revised_resolution_recovers_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            raw = json.loads(json.dumps(state.final_integration))
            target = raw["counterargument_dispositions"][0]
            target["resolution"] = "revised_with_research_gap_retained"
            target["research_gap_required"] = True
            target["remaining_uncertainty"] = "Explicit retained research gap"
            logical_task_id = "deliberation_manager_final_integration_revision_0"
            state.final_integration = None
            state.manager_invalid_payloads[logical_task_id] = {
                "stage": "final_integration",
                "output_schema": "FinalIntegratedAnalysis",
                "invalid_payload": raw,
                "validation_errors": [
                    {
                        "loc": ["counterargument_dispositions", 0, "resolution"],
                        "input": "revised_with_research_gap_retained",
                    }
                ],
                "recorded_at": "2026-08-24T00:00:00+00:00",
            }
            calls_before = len(provider.calls)

            recovered = manager._recover_saved_manager_payload(
                state,
                output_schema=FinalIntegratedAnalysis,
                stage="final_integration",
            )

            self.assertIsNotNone(recovered)
            self.assertEqual(len(provider.calls), calls_before)
            disposition = recovered.counterargument_dispositions[0]
            self.assertEqual(disposition.resolution, "revised")
            self.assertTrue(disposition.research_gap_required)
            self.assertEqual(
                disposition.remaining_uncertainty,
                "Explicit retained research gap",
            )
            self.assertEqual(
                state.manager_payload_recoveries[-1][
                    "normalized_compound_resolution_tokens"
                ][disposition.counterargument_id],
                "revised_with_research_gap_retained",
            )

    def test_saved_manager_payload_is_not_replayed_across_revisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            state.revision_count = 1
            state.manager_invalid_payloads[
                "deliberation_manager_final_integration_revision_0_recovery_1"
            ] = {
                "stage": "final_integration",
                "output_schema": "FinalIntegratedAnalysis",
                "invalid_payload": json.loads(json.dumps(state.final_integration)),
                "validation_errors": [],
                "recorded_at": "2026-08-18T00:00:00+00:00",
            }
            calls_before = len(provider.calls)

            recovered = manager._recover_saved_manager_payload(
                state,
                output_schema=FinalIntegratedAnalysis,
                stage="final_integration",
            )

            self.assertIsNone(recovered)
            self.assertEqual(len(provider.calls), calls_before)

    def test_cross_revision_replay_rolls_back_only_unexecuted_limit_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                deliberation_review_decisions=[
                    "revision_required",
                    "revision_required",
                ],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider, demo_safe_mode=False)
            blocked = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            self.assertEqual(blocked.status, "BLOCKED")
            self.assertEqual(blocked.revision_count, 2)
            self.assertEqual(blocked.checkpoint_revisions["final_integration"], 1)
            blocked.manager_payload_recoveries.append(
                {
                    "logical_task_id": (
                        "deliberation_manager_final_integration_revision_0_"
                        "recovery_1_contract_repair_1"
                    ),
                    "stage": "final_integration",
                    "output_schema": "FinalIntegratedAnalysis",
                    "compatibility_adapter": "saved_final_identifier_rebinding",
                }
            )
            calls_before = len(provider.calls)

            repaired = manager._repair_unexecuted_revision_after_cross_revision_replay(
                blocked
            )

            self.assertTrue(repaired)
            self.assertEqual(blocked.revision_count, 1)
            self.assertEqual([item.iteration for item in blocked.revision_history], [1])
            self.assertIsNone(blocked.final_integration)
            self.assertIsNotNone(blocked.initial_integration)
            self.assertIsNotNone(blocked.counterargument_analysis)
            self.assertNotIn("final_integration", blocked.checkpoint_revisions)
            self.assertEqual(len(provider.calls), calls_before)
            self.assertEqual(
                blocked.manager_payload_recoveries[-1]["provider_call_count"],
                0,
            )

    def test_final_integration_rejects_undefined_change_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            payload = json.loads(json.dumps(state.final_integration))
            payload["traceability_index"][0]["integration_change_ids"] = [
                "change_not_defined"
            ]
            with self.assertRaisesRegex(ValueError, "undefined integration change"):
                FinalIntegratedAnalysis.model_validate(payload)

    def test_recover_reuses_persisted_counterargument_result_by_deterministic_task_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            counter_calls = provider.calls.count("CounterargumentAnalysisResult")
            request = next(
                message
                for message in state.message_history
                if message.receiver_agent_id == "deliberation.counterargument_analyst"
                and message.message_type
                == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            )
            self.assertEqual(request.payload["task_id"], "counter_task_revision_0")
            manager._clear_recovery_checkpoints(state, "counterargument")
            state.status = "FAILED"
            state.error = {"message": "fault injected after counterargument persistence"}
            manager.repository.save(state)

            recovered = asyncio.run(manager.recover(state.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(
                provider.calls.count("CounterargumentAnalysisResult"),
                counter_calls,
            )

    def test_saved_invalid_counterargument_payload_is_repaired_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            original_request = next(
                message
                for message in state.message_history
                if message.receiver_agent_id == "deliberation.counterargument_analyst"
                and message.message_type
                == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            )
            repair_task_id = "counter_task_revision_0_context_repair_1"
            task_payload = json.loads(json.dumps(original_request.payload))
            task_payload["task_id"] = repair_task_id
            request = PMPMessage.create(
                workflow_id=state.workflow_id,
                sender_agent_id="deliberation.manager",
                receiver_agent_id="deliberation.counterargument_analyst",
                message_type=MessageType.DELIBERATION_TASK_ASSIGNMENT,
                objective="recover saved counterargument raw",
                payload=task_payload,
            )
            raw = json.loads(json.dumps(state.counterargument_analysis))
            raw["task_id"] = repair_task_id
            raw["analysis_id"] = "counteranalysis_saved_raw"
            raw["counterarguments"][0]["revision_target_agent_ids"] = [
                "deliberation.researcher",
                "deliberation.manager",
            ]
            nonrevision = json.loads(json.dumps(raw["counterarguments"][0]))
            nonrevision.update(
                {
                    "counterargument_id": "counter_nonrevision_saved",
                    "required_revision": False,
                    "revision_target_agent_ids": ["deliberation.researcher"],
                }
            )
            raw["counterarguments"].append(nonrevision)
            response = PMPMessage.create(
                workflow_id=state.workflow_id,
                parent_message_id=request.message_id,
                sender_agent_id="deliberation.counterargument_analyst",
                receiver_agent_id="deliberation.manager",
                message_type=MessageType.ERROR,
                objective="saved validation failure",
                payload={
                    "task_id": repair_task_id,
                    "error_code": "PayloadValidationError",
                    "invalid_payload": raw,
                },
            )
            state.message_history.extend([request, response])
            calls_before = len(provider.calls)

            recovered = manager._recover_saved_counterargument_result(
                state,
                CounterargumentTask.model_validate(task_payload),
                InitialIntegratedAnalysis.model_validate(state.initial_integration),
            )

            self.assertIsNotNone(recovered)
            self.assertEqual(len(provider.calls), calls_before)
            self.assertEqual(
                recovered.analysis_id,
                "counterargument_analysis_saved_raw",
            )
            self.assertEqual(
                recovered.counterarguments[-1].revision_target_agent_ids,
                [],
            )
            self.assertEqual(
                state.counterargument_payload_recoveries[-1]["source_message_id"],
                response.message_id,
            )

    def test_recover_does_not_redispatch_ambiguous_revision_provider_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                deliberation_review_decisions=["mixed_real_case"],
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider)
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
            provider.fail_agent_ids.add("deliberation.stakeholder_response_analyst")
            failed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(failed.status, "FAILED")
            calls_before_recovery = len(provider.calls)
            provider.fail_agent_ids.clear()

            blocked = asyncio.run(manager.recover(waiting.workflow_id))

            self.assertEqual(blocked.status, "FAILED")
            self.assertIn("explicit provider retry authorization", blocked.error["message"])
            self.assertEqual(len(provider.calls), calls_before_recovery)

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
        for task in tasks:
            self.assertEqual(
                {item.evidence_id for item in task.evidence_context},
                set(task.target_evidence_ids),
            )
            self.assertTrue(all(item.source_id for item in task.evidence_context))

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
            self.assertLess(len(routing_trace), len(state.message_history) + 1)

    def test_provider_views_remove_duplicate_research_tables_and_final_primary_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = _CaptureInputsProvider(
                reservation_root=Path(temporary) / "provider_call_reservations"
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            by_schema = {
                schema: payload for schema, payload in provider.input_payloads
            }
            for schema in (
                "InitialIntegratedAnalysis",
                "CounterargumentAnalysisResult",
                "FinalIntegratedAnalysis",
                "DeliberationQualityReviewOutput",
            ):
                compact_report = by_schema[schema]["research_report"]
                self.assertIn("evidence_items", compact_report)
                self.assertNotIn("sources", compact_report)
                self.assertNotIn("source_metadata", compact_report)
                self.assertNotIn("evidence_quality_assessments", compact_report)
            counter_payload = by_schema["CounterargumentAnalysisResult"]
            self.assertNotIn("agreements", counter_payload)
            self.assertNotIn("conflicts", counter_payload)
            self.assertNotIn("unresolved_items", counter_payload)
            final_payload = by_schema["FinalIntegratedAnalysis"]
            self.assertNotIn("primary_analyses", final_payload)
            self.assertEqual(
                set(final_payload["primary_analysis_ids"]),
                set(state.analysis_results),
            )

    def test_context_budget_fault_stops_before_reservation_and_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            reservation_root = Path(temporary) / "provider_call_reservations"
            provider = _BudgetRejectingProvider(reservation_root=reservation_root)
            manager = make_manager(Path(temporary), provider)

            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))

            self.assertEqual(state.status, "FAILED")
            self.assertEqual(provider.calls, [])
            self.assertEqual(list(reservation_root.rglob("*.json")), [])

    def test_counterargument_context_failure_uses_one_deterministic_repair_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            state.revision_count = 1
            state.message_history.append(
                PMPMessage.create(
                    workflow_id=state.workflow_id,
                    sender_agent_id="deliberation.counterargument_analyst",
                    receiver_agent_id="deliberation.manager",
                    message_type=MessageType.ERROR,
                    objective="context failure",
                    payload={
                        "task_id": "counter_task_revision_1",
                        "message": "maximum context length is 64000; reduce the length",
                    },
                )
            )
            repair = "counter_task_revision_1_context_repair_1"
            self.assertEqual(manager._counterargument_task_id(state), repair)
            state.message_history.append(
                PMPMessage.create(
                    workflow_id=state.workflow_id,
                    sender_agent_id="deliberation.manager",
                    receiver_agent_id="deliberation.counterargument_analyst",
                    message_type=MessageType.DELIBERATION_TASK_ASSIGNMENT,
                    objective="one context repair",
                    payload={"task_id": repair},
                )
            )
            self.assertEqual(manager._counterargument_task_id(state), repair)

    def test_ambiguous_manager_transport_requires_one_explicit_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _AmbiguousFinalOnceProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider, demo_safe_mode=False)

            failed = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )

            self.assertEqual(failed.status, "FAILED")
            self.assertIsNone(failed.final_integration)
            self.assertEqual(provider.calls.count("FinalIntegratedAnalysis"), 1)
            self.assertEqual(len(failed.manager_provider_failures), 1)
            failure = failed.manager_provider_failures[0]
            self.assertEqual(failure["root_exception_type"], "IncompleteReadForTest")
            self.assertFalse(failure["automatic_retry_allowed"])

            calls_before_blocked_recovery = list(provider.calls)
            blocked = asyncio.run(manager.recover(failed.workflow_id))
            self.assertEqual(blocked.status, "FAILED")
            self.assertIn("explicit provider retry authorization", blocked.error["message"])
            self.assertEqual(provider.calls, calls_before_blocked_recovery)

            safe_manager = make_manager(root, provider, demo_safe_mode=True)
            recovered = asyncio.run(
                safe_manager.retry_provider_call(failed.workflow_id)
            )

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(provider.calls.count("FinalIntegratedAnalysis"), 2)
            self.assertEqual(
                provider.calls[calls_before_blocked_recovery.__len__():],
                ["FinalIntegratedAnalysis", "DeliberationQualityReviewOutput"],
            )
            authorization = safe_manager.provider_retry_store.for_original_task(
                workflow_id=failed.workflow_id,
                provider_id="mock",
                original_task_id=failure["logical_task_id"],
            )
            self.assertIsNotNone(authorization)
            self.assertEqual(authorization.status, "CONSUMED")
            self.assertTrue(Path(authorization.reservation_path).is_file())

    def test_ambiguous_quality_review_transport_retries_only_reviewer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _AmbiguousQualityOnceProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider, demo_safe_mode=False)
            failed = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )

            self.assertEqual(failed.status, "FAILED")
            self.assertIsNotNone(failed.final_integration)
            self.assertIsNotNone(failed.deterministic_validation)
            self.assertIsNone(failed.review_result)
            self.assertEqual(
                provider.calls.count("DeliberationQualityReviewOutput"),
                1,
            )
            calls_before_retry = len(provider.calls)

            safe_manager = make_manager(root, provider, demo_safe_mode=True)
            recovered = asyncio.run(
                safe_manager.retry_provider_call(failed.workflow_id)
            )

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(
                provider.calls[calls_before_retry:],
                ["DeliberationQualityReviewOutput"],
            )
            self.assertEqual(
                provider.calls.count("FinalIntegratedAnalysis"),
                1,
            )

    def test_operator_retry_cannot_retry_the_retry_after_second_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _AmbiguousFinalOnceProvider(
                interrupt_final_attempts=2,
                reservation_root=root / "provider_call_reservations",
            )
            manager = make_manager(root, provider, demo_safe_mode=False)
            failed = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            safe_manager = make_manager(root, provider, demo_safe_mode=True)

            retry_failed = asyncio.run(
                safe_manager.retry_provider_call(failed.workflow_id)
            )

            self.assertEqual(retry_failed.status, "FAILED")
            self.assertEqual(provider.calls.count("FinalIntegratedAnalysis"), 2)
            calls_before_rejected_retry = list(provider.calls)
            with self.assertRaisesRegex(ValueError, "cannot authorize another retry"):
                asyncio.run(safe_manager.retry_provider_call(failed.workflow_id))
            self.assertEqual(provider.calls, calls_before_rejected_retry)

    def test_saved_quality_review_response_is_reused_after_state_save_fault(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider, demo_safe_mode=False)
            completed = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            completed.status = "FAILED"
            completed.review_result = None
            completed.checkpoint_revisions.pop("quality_review", None)
            completed.deliberation_result = None
            completed.conclusion_sent = False
            completed.completed_at = None
            completed.error = {"message": "fault after review response persistence"}
            manager.repository.save(completed)
            calls_before_recovery = list(provider.calls)

            recovered = asyncio.run(manager.recover(completed.workflow_id))

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(provider.calls, calls_before_recovery)

    def test_changed_review_artifact_gets_content_addressed_task_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider, demo_safe_mode=False)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            report = ResearchReport.model_validate(state.research_report)
            primary = manager._normalized_primary_analyses(state)
            initial = InitialIntegratedAnalysis.model_validate(
                state.initial_integration
            )
            counterargument = CounterargumentAnalysisResult.model_validate(
                state.counterargument_analysis
            )
            final_data = json.loads(json.dumps(state.final_integration))
            previous_final_id = final_data["integration_id"]
            final_data["integration_id"] = "integration_final_changed_artifact"
            for entry in final_data.get("traceability_index", []):
                entry["integration_ids"] = [
                    final_data["integration_id"]
                    if item == previous_final_id
                    else item
                    for item in entry.get("integration_ids", [])
                ]
            final = FinalIntegratedAnalysis.model_validate(final_data)
            validation = DeterministicValidationResult.model_validate(
                state.deterministic_validation
            )
            calls_before = len(provider.calls)

            _review, response = asyncio.run(
                manager._request_review(
                    state,
                    report,
                    initial,
                    counterargument,
                    final,
                    validation,
                    primary,
                    recovery=False,
                    reuse_saved_response=True,
                )
            )

            self.assertEqual(len(provider.calls), calls_before + 1)
            request = next(
                message
                for message in state.message_history
                if message.message_id == response.parent_message_id
            )
            self.assertRegex(
                request.payload["task_id"],
                r"^delib_review_task_revision_0_artifact_[0-9a-f]{16}$",
            )

    def test_manager_contract_repair_accepts_distinct_failed_retry_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = MockModelProvider(
                reservation_root=root / "provider_call_reservations"
            )
            manager = make_manager(root, provider, demo_safe_mode=True)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            original_task_id = "deliberation_manager_final_integration_revision_0"
            state.status = "FAILED"
            state.final_integration = None
            state.deterministic_validation = None
            state.review_result = None
            state.deliberation_result = None
            state.completed_at = None
            state.conclusion_sent = False
            for checkpoint in (
                "final_integration",
                "deterministic_validation",
                "quality_review",
            ):
                state.checkpoint_revisions.pop(checkpoint, None)
            state.manager_provider_failures.append(
                {
                    "failure_id": "manager_provider_failure_contract_test",
                    "logical_task_id": original_task_id,
                    "stage": "final_integration",
                    "error_class": "ProviderResponseContractError",
                    "error_message": "strict JSON contract failed",
                    "root_exception_type": "JSONDecodeError",
                    "retry_count": 0,
                    "automatic_retry_allowed": False,
                    "provider": "mock",
                    "model_id": "mock",
                    "recorded_at": "2026-08-18T00:00:00+00:00",
                    "compatibility_source": None,
                }
            )
            retry = manager.provider_retry_store.authorize_once(
                workflow_id=state.workflow_id,
                provider_id="mock",
                agent_id=manager.agent_id,
                original_task_id=original_task_id,
                source_error_message_id="manager_provider_failure_contract_test",
                source_error_class="ProviderResponseContractError",
            )
            retry_path = manager.provider_retry_store.reservation_path(
                provider_id="mock",
                workflow_id=state.workflow_id,
                task_id=retry.retry_task_id,
            )
            manager.repository.write_json_atomic(
                retry_path,
                {
                    "workflow_id": state.workflow_id,
                    "task_id": retry.retry_task_id,
                    "agent_id": manager.agent_id,
                    "stage": "final_integration",
                    "provider": "MockModelProvider",
                    "model_id": "vendor/mock-second",
                },
            )
            manager.provider_retry_store.consume(
                retry,
                reservation_path=retry_path,
            )
            state.error = {
                "message": "OpenRouter HTTP 400: Request contains an invalid argument"
            }
            manager.repository.save(state)
            calls_before = len(provider.calls)

            repaired = asyncio.run(
                manager.repair_provider_contract(
                    state.workflow_id,
                    repair_model_id="vendor/mock-third",
                )
            )

            self.assertEqual(repaired.status, "COMPLETED")
            self.assertEqual(
                provider.calls[calls_before:],
                ["FinalIntegratedAnalysis", "DeliberationQualityReviewOutput"],
            )
            authorization = (
                manager.provider_contract_repair_store.for_original_task(
                    workflow_id=state.workflow_id,
                    provider_id="mock",
                    original_task_id=original_task_id,
                )
            )
            self.assertEqual(authorization.status, "CONSUMED")
            self.assertEqual(
                authorization.retry_failed_model_id,
                "vendor/mock-second",
            )
            repair_reservation = json.loads(
                Path(authorization.reservation_path).read_text(encoding="utf-8")
            )
            self.assertEqual(repair_reservation["model_id"], "vendor/mock-third")

    def test_legacy_incomplete_read_state_uses_compatibility_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reservation_root = root / "provider_call_reservations"
            provider = MockModelProvider(reservation_root=reservation_root)
            manager = make_manager(root, provider, demo_safe_mode=False)
            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            state.status = "FAILED"
            state.final_integration = None
            state.deterministic_validation = None
            state.review_result = None
            state.deliberation_result = None
            state.conclusion_sent = False
            state.completed_at = None
            state.checkpoint_revisions.pop("final_integration", None)
            state.checkpoint_revisions.pop("deterministic_validation", None)
            state.checkpoint_revisions.pop("quality_review", None)
            state.error = {
                "message": (
                    "Deliberation統合またはQuality Reviewに失敗しました: "
                    "IncompleteRead(5962 bytes read)"
                )
            }
            legacy_task_id = (
                "deliberation_manager_final_integration_revision_0_recovery_1"
            )
            reservation = manager.provider_retry_store.reservation_path(
                provider_id="mock",
                workflow_id=state.workflow_id,
                task_id=legacy_task_id,
            )
            reservation.parent.mkdir(parents=True, exist_ok=True)
            reservation.write_text(
                json.dumps(
                    {
                        "workflow_id": state.workflow_id,
                        "task_id": legacy_task_id,
                        "agent_id": "deliberation.manager",
                        "stage": "final_integration",
                        "provider": "MockModelProvider",
                        "model_id": "mock",
                    }
                ),
                encoding="utf-8",
            )
            manager.repository.save(state)
            calls_before_retry = len(provider.calls)
            safe_manager = make_manager(root, provider, demo_safe_mode=True)

            recovered = asyncio.run(
                safe_manager.retry_provider_call(state.workflow_id)
            )

            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(
                provider.calls[calls_before_retry:],
                ["FinalIntegratedAnalysis", "DeliberationQualityReviewOutput"],
            )
            self.assertEqual(
                recovered.manager_provider_failures[-1]["compatibility_source"],
                "pre_cycle_012_state_error",
            )

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
