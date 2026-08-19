import asyncio
from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from common.models.errors import ProviderResponseContractError
from common.models.pmp import PMPMessage
from common.structured_outputs import strict_output_schema, strict_schema_violations
from conclusion.manager import ConclusionManager
from conclusion.schemas import (
    ConclusionPackage,
    ConclusionQualityReviewOutput,
    DecisionContext,
    DecisionEvaluationResult,
    DecisionIntegrationResult,
    PositionGenerationResult,
)
from conclusion.validator import ConclusionValidator
from conclusion.workflow import (
    DECISION_EVALUATOR_ID,
    DECISION_INTEGRATOR_ID,
    POSITION_GENERATOR_ID,
    QUALITY_REVIEWER_ID,
)
from providers.mock_provider import MockModelProvider
from tests.conclusion_helpers import make_conclusion_handoff, make_conclusion_manager


class _FailOnceConclusionProvider(MockModelProvider):
    """Inject one billed-response contract failure at a selected stage."""

    def __init__(self, *, failed_schema: type, reservation_root: Path) -> None:
        super().__init__(reservation_root=reservation_root)
        self.failed_schema = failed_schema
        self.attempts: dict[str, int] = {}

    async def generate_structured(self, **kwargs) -> dict:
        schema = kwargs["output_schema"]
        name = schema.__name__
        self.attempts[name] = self.attempts.get(name, 0) + 1
        if schema is self.failed_schema and self.attempts[name] == 1:
            raise ProviderResponseContractError(
                "Injected non-finite Provider response",
                provider=self.provider_id,
                model_id=kwargs["model"],
                response_content_sha256="a" * 64,
                response_content_length=8,
                response_root_type="float",
                response_invalid_path="$",
            )
        return await super().generate_structured(**kwargs)


class _ContractInvalidPrimaryModelProvider(MockModelProvider):
    """Fail every Position call on one model and succeed on a repair model."""

    def __init__(
        self,
        *,
        failed_model: str,
        reservation_root: Path,
        conclusion_review_decisions: list[str] | None = None,
    ) -> None:
        super().__init__(
            reservation_root=reservation_root,
            conclusion_review_decisions=conclusion_review_decisions,
        )
        self.failed_model = failed_model
        self.failed_models = {failed_model}
        self.position_models: list[str] = []

    async def generate_structured(self, **kwargs) -> dict:
        if kwargs["output_schema"] is PositionGenerationResult:
            model = kwargs["model"]
            self.position_models.append(model)
            if model in self.failed_models:
                raise ProviderResponseContractError(
                    "Injected repeated strict-output contract violation",
                    provider=self.provider_id,
                    model_id=model,
                    response_content_sha256="b" * 64,
                    response_content_length=8224,
                    response_root_type="float",
                    response_invalid_path="$",
                )
        return await super().generate_structured(**kwargs)


class _RepeatedEvaluationProvider(MockModelProvider):
    """Inject lossless structured-output repetition at Decision Evaluation."""

    async def generate_structured(self, **kwargs) -> dict:
        payload = await super().generate_structured(**kwargs)
        if kwargs["output_schema"] is DecisionEvaluationResult:
            evaluations = deepcopy(payload["candidate_evaluations"])
            payload["candidate_evaluations"] = evaluations * 4
        return payload


class _InvalidEvaluationReferenceOnceProvider(MockModelProvider):
    """Inject one semantically invalid cross-reference after a valid Position."""

    def __init__(self, *, reservation_root: Path) -> None:
        super().__init__(reservation_root=reservation_root)
        self.evaluation_attempts = 0

    async def generate_structured(self, **kwargs) -> dict:
        payload = await super().generate_structured(**kwargs)
        if kwargs["output_schema"] is DecisionEvaluationResult:
            self.evaluation_attempts += 1
            if self.evaluation_attempts == 1:
                payload["conditional_advantages"][0][
                    "advantaged_candidate_ids"
                ] = ["position_a_or_position_b"]
        return payload


class _ContradictoryQualityDecisionOnceProvider(MockModelProvider):
    """Inject the observed approval/revision routing contradiction once."""

    def __init__(self, *, reservation_root: Path) -> None:
        super().__init__(reservation_root=reservation_root)
        self.quality_review_attempts = 0

    async def generate_structured(self, **kwargs) -> dict:
        payload = await super().generate_structured(**kwargs)
        if kwargs["output_schema"] is ConclusionQualityReviewOutput:
            self.quality_review_attempts += 1
            if self.quality_review_attempts == 1:
                finding_id = "qr_trace_gap_claim_task_reorg"
                payload.update(
                    {
                        "status": "approved_with_conditions",
                        "playwright_readiness": "ready_with_conditions",
                        "findings": [
                            {
                                "finding_id": finding_id,
                                "severity": "HIGH",
                                "category": "traceability",
                                "issue": "Conclusion traceability mapping is incomplete",
                                "required_action": "Add the missing mapping",
                                "affected_agent_ids": [
                                    "conclusion.manager",
                                    "conclusion.decision_integrator",
                                ],
                                "affected_candidate_ids": [],
                            }
                        ],
                        "revision_scope": "targeted",
                        "revision_targets": [
                            "conclusion.manager",
                            "conclusion.decision_integrator",
                        ],
                        "upstream_revision_requests": [
                            {
                                "revision_request_id": "upstream_trace_gap",
                                "affected_candidate_ids": [],
                                "affected_claim_ids": [
                                    "claim_task_reorganization_observed"
                                ],
                                "missing_analysis_description": (
                                    "Conclusion traceability mapping is incomplete"
                                ),
                                "required_analysis_types": [
                                    "traceability_mapping",
                                    "schema_validation",
                                ],
                                "acceptance_conditions": [
                                    "Claim mapping is present"
                                ],
                                "source_finding_ids": [finding_id],
                            }
                        ],
                        "limitations_to_disclose": [
                            "Traceability mapping requires a repair"
                        ],
                    }
                )
        return payload


class _FailSecondEvaluationProvider(MockModelProvider):
    """Fail the evaluator inside an explicit revision, then allow recovery."""

    def __init__(self, *, reservation_root: Path) -> None:
        super().__init__(
            conclusion_review_decisions=["revision_required", "approved"],
            reservation_root=reservation_root,
        )
        self.evaluation_attempts = 0

    async def generate_structured(self, **kwargs) -> dict:
        if kwargs["output_schema"] is DecisionEvaluationResult:
            self.evaluation_attempts += 1
            if self.evaluation_attempts == 2:
                raise ProviderResponseContractError(
                    "Injected revision evaluator contract failure",
                    provider=self.provider_id,
                    model_id=kwargs["model"],
                    response_content_sha256="c" * 64,
                    response_content_length=64,
                    response_root_type="object",
                    response_invalid_path="$.candidate_evaluations[0]",
                )
        return await super().generate_structured(**kwargs)


class _UnknownIntegrationReferencesOnceProvider(MockModelProvider):
    """Return schema-valid but canonically unknown Integration references once."""

    def __init__(self, *, reservation_root: Path) -> None:
        super().__init__(reservation_root=reservation_root)
        self.integration_attempts = 0

    async def generate_structured(self, **kwargs) -> dict:
        payload = await super().generate_structured(**kwargs)
        if kwargs["output_schema"] is DecisionIntegrationResult:
            self.integration_attempts += 1
            if self.integration_attempts == 1:
                payload["integrated_option"]["candidate_ids"][-1] = (
                    "rationale_for_integration_and_selection"
                )
                payload["major_tradeoffs"].append(
                    {
                        "tradeoff_id": "tradeoff_unknown_claim",
                        "description": "Injected traceability failure",
                        "related_claim_ids": ["claim_task_reorganization_observed"],
                        "evidence_ids": [],
                    }
                )
        return payload


class _UnknownIntegrationScalarReferenceProvider(MockModelProvider):
    """Return an unknown scalar reference that must never be auto-rewritten."""

    def __init__(self, *, reservation_root: Path) -> None:
        super().__init__(reservation_root=reservation_root)
        self.integration_attempts = 0

    async def generate_structured(self, **kwargs) -> dict:
        payload = await super().generate_structured(**kwargs)
        if kwargs["output_schema"] is DecisionIntegrationResult:
            self.integration_attempts += 1
            payload["recommended_options"][0]["candidate_id"] = "candidate_unknown"
        return payload


class _FailManagerRepairReviewOnceProvider(MockModelProvider):
    """Inject a billed-response fault only for the bounded Manager re-review."""

    def __init__(self, *, reservation_root: Path) -> None:
        super().__init__(reservation_root=reservation_root)
        self.fail_next_quality_review = False

    async def generate_structured(self, **kwargs) -> dict:
        payload = await super().generate_structured(**kwargs)
        if (
            kwargs["output_schema"] is ConclusionQualityReviewOutput
            and self.fail_next_quality_review
        ):
            self.fail_next_quality_review = False
            raise ProviderResponseContractError(
                "Injected Manager repair review contract failure",
                provider=self.provider_id,
                model_id=kwargs["model"],
                response_content_sha256="d" * 64,
                response_content_length=128,
                response_root_type="object",
                response_invalid_path="$.status",
            )
        return payload


class ConclusionManagerTests(unittest.TestCase):
    @staticmethod
    def _inject_legacy_manager_alternative_finding(manager, state) -> None:
        state.status = "BLOCKED"
        state.revision_count = 2
        state.conclusion_package["alternatives"] = []
        state.review_result = {
            "review_id": f"legacy_manager_review_{len(state.manager_repair_history)}",
            "status": "revision_required",
            "reason": "A viable non-primary candidate is absent from alternatives",
            "playwright_readiness": "not_ready",
            "findings": [
                {
                    "finding_id": f"legacy_alternative_finding_{len(state.manager_repair_history)}",
                    "severity": "MINOR",
                    "category": "completeness",
                    "issue": "Conclusion Package alternatives are incomplete",
                    "required_action": "Materialize every viable fallback",
                    "affected_agent_ids": ["conclusion.manager"],
                    "affected_candidate_ids": list(
                        state.decision_integration["viable_candidates"]
                    ),
                }
            ],
            "blocking_finding_ids": [],
            "revision_scope": "targeted",
            "revision_targets": ["conclusion.manager"],
            "upstream_revision_requests": [],
            "limitations_to_disclose": [],
            "reviewed_candidate_ids": [
                item["position_candidate_id"] for item in state.position_candidates
            ],
            "reviewed_evaluation_result_id": state.decision_evaluation[
                "decision_evaluation_result_id"
            ],
            "reviewed_integration_result_id": state.decision_integration[
                "decision_integration_result_id"
            ],
        }
        manager.repository.save(state)

    def test_normal_flow_waits_for_human_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            handoff = make_conclusion_handoff(data_dir, provider)
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(handoff))
            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(len(state.position_candidates), 3)
            self.assertFalse(state.playwright_sent)
            self.assertTrue((data_dir / "artifacts" / "conclusion_packages" / f"{state.workflow_id}.json").exists())
            self.assertFalse((data_dir / "outbox" / "playwright" / f"{state.workflow_id}.json").exists())

    def test_package_materializes_every_viable_non_primary_alternative(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )

            primary_id = state.conclusion_package["primary_recommendation"][
                "candidate_id"
            ]
            expected = set(state.decision_integration["viable_candidates"]) - {
                primary_id
            }
            alternatives = state.conclusion_package["alternatives"]
            self.assertEqual(
                {item["candidate_id"] for item in alternatives},
                expected,
            )
            self.assertTrue(
                all(item["reason"] and item["applicable_conditions"] for item in alternatives)
            )
            self.assertTrue(state.deterministic_validation["passed"])
            self.assertEqual(
                state.deterministic_validation["metrics"][
                    "expected_alternative_count"
                ],
                len(expected),
            )
            self.assertEqual(
                state.deterministic_validation["metrics"]["alternative_count"],
                len(expected),
            )

            broken_package = deepcopy(state.conclusion_package)
            broken_package["alternatives"] = []
            validation = ConclusionValidator().validate(
                decision_context=DecisionContext.model_validate(state.decision_context),
                position_generation=PositionGenerationResult.model_validate(
                    state.position_generation
                ),
                decision_evaluation=DecisionEvaluationResult.model_validate(
                    state.decision_evaluation
                ),
                decision_integration=DecisionIntegrationResult.model_validate(
                    state.decision_integration
                ),
                conclusion_package=ConclusionPackage.model_validate(broken_package),
            )
            self.assertFalse(validation.passed)
            self.assertEqual(
                {item.category for item in validation.findings},
                {"alternative_coverage"},
            )
            self.assertEqual(
                validation.metrics["missing_alternative_count"],
                len(expected),
            )

    def test_bounded_manager_repair_at_revision_limit_reuses_specialists(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                max_revisions=2,
                demo_safe_mode=True,
            )
            state = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            self._inject_legacy_manager_alternative_finding(manager, state)
            counts_before = {
                agent_id: provider.agent_calls.count(agent_id)
                for agent_id in (
                    POSITION_GENERATOR_ID,
                    DECISION_EVALUATOR_ID,
                    DECISION_INTEGRATOR_ID,
                    QUALITY_REVIEWER_ID,
                )
            }

            repaired = asyncio.run(manager.revise(state.workflow_id))

            self.assertEqual(repaired.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(repaired.revision_count, 2)
            self.assertEqual(len(repaired.manager_repair_history), 1)
            repair = repaired.manager_repair_history[0]
            expected_alternatives = set(
                repaired.decision_integration["viable_candidates"]
            ) - {
                repaired.conclusion_package["primary_recommendation"]["candidate_id"]
            }
            self.assertEqual(
                set(repair.added_alternative_candidate_ids), expected_alternatives
            )
            self.assertEqual(
                {item["candidate_id"] for item in repaired.conclusion_package["alternatives"]},
                expected_alternatives,
            )
            for agent_id in (
                POSITION_GENERATOR_ID,
                DECISION_EVALUATOR_ID,
                DECISION_INTEGRATOR_ID,
            ):
                self.assertEqual(
                    provider.agent_calls.count(agent_id), counts_before[agent_id]
                )
            self.assertEqual(
                provider.agent_calls.count(QUALITY_REVIEWER_ID),
                counts_before[QUALITY_REVIEWER_ID] + 1,
            )
            requests = [
                message
                for message in repaired.message_history
                if message.sender_agent_id == "conclusion.manager"
                and message.receiver_agent_id == QUALITY_REVIEWER_ID
                and message.payload.get("task_id") == repair.reviewer_task_id
            ]
            self.assertEqual(len(requests), 1)
            self.assertTrue(repair.reviewer_task_id.endswith("_manager_repair_1"))

            self._inject_legacy_manager_alternative_finding(manager, repaired)
            calls_before_exhausted = len(provider.agent_calls)
            exhausted = asyncio.run(manager.revise(repaired.workflow_id))
            self.assertEqual(exhausted.status, "BLOCKED")
            self.assertEqual(exhausted.revision_count, 2)
            self.assertEqual(len(exhausted.manager_repair_history), 1)
            self.assertEqual(len(provider.agent_calls), calls_before_exhausted)

    def test_manager_repair_review_fault_recovers_without_specialist_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _FailManagerRepairReviewOnceProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                max_revisions=2,
                demo_safe_mode=True,
            )
            state = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            self._inject_legacy_manager_alternative_finding(manager, state)
            specialist_counts = {
                agent_id: provider.agent_calls.count(agent_id)
                for agent_id in (
                    POSITION_GENERATOR_ID,
                    DECISION_EVALUATOR_ID,
                    DECISION_INTEGRATOR_ID,
                )
            }
            provider.fail_next_quality_review = True

            failed = asyncio.run(manager.revise(state.workflow_id))

            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.failed_agents, [QUALITY_REVIEWER_ID])
            self.assertEqual(failed.revision_count, 2)
            self.assertEqual(len(failed.manager_repair_history), 1)
            self.assertTrue(failed.deterministic_validation["passed"])
            with self.assertRaisesRegex(
                ValueError,
                "explicit provider retry authorization is required",
            ):
                asyncio.run(manager.recover(failed.workflow_id))

            recovered = asyncio.run(manager.retry_provider_call(failed.workflow_id))

            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(recovered.revision_count, 2)
            self.assertEqual(len(recovered.manager_repair_history), 1)
            for agent_id, count in specialist_counts.items():
                self.assertEqual(provider.agent_calls.count(agent_id), count)
            repair_task_id = recovered.manager_repair_history[0].reviewer_task_id
            quality_tasks = [
                message.payload.get("task_id")
                for message in recovered.message_history
                if message.sender_agent_id == "conclusion.manager"
                and message.receiver_agent_id == QUALITY_REVIEWER_ID
            ]
            self.assertIn(repair_task_id, quality_tasks)
            self.assertIn(repair_task_id + "_operator_retry_1", quality_tasks)

    def test_all_provider_tasks_have_deterministic_stage_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )

            expected = {
                POSITION_GENERATOR_ID: "conclusion_position_generation_upstream_0_revision_0",
                DECISION_EVALUATOR_ID: "conclusion_decision_evaluation_upstream_0_revision_0",
                DECISION_INTEGRATOR_ID: "conclusion_decision_integration_upstream_0_revision_0",
                QUALITY_REVIEWER_ID: "conclusion_quality_review_upstream_0_revision_0",
            }
            requests = {
                message.receiver_agent_id: message
                for message in state.message_history
                if message.sender_agent_id == "conclusion.manager"
                and message.receiver_agent_id in expected
            }
            self.assertEqual(set(requests), set(expected))
            self.assertEqual(
                {agent_id: message.payload["task_id"] for agent_id, message in requests.items()},
                expected,
            )
            self.assertEqual(len(set(expected.values())), len(expected))

    def test_deterministic_validator_recursively_checks_all_reference_kinds(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            context_raw = deepcopy(state.decision_context)
            valid_claim_id = context_raw["key_claim_ids"][0]
            context_raw["tradeoffs"].append(
                {
                    "tradeoff_id": "tradeoff_provider_view",
                    "description": "Narrative must be preserved",
                    "related_claim_ids": [valid_claim_id, "claim_unknown_in_context"],
                    "evidence_ids": [],
                }
            )
            context_view, removed = (
                ConclusionValidator.canonical_decision_context_view(
                    DecisionContext.model_validate(context_raw)
                )
            )
            self.assertEqual(
                context_view.tradeoffs[-1]["description"],
                "Narrative must be preserved",
            )
            self.assertEqual(
                context_view.tradeoffs[-1]["related_claim_ids"],
                [valid_claim_id],
            )
            self.assertEqual(
                {item["id"] for item in removed},
                {"claim_unknown_in_context"},
            )
            position_raw = deepcopy(state.position_generation)
            position_raw["position_candidates"][0]["target_problem_ids"].append(
                "problem_unknown"
            )
            position_raw["position_candidates"][0]["target_stakeholder_ids"].append(
                "stakeholder_unknown"
            )
            evaluation_raw = deepcopy(state.decision_evaluation)
            evaluation_raw["candidate_evaluations"][0]["supporting_analysis_ids"].append(
                "analysis_unknown"
            )
            integration_raw = deepcopy(state.decision_integration)
            integration_raw["major_tradeoffs"].append(
                {
                    "tradeoff_id": "tradeoff_unknown_refs",
                    "description": "Unknown structured references",
                    "related_claim_ids": ["claim_unknown"],
                    "evidence_ids": ["evidence_unknown"],
                }
            )
            integration_raw["integrated_option"]["candidate_ids"][-1] = (
                "candidate_unknown"
            )
            package_raw = deepcopy(state.conclusion_package)
            package_raw["evidence_traceability"][0]["source_id"] = "source_unknown"

            validation = ConclusionValidator().validate(
                decision_context=DecisionContext.model_validate(state.decision_context),
                position_generation=PositionGenerationResult.model_validate(position_raw),
                decision_evaluation=DecisionEvaluationResult.model_validate(evaluation_raw),
                decision_integration=DecisionIntegrationResult.model_validate(integration_raw),
                conclusion_package=ConclusionPackage.model_validate(package_raw),
            )

            self.assertFalse(validation.passed)
            affected = {
                item
                for finding in validation.findings
                if finding.category == "traceability"
                for item in finding.affected_ids
            }
            self.assertTrue(
                {
                    "problem_unknown",
                    "stakeholder_unknown",
                    "analysis_unknown",
                    "claim_unknown",
                    "evidence_unknown",
                    "candidate_unknown",
                    "source_unknown",
                }.issubset(affected)
            )

    def test_position_contract_failure_requires_one_shot_retry_and_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _FailOnceConclusionProvider(
                failed_schema=PositionGenerationResult,
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            self.assertEqual(failed.status, "FAILED")
            self.assertIsNone(failed.position_generation)
            with self.assertRaisesRegex(ValueError, "explicit provider retry authorization"):
                asyncio.run(manager.recover(failed.workflow_id))

            error = next(
                message
                for message in reversed(failed.message_history)
                if message.message_type == "error"
            )
            self.assertEqual(error.payload["error_class"], "ProviderResponseContractError")
            self.assertEqual(error.payload["retry_count"], 0)
            self.assertFalse(error.payload["automatic_retry_allowed"])
            self.assertEqual(error.payload["response_content_sha256"], "a" * 64)
            self.assertEqual(error.payload["response_content_length"], 8)
            self.assertEqual(error.payload["response_root_type"], "float")
            self.assertEqual(error.payload["response_invalid_path"], "$")
            self.assertNotIn("response_content", error.payload)

            recovered = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(provider.attempts["PositionGenerationResult"], 2)
            self.assertEqual(provider.attempts["DecisionEvaluationResult"], 1)
            self.assertEqual(provider.attempts["DecisionIntegrationResult"], 1)
            self.assertEqual(provider.attempts["ConclusionQualityReviewOutput"], 1)

            original_task_id = error.payload["task_id"]
            authorization = manager.provider_retry_store.for_original_task(
                workflow_id=failed.workflow_id,
                provider_id=provider.provider_id,
                original_task_id=original_task_id,
            )
            self.assertIsNotNone(authorization)
            self.assertEqual(authorization.status, "CONSUMED")
            self.assertTrue(Path(authorization.reservation_path).exists())
            with self.assertRaisesRegex(ValueError, "must be FAILED"):
                asyncio.run(manager.retry_provider_call(failed.workflow_id))

    def test_evaluation_recovery_reuses_saved_position_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _FailOnceConclusionProvider(
                failed_schema=DecisionEvaluationResult,
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            saved_position = failed.position_generation
            self.assertEqual(failed.status, "FAILED")
            self.assertIsNotNone(saved_position)

            recovered = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(recovered.position_generation, saved_position)
            self.assertEqual(provider.attempts["PositionGenerationResult"], 1)
            self.assertEqual(provider.attempts["DecisionEvaluationResult"], 2)
            self.assertEqual(provider.attempts["DecisionIntegrationResult"], 1)
            self.assertEqual(provider.attempts["ConclusionQualityReviewOutput"], 1)

    def test_exact_repeated_evaluation_pairs_are_collapsed_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _RepeatedEvaluationProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(data_dir, provider)

            state = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )

            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(
                len(state.decision_evaluation["candidate_evaluations"]),
                3 * 14,
            )
            self.assertEqual(provider.calls.count("DecisionEvaluationResult"), 1)

    def test_invalid_evaluation_reference_fault_retries_only_failed_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _InvalidEvaluationReferenceOnceProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )

            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            saved_position = deepcopy(failed.position_generation)
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.failed_agents, [DECISION_EVALUATOR_ID])
            self.assertEqual(provider.calls.count("PositionGenerationResult"), 1)
            self.assertEqual(provider.calls.count("DecisionEvaluationResult"), 1)
            error = next(
                message
                for message in reversed(failed.message_history)
                if message.message_type == "error"
            )
            self.assertEqual(error.payload["error_class"], "PayloadValidationError")
            self.assertIn("invalid_payload", error.payload)

            recovered = asyncio.run(manager.retry_provider_call(failed.workflow_id))

            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(recovered.position_generation, saved_position)
            self.assertEqual(provider.calls.count("PositionGenerationResult"), 1)
            self.assertEqual(provider.calls.count("DecisionEvaluationResult"), 2)
            self.assertEqual(provider.calls.count("DecisionIntegrationResult"), 1)
            self.assertEqual(provider.calls.count("ConclusionQualityReviewOutput"), 1)

    def test_quality_decision_contradiction_retries_only_reviewer_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _ContradictoryQualityDecisionOnceProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )

            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            saved_position = deepcopy(failed.position_generation)
            saved_evaluation = deepcopy(failed.decision_evaluation)
            saved_integration = deepcopy(failed.decision_integration)
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.failed_agents, [QUALITY_REVIEWER_ID])
            error = next(
                message
                for message in reversed(failed.message_history)
                if message.message_type == "error"
            )
            self.assertEqual(error.payload["error_class"], "PayloadValidationError")
            self.assertEqual(
                error.payload["invalid_payload"]["status"],
                "approved_with_conditions",
            )

            recovered = asyncio.run(manager.retry_provider_call(failed.workflow_id))

            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(recovered.position_generation, saved_position)
            self.assertEqual(recovered.decision_evaluation, saved_evaluation)
            self.assertEqual(recovered.decision_integration, saved_integration)
            self.assertEqual(provider.calls.count("PositionGenerationResult"), 1)
            self.assertEqual(provider.calls.count("DecisionEvaluationResult"), 1)
            self.assertEqual(provider.calls.count("DecisionIntegrationResult"), 1)
            self.assertEqual(provider.calls.count("ConclusionQualityReviewOutput"), 2)

    def test_repeated_contract_failure_uses_one_distinct_model_repair_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            failed_model = "qwen/failing-structured-output"
            repair_model = "openai/gpt-5-mini"
            provider = _ContractInvalidPrimaryModelProvider(
                failed_model=failed_model,
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            manager.registry.get(POSITION_GENERATOR_ID).model = failed_model

            first_failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            retry_failed = asyncio.run(
                manager.retry_provider_call(first_failed.workflow_id)
            )
            self.assertEqual(retry_failed.status, "FAILED")
            self.assertEqual(
                provider.position_models,
                [failed_model, failed_model],
            )
            with self.assertRaisesRegex(
                ValueError,
                "cannot authorize another retry|already consumed",
            ):
                asyncio.run(manager.retry_provider_call(first_failed.workflow_id))
            with self.assertRaisesRegex(ValueError, "contract repair authorization"):
                asyncio.run(manager.recover(first_failed.workflow_id))

            repaired = asyncio.run(
                manager.repair_provider_contract(
                    first_failed.workflow_id,
                    repair_model_id=repair_model,
                )
            )
            self.assertEqual(repaired.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(
                provider.position_models,
                [failed_model, failed_model, repair_model],
            )
            position_requests = [
                message
                for message in repaired.message_history
                if message.sender_agent_id == "conclusion.manager"
                and message.receiver_agent_id == POSITION_GENERATOR_ID
            ]
            task_ids = [message.payload["task_id"] for message in position_requests]
            self.assertEqual(len(task_ids), 3)
            self.assertEqual(len(set(task_ids)), 3)
            self.assertTrue(task_ids[1].endswith("_operator_retry_1"))
            self.assertTrue(task_ids[2].endswith("_provider_contract_repair_1"))
            authorization = (
                manager.provider_contract_repair_store.for_original_task(
                    workflow_id=repaired.workflow_id,
                    provider_id=provider.provider_id,
                    original_task_id=task_ids[0],
                )
            )
            self.assertIsNotNone(authorization)
            self.assertEqual(authorization.status, "CONSUMED")
            self.assertEqual(authorization.repair_model_id, repair_model)
            repair_reservation = json.loads(
                Path(authorization.reservation_path).read_text(encoding="utf-8")
            )
            self.assertEqual(repair_reservation["model_id"], repair_model)
            self.assertEqual(
                provider.agent_calls.count("conclusion.decision_evaluator"),
                1,
            )
            self.assertEqual(
                provider.agent_calls.count("conclusion.decision_integrator"),
                1,
            )
            self.assertEqual(
                provider.agent_calls.count("conclusion.quality_reviewer"),
                1,
            )

    def test_contract_repair_rejects_same_model_and_cannot_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            failed_model = "qwen/failing-structured-output"
            provider = _ContractInvalidPrimaryModelProvider(
                failed_model=failed_model,
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            manager.registry.get(POSITION_GENERATOR_ID).model = failed_model
            first_failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            retry_failed = asyncio.run(
                manager.retry_provider_call(first_failed.workflow_id)
            )
            with self.assertRaisesRegex(ValueError, "different model"):
                manager.authorize_provider_contract_repair(
                    retry_failed.workflow_id,
                    repair_model_id=failed_model,
                )

            repaired = asyncio.run(
                manager.repair_provider_contract(
                    retry_failed.workflow_id,
                    repair_model_id="openai/gpt-5-mini",
                )
            )
            self.assertEqual(repaired.status, "WAITING_HUMAN_SELECTION")
            with self.assertRaisesRegex(ValueError, "must be FAILED"):
                asyncio.run(
                    manager.repair_provider_contract(
                        repaired.workflow_id,
                        repair_model_id="openai/gpt-5-mini",
                    )
                )

    def test_verified_contract_repair_model_is_reused_by_future_logical_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            failed_model = "qwen/failing-structured-output"
            repair_model = "openai/gpt-5-mini"
            provider = _ContractInvalidPrimaryModelProvider(
                failed_model=failed_model,
                reservation_root=data_dir / "provider_call_reservations",
                conclusion_review_decisions=["revision_required", "approved"],
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            manager.registry.get(POSITION_GENERATOR_ID).model = failed_model
            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            failed = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            repaired = asyncio.run(
                manager.repair_provider_contract(
                    failed.workflow_id,
                    repair_model_id=repair_model,
                )
            )

            self.assertEqual(repaired.status, "BLOCKED")
            revised = asyncio.run(manager.revise(repaired.workflow_id))

            self.assertEqual(revised.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(
                provider.position_models,
                [failed_model, failed_model, repair_model, repair_model],
            )
            bindings = manager.provider_model_compatibility_store.list_verified(
                provider_id=provider.provider_id
            )
            self.assertEqual(len(bindings), 1)
            self.assertEqual(bindings[0].compatible_model_id, repair_model)

    def test_legacy_successful_repair_binding_recovers_failed_revision_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            failed_model = "qwen/failing-structured-output"
            repair_model = "openai/gpt-5-mini"
            provider = _ContractInvalidPrimaryModelProvider(
                failed_model=failed_model,
                reservation_root=data_dir / "provider_call_reservations",
                conclusion_review_decisions=["revision_required", "approved"],
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            manager.registry.get(POSITION_GENERATOR_ID).model = failed_model
            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            failed = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            blocked = asyncio.run(
                manager.repair_provider_contract(
                    failed.workflow_id,
                    repair_model_id=repair_model,
                )
            )
            binding_files = list(
                (data_dir / "provider_model_compatibility").rglob("*.json")
            )
            self.assertEqual(len(binding_files), 1)
            binding_files[0].unlink()

            promote = manager._promote_saved_contract_repairs
            manager._promote_saved_contract_repairs = lambda _state: []
            revision_failed = asyncio.run(manager.revise(blocked.workflow_id))
            manager._promote_saved_contract_repairs = promote

            self.assertEqual(revision_failed.status, "FAILED")
            self.assertEqual(
                revision_failed.failed_agents,
                [POSITION_GENERATOR_ID],
            )
            recovered = asyncio.run(
                manager.retry_provider_call(revision_failed.workflow_id)
            )

            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(
                provider.position_models,
                [
                    failed_model,
                    failed_model,
                    repair_model,
                    failed_model,
                    repair_model,
                ],
            )
            retry_reservation = json.loads(
                (
                    data_dir
                    / "provider_call_reservations"
                    / provider.provider_id
                    / recovered.workflow_id
                    / "conclusion_position_generation_upstream_0_revision_1_operator_retry_1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(retry_reservation["model_id"], repair_model)
            self.assertEqual(
                len(
                    manager.provider_model_compatibility_store.list_verified(
                        provider_id=provider.provider_id
                    )
                ),
                1,
            )

    def test_failed_contract_repair_is_exhausted_without_fourth_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            failed_model = "qwen/failing-structured-output"
            repair_model = "openai/gpt-5-mini"
            provider = _ContractInvalidPrimaryModelProvider(
                failed_model=failed_model,
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            manager.registry.get(POSITION_GENERATOR_ID).model = failed_model
            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            failed = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            provider.failed_models.add(repair_model)
            repair_failed = asyncio.run(
                manager.repair_provider_contract(
                    failed.workflow_id,
                    repair_model_id=repair_model,
                )
            )
            self.assertEqual(repair_failed.status, "FAILED")
            self.assertEqual(
                provider.position_models,
                [failed_model, failed_model, repair_model],
            )
            with self.assertRaisesRegex(ValueError, "requires a failed one-shot"):
                asyncio.run(
                    manager.repair_provider_contract(
                        repair_failed.workflow_id,
                        repair_model_id="openai/gpt-5.5",
                    )
                )
            with self.assertRaisesRegex(ValueError, "repair is exhausted"):
                asyncio.run(manager.recover(repair_failed.workflow_id))
            self.assertEqual(len(provider.position_models), 3)

    def test_contract_repair_result_checkpoint_fault_restores_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            failed_model = "qwen/failing-structured-output"
            repair_model = "openai/gpt-5-mini"
            provider = _ContractInvalidPrimaryModelProvider(
                failed_model=failed_model,
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            manager.registry.get(POSITION_GENERATOR_ID).model = failed_model
            canonical_task_id = ConclusionManager._logical_task_id

            def legacy_position_task_id(
                state,
                agent_id,
                *,
                operation_variant=None,
            ):
                if agent_id == POSITION_GENERATOR_ID and operation_variant is None:
                    return "position_task_legacy_checkpoint"
                return canonical_task_id(
                    state,
                    agent_id,
                    operation_variant=operation_variant,
                )

            manager._logical_task_id = legacy_position_task_id
            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            failed = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            repaired = asyncio.run(
                manager.repair_provider_contract(
                    failed.workflow_id,
                    repair_model_id=repair_model,
                )
            )
            del manager._logical_task_id
            calls_before_fault_recovery = list(provider.calls)
            position_models_before = list(provider.position_models)
            repaired.status = "FAILED"
            repaired.position_generation = None
            repaired.position_candidates = []
            repaired.evaluation_framework = None
            repaired.decision_evaluation = None
            repaired.decision_integration = None
            repaired.conclusion_package = None
            repaired.deterministic_validation = None
            repaired.review_result = None
            repaired.error = {"message": "injected checkpoint write fault"}
            manager.repository.save(repaired)

            recovered = asyncio.run(manager.recover(repaired.workflow_id))

            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(provider.calls, calls_before_fault_recovery)
            self.assertEqual(provider.position_models, position_models_before)
            self.assertIsNotNone(recovered.position_generation)
            self.assertIsNotNone(recovered.decision_evaluation)
            self.assertIsNotNone(recovered.decision_integration)
            self.assertIsNotNone(recovered.review_result)

    def test_legacy_non_finite_payload_error_is_retry_eligible_on_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _FailOnceConclusionProvider(
                failed_schema=PositionGenerationResult,
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            error = next(
                message
                for message in reversed(failed.message_history)
                if message.message_type == "error"
            )
            error.payload["error_class"] = "PayloadValidationError"
            error.payload["message"] = (
                "1 validation error for PositionGenerationResult\n"
                "  Input should be a valid dictionary or instance "
                "[type=model_type, input_value=inf, input_type=float]"
            )
            error.payload["validation_field_path"] = None
            manager.repository.save(failed)

            authorization = manager.authorize_provider_retry(failed.workflow_id)
            self.assertEqual(
                authorization.source_error_class,
                "ProviderResponseContractError",
            )

    def test_saved_result_messages_restore_all_missing_checkpoints_without_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(data_dir, provider)
            completed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            original_calls = list(provider.calls)
            original_framework = completed.evaluation_framework
            completed.status = "FAILED"
            completed.position_generation = None
            completed.position_candidates = []
            completed.evaluation_framework = None
            completed.decision_evaluation = None
            completed.decision_integration = None
            completed.conclusion_package = None
            completed.deterministic_validation = None
            completed.review_result = None
            completed.error = {"message": "fault after result-message persistence"}
            manager.repository.save(completed)

            recovered = asyncio.run(manager.recover(completed.workflow_id))

            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(provider.calls, original_calls)
            self.assertEqual(recovered.evaluation_framework, original_framework)
            self.assertIsNotNone(recovered.position_generation)
            self.assertIsNotNone(recovered.decision_evaluation)
            self.assertIsNotNone(recovered.decision_integration)
            self.assertIsNotNone(recovered.review_result)

    def test_human_selection_finalizes_and_writes_playwright_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            selected_id = state.position_candidates[0]["position_candidate_id"]
            final = manager.select(state.workflow_id, [selected_id])
            self.assertEqual(final.status, "COMPLETED")
            self.assertTrue(final.playwright_sent)
            self.assertEqual(final.human_selection["selected_candidate_ids"], [selected_id])
            outbox = data_dir / "outbox" / "playwright" / f"{state.workflow_id}.json"
            message = json.loads(outbox.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "conclusion_handoff")
            self.assertEqual(message["receiver_agent_id"], "playwright.manager")

    def test_invalid_candidate_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            with self.assertRaises(ValueError):
                manager.select(state.workflow_id, ["position_missing"])

    def test_final_selection_is_idempotent_and_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            first = manager.select(waiting.workflow_id, [waiting.position_candidates[0]["position_candidate_id"]])
            second = manager.select(waiting.workflow_id, [waiting.position_candidates[1]["position_candidate_id"]])
            self.assertEqual(first.final_conclusion, second.final_conclusion)

    def test_duplicate_candidate_revision_reruns_all_dependents(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_duplicate_candidates_once=True,
                conclusion_review_decisions=["revision_required", "approved"],
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(state.revision_count, 1)
            self.assertEqual(provider.agent_calls.count("conclusion.position_generator"), 2)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_evaluator"), 2)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_integrator"), 2)

    def test_safe_mode_explicit_revision_runs_exactly_one_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["revision_required", "approved"]
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )

            self.assertEqual(blocked.status, "BLOCKED")
            self.assertEqual(blocked.revision_count, 0)
            revised = asyncio.run(manager.revise(blocked.workflow_id))

            self.assertEqual(revised.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(revised.revision_count, 1)
            self.assertEqual(
                revised.revision_history[0].target_agent_ids,
                [POSITION_GENERATOR_ID],
            )
            for agent_id in (
                POSITION_GENERATOR_ID,
                DECISION_EVALUATOR_ID,
                DECISION_INTEGRATOR_ID,
                QUALITY_REVIEWER_ID,
            ):
                self.assertEqual(provider.agent_calls.count(agent_id), 2)
            revision_requests = [
                message
                for message in revised.message_history
                if message.sender_agent_id == "conclusion.manager"
                and message.payload.get("task_id", "").endswith("_revision_1")
            ]
            self.assertEqual(len(revision_requests), 4)

    def test_safe_mode_explicit_revision_never_enters_a_second_automatic_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=[
                    "revision_required",
                    "revision_required",
                    "approved",
                ]
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            revised = asyncio.run(manager.revise(blocked.workflow_id))

            self.assertEqual(revised.status, "BLOCKED")
            self.assertEqual(revised.revision_count, 1)
            for agent_id in (
                POSITION_GENERATOR_ID,
                DECISION_EVALUATOR_ID,
                DECISION_INTEGRATOR_ID,
                QUALITY_REVIEWER_ID,
            ):
                self.assertEqual(provider.agent_calls.count(agent_id), 2)
            self.assertEqual(len(provider.conclusion_review_decisions), 1)

    def test_reference_contract_recovers_saved_list_payload_without_provider_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _UnknownIntegrationReferencesOnceProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )

            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )

            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.failed_agents, [DECISION_INTEGRATOR_ID])
            self.assertEqual(provider.agent_calls.count(QUALITY_REVIEWER_ID), 0)
            error = next(
                message
                for message in reversed(failed.message_history)
                if message.message_type == "error"
            )
            self.assertEqual(error.payload["error_class"], "PayloadValidationError")
            self.assertTrue(error.payload["invalid_payload"])
            paths = {
                item["loc"] for item in error.payload["validation_errors"]
            }
            self.assertIn(
                "integrated_option.candidate_ids[1]",
                paths,
            )
            self.assertIn(
                "major_tradeoffs[1].related_claim_ids[0]",
                paths,
            )

            recovered = asyncio.run(manager.recover(failed.workflow_id))

            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(provider.integration_attempts, 1)
            self.assertEqual(provider.agent_calls.count(QUALITY_REVIEWER_ID), 1)
            self.assertEqual(len(recovered.provider_payload_recoveries), 1)
            recovery = recovered.provider_payload_recoveries[0]
            self.assertEqual(recovery["agent_id"], DECISION_INTEGRATOR_ID)
            self.assertEqual(
                {item["id"] for item in recovery["removed_references"]},
                {
                    "rationale_for_integration_and_selection",
                    "claim_task_reorganization_observed",
                },
            )
            self.assertEqual(
                recovered.decision_integration["integrated_option"]["candidate_ids"],
                ["position_a"],
            )
            reservations = sorted(
                path.stem
                for path in (
                    data_dir
                    / "provider_call_reservations"
                    / provider.provider_id
                    / failed.workflow_id
                ).glob("*.json")
                if "decision_integration" in path.name
            )
            self.assertEqual(
                reservations,
                [
                    "conclusion_decision_integration_upstream_0_revision_0",
                ],
            )

    def test_saved_unknown_scalar_reference_is_not_auto_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _UnknownIntegrationScalarReferenceProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )

            self.assertEqual(failed.status, "FAILED")
            with self.assertRaisesRegex(
                ValueError,
                "explicit provider retry authorization is required",
            ):
                asyncio.run(manager.recover(failed.workflow_id))
            self.assertEqual(provider.integration_attempts, 1)
            self.assertEqual(provider.agent_calls.count(QUALITY_REVIEWER_ID), 0)

    def test_conclusion_strict_schemas_bind_dynamic_input_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            candidate_ids = [
                item["position_candidate_id"] for item in state.position_candidates
            ]
            position_schema = strict_output_schema(
                PositionGenerationResult,
                input_data={
                    "task_id": "position_task_dynamic_schema",
                    "decision_context": state.decision_context,
                },
            )
            self.assertEqual(strict_schema_violations(position_schema), [])
            self.assertEqual(
                position_schema["$defs"]["PositionCandidate"]["properties"]
                ["supporting_claim_ids"]["items"]["enum"],
                state.decision_context["key_claim_ids"],
            )
            self.assertEqual(
                position_schema["properties"]["task_id"]["enum"],
                ["position_task_dynamic_schema"],
            )

            evaluation_schema = strict_output_schema(
                DecisionEvaluationResult,
                input_data={
                    "task_id": "evaluation_task_dynamic_schema",
                    "decision_context": state.decision_context,
                    "position_candidates": state.position_candidates,
                    "evaluation_framework": state.evaluation_framework,
                },
            )
            self.assertEqual(strict_schema_violations(evaluation_schema), [])
            self.assertEqual(
                evaluation_schema["$defs"]["CandidateCriterionEvaluation"]
                ["properties"]["candidate_id"]["enum"],
                candidate_ids,
            )
            self.assertEqual(
                evaluation_schema["$defs"]["ConditionalAdvantage"]["properties"]
                ["advantaged_candidate_ids"]["items"]["enum"],
                candidate_ids,
            )
            integration_input = {
                "task_id": "integration_task_dynamic_schema",
                "decision_context": state.decision_context,
                "position_candidates": state.position_candidates,
                "decision_evaluation": state.decision_evaluation,
            }
            integration_schema = strict_output_schema(
                DecisionIntegrationResult,
                input_data=integration_input,
            )

            self.assertEqual(strict_schema_violations(integration_schema), [])
            self.assertEqual(
                integration_schema["$defs"]["IntegratedOption"]["properties"]
                ["candidate_ids"]["minItems"],
                1,
            )
            self.assertEqual(
                integration_schema["$defs"]["IntegratedOption"]["properties"]
                ["candidate_ids"]["items"]["enum"],
                candidate_ids,
            )
            for definition in (
                "ExcludedCandidate",
                "CandidateComparisonSummary",
                "RecommendedOption",
            ):
                self.assertEqual(
                    integration_schema["$defs"][definition]["properties"]
                    ["candidate_id"]["enum"],
                    candidate_ids,
                )
            self.assertEqual(
                integration_schema["properties"]["task_id"]["enum"],
                ["integration_task_dynamic_schema"],
            )
            self.assertEqual(
                integration_schema["properties"]["decision_evaluation_result_id"]
                ["enum"],
                [state.decision_evaluation["decision_evaluation_result_id"]],
            )

            review_schema = strict_output_schema(
                ConclusionQualityReviewOutput,
                input_data={
                    "position_generation": state.position_generation,
                    "decision_evaluation": state.decision_evaluation,
                    "decision_integration": state.decision_integration,
                    "conclusion_package": state.conclusion_package,
                    "deterministic_validation": state.deterministic_validation,
                },
            )
            self.assertEqual(strict_schema_violations(review_schema), [])
            self.assertEqual(
                review_schema["properties"]["reviewed_candidate_ids"]["items"]
                ["enum"],
                candidate_ids,
            )
            self.assertEqual(
                review_schema["properties"]["reviewed_integration_result_id"]
                ["enum"],
                [state.decision_integration["decision_integration_result_id"]],
            )
            self.assertEqual(
                review_schema["$defs"]["ConclusionQualityFinding"]["properties"]
                ["affected_candidate_ids"]["items"]["enum"],
                candidate_ids,
            )

    def test_second_explicit_revision_revalidates_legacy_trace_and_starts_at_integrator(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=[
                    "revision_required",
                    "revision_required",
                    "approved",
                ],
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                max_revisions=2,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            blocked = asyncio.run(manager.revise(blocked.workflow_id))
            self.assertEqual(blocked.status, "BLOCKED")
            self.assertEqual(blocked.revision_count, 1)

            blocked.decision_integration["major_tradeoffs"].append(
                {
                    "tradeoff_id": "tradeoff_legacy_unknown_claim",
                    "description": "Legacy schema-valid reference failure",
                    "related_claim_ids": ["claim_task_reorganization_observed"],
                    "evidence_ids": [],
                }
            )
            blocked.decision_integration["integrated_option"]["candidate_ids"][-1] = (
                "rationale_for_integration_and_selection"
            )
            review = deepcopy(blocked.review_result)
            review["reason"] = "Legacy trace-only finding"
            review["findings"][0]["category"] = "traceability"
            review["findings"][0]["affected_agent_ids"] = [
                POSITION_GENERATOR_ID,
                DECISION_INTEGRATOR_ID,
                "conclusion.manager",
            ]
            review["revision_scope"] = "multi_agent"
            review["revision_targets"] = [
                POSITION_GENERATOR_ID,
                DECISION_INTEGRATOR_ID,
                "conclusion.manager",
            ]
            blocked.review_result = review
            manager.repository.save(blocked)
            before_counts = {
                agent_id: provider.agent_calls.count(agent_id)
                for agent_id in (
                    POSITION_GENERATOR_ID,
                    DECISION_EVALUATOR_ID,
                    DECISION_INTEGRATOR_ID,
                    QUALITY_REVIEWER_ID,
                )
            }

            revised = asyncio.run(manager.revise(blocked.workflow_id))

            self.assertEqual(revised.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(revised.revision_count, 2)
            self.assertEqual(
                revised.revision_history[-1].target_agent_ids,
                [DECISION_INTEGRATOR_ID],
            )
            self.assertEqual(
                provider.agent_calls.count(POSITION_GENERATOR_ID),
                before_counts[POSITION_GENERATOR_ID],
            )
            self.assertEqual(
                provider.agent_calls.count(DECISION_EVALUATOR_ID),
                before_counts[DECISION_EVALUATOR_ID],
            )
            self.assertEqual(
                provider.agent_calls.count(DECISION_INTEGRATOR_ID),
                before_counts[DECISION_INTEGRATOR_ID] + 1,
            )
            self.assertEqual(
                provider.agent_calls.count(QUALITY_REVIEWER_ID),
                before_counts[QUALITY_REVIEWER_ID] + 1,
            )
            revision_two_requests = [
                message
                for message in revised.message_history
                if message.sender_agent_id == "conclusion.manager"
                and str(message.payload.get("task_id", "")).endswith("_revision_2")
            ]
            self.assertEqual(
                [message.receiver_agent_id for message in revision_two_requests],
                [DECISION_INTEGRATOR_ID, QUALITY_REVIEWER_ID],
            )

    def test_explicit_revision_limit_stops_before_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["revision_required"],
                reservation_root=data_dir / "provider_call_reservations",
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                max_revisions=2,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            blocked.revision_count = 2
            manager.repository.save(blocked)
            calls_before = provider.calls

            still_blocked = asyncio.run(manager.revise(blocked.workflow_id))

            self.assertEqual(still_blocked.status, "BLOCKED")
            self.assertEqual(still_blocked.revision_count, 2)
            self.assertEqual(provider.calls, calls_before)

    def test_explicit_revision_rejects_true_blocked_quality_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(conclusion_review_decisions=["blocked"])
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )

            with self.assertRaisesRegex(ValueError, "requires a revision_required"):
                asyncio.run(manager.revise(blocked.workflow_id))

    def test_explicit_revision_fault_recovers_without_replaying_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = _FailSecondEvaluationProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            manager = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )
            failed = asyncio.run(manager.revise(blocked.workflow_id))

            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.revision_count, 1)
            self.assertEqual(failed.failed_agents, [DECISION_EVALUATOR_ID])
            saved_revision_position = deepcopy(failed.position_generation)
            recovered = asyncio.run(manager.retry_provider_call(failed.workflow_id))

            self.assertEqual(recovered.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(recovered.position_generation, saved_revision_position)
            self.assertEqual(recovered.revision_count, 1)
            self.assertEqual(provider.evaluation_attempts, 3)
            self.assertEqual(
                provider.agent_calls.count(POSITION_GENERATOR_ID),
                2,
            )
            self.assertEqual(
                provider.agent_calls.count(DECISION_INTEGRATOR_ID),
                2,
            )
            self.assertEqual(
                provider.agent_calls.count(QUALITY_REVIEWER_ID),
                2,
            )

    def test_evaluator_revision_does_not_rerun_position_generator(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["evaluator_revision_required", "approved"]
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(provider.agent_calls.count("conclusion.position_generator"), 1)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_evaluator"), 2)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_integrator"), 2)

    def test_two_revision_required_reviews_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["revision_required", "revision_required"]
            )
            manager = make_conclusion_manager(data_dir, provider, max_revisions=2)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 2)
            self.assertFalse(state.playwright_sent)

    def test_upstream_revision_waits_and_writes_deliberation_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["upstream_revision_required"]
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "WAITING_UPSTREAM_REVISION")
            path = data_dir / "outbox" / "deliberation_revision" / f"{state.workflow_id}.json"
            self.assertTrue(path.exists())
            message = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "revision_request")
            self.assertEqual(message["receiver_agent_id"], "deliberation.manager")

    def test_upstream_revision_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["upstream_revision_required", "approved"]
            )
            handoff = make_conclusion_handoff(data_dir, provider)
            manager = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(handoff))
            revised = handoff.model_dump(mode="json")
            revised["message_id"] = str(uuid4())
            manager.repository.write_json_atomic(
                manager.repository.deliberation_outbox_dir / f"{waiting.workflow_id}.json",
                revised,
            )
            resumed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(resumed.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(resumed.upstream_revision_count, 1)

    def test_blocking_issue_is_not_compensated(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(conclusion_blocking_candidate_id="position_c")
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertNotIn("position_c", state.decision_integration["viable_candidates"])
            self.assertIn("position_c", [item["candidate_id"] for item in state.decision_integration["excluded_candidates"]])

    def test_not_evaluable_is_not_zeroed(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            ratings = [
                item["rating"]
                for item in state.decision_evaluation["candidate_evaluations"]
                if item["candidate_id"] == "position_c" and item["criterion"] == "POLITICAL_FEASIBILITY"
            ]
            self.assertEqual(ratings, ["NOT_EVALUABLE"])

    def test_requested_candidate_integration_is_reviewed_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            candidate_ids = [item["position_candidate_id"] for item in waiting.position_candidates[:2]]
            updated = asyncio.run(manager.integrate_candidates(waiting.workflow_id, candidate_ids))
            self.assertEqual(updated.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(updated.decision_integration["integrated_option"]["candidate_ids"], candidate_ids)
            self.assertEqual(provider.agent_calls.count("conclusion.position_generator"), 1)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_evaluator"), 1)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_integrator"), 2)

    def test_quality_reviewer_failure_fails_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(fail_agent_ids={"conclusion.quality_reviewer"})
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "FAILED")
            self.assertFalse(state.playwright_sent)

    def test_unapproved_deliberation_handoff_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            handoff = make_conclusion_handoff(data_dir, provider)
            raw = handoff.model_dump(mode="json")
            raw["payload"]["quality_review"]["status"] = "revision_required"
            raw["payload"]["deliberation_result"]["quality_review"]["status"] = "revision_required"
            with self.assertRaises(ValueError):
                asyncio.run(manager.start_from_message(PMPMessage.model_validate(raw)))

    def test_lowercase_ready_with_conditions_handoff_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            handoff = make_conclusion_handoff(data_dir, provider)
            raw = handoff.model_dump(mode="json")
            for review in (
                raw["payload"]["quality_review"],
                raw["payload"]["deliberation_result"]["quality_review"],
            ):
                review["status"] = "approved_with_conditions"
                review["conclusion_readiness"] = "ready_with_conditions"
            validated = manager._validate_deliberation_handoff(
                PMPMessage.model_validate(raw)
            )
            self.assertEqual(
                validated.quality_review.conclusion_readiness,
                "ready_with_conditions",
            )

    def test_legacy_uppercase_readiness_is_normalized_on_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            raw = make_conclusion_handoff(data_dir, provider).model_dump(mode="json")
            for review in (
                raw["payload"]["quality_review"],
                raw["payload"]["deliberation_result"]["quality_review"],
            ):
                review["conclusion_readiness"] = "READY"
            validated = manager._validate_deliberation_handoff(
                PMPMessage.model_validate(raw)
            )
            self.assertEqual(validated.quality_review.conclusion_readiness, "ready")

    def test_mismatched_quality_review_copies_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            raw = make_conclusion_handoff(data_dir, provider).model_dump(mode="json")
            raw["payload"]["quality_review"]["review_id"] = "review_conflict"
            with self.assertRaisesRegex(ValueError, "copies do not match"):
                manager._validate_deliberation_handoff(PMPMessage.model_validate(raw))

    def test_not_ready_handoff_is_rejected_before_any_conclusion_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            raw = make_conclusion_handoff(data_dir, provider).model_dump(mode="json")
            calls_before = len(provider.calls)
            for review in (
                raw["payload"]["quality_review"],
                raw["payload"]["deliberation_result"]["quality_review"],
            ):
                review["conclusion_readiness"] = "not_ready"
            with self.assertRaises(ValueError):
                asyncio.run(
                    manager.start_from_message(PMPMessage.model_validate(raw))
                )
            self.assertEqual(len(provider.calls), calls_before)

    def test_invalid_deliberation_routing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            handoff = make_conclusion_handoff(data_dir, provider)
            raw = handoff.model_dump(mode="json")
            raw["sender_agent_id"] = "researcher.manager"
            with self.assertRaises(ValueError):
                asyncio.run(manager.start_from_message(PMPMessage.model_validate(raw)))

    def test_start_is_idempotent_for_saved_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_conclusion_handoff(data_dir, provider)
            manager = make_conclusion_manager(data_dir, provider)
            manager.repository.write_json_atomic(
                manager.repository.deliberation_outbox_dir / f"{handoff.workflow_id}.json",
                handoff.model_dump(mode="json"),
            )
            first = asyncio.run(manager.start(handoff.workflow_id))
            call_count = len(provider.calls)
            second = asyncio.run(manager.start(handoff.workflow_id))
            self.assertEqual(first.status, second.status)
            self.assertEqual(len(provider.calls), call_count)

    def test_every_conclusion_agent_response_has_distinct_rd_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            traces = []
            for message in state.message_history:
                if message.sender_agent_id.startswith("conclusion."):
                    trace = message.metadata.extensions.get("role_definition")
                    if trace:
                        traces.append(trace)
            agent_ids = {item["agent_id"] for item in traces}
            hashes = {item["role_definition_hash"] for item in traces}
            self.assertTrue({
                "conclusion.manager",
                "conclusion.position_generator",
                "conclusion.decision_evaluator",
                "conclusion.decision_integrator",
                "conclusion.quality_reviewer",
            } - agent_ids == set())
            self.assertGreaterEqual(len(hashes), 5)

    def test_playwright_handoff_contains_canonical_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            completed = manager.select(waiting.workflow_id, [waiting.position_candidates[0]["position_candidate_id"]])
            message = json.loads(
                (data_dir / "outbox" / "playwright" / f"{completed.workflow_id}.json").read_text(encoding="utf-8")
            )
            required = {
                "conclusion_id", "topic", "general_opinion", "central_question", "selected_position",
                "recommendations", "decision_rationale", "supporting_claims", "supporting_analysis",
                "evidence_links", "evaluation_summary", "implementation_conditions", "expected_benefits",
                "risks", "trade_offs", "affected_stakeholders", "counterarguments", "uncertainties",
                "limitations", "unresolved_issues", "prohibited_interpretations", "source_registry_reference",
                "quality_review", "workflow_metadata",
            }
            self.assertFalse(required - message["payload"].keys())
            self.assertEqual(message["payload"]["workflow_metadata"]["human_selection"]["selection_type"], "candidate")


if __name__ == "__main__":
    unittest.main()
