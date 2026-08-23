from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from common.models.pmp import (
    MessageStatus,
    MessageType,
    PMPMessage,
    PMPMetadata,
    PMPRouting,
)
from common.models.revision import (
    HumanSelectionImpact,
    LayerId,
    RevisionArtifactRef,
    RevisionAuditEvent,
    RevisionAuditEventType,
    RevisionBudgetPolicy,
    RevisionControlPhase,
    RevisionExecutionAuthorization,
    RevisionFindingDisposition,
    RevisionFindingOutcome,
    RevisionRequestV1,
    RevisionResultV1,
    RevisionRoute,
    deterministic_revision_request_id,
)
from common.specifications import audit_common_specifications
from common.validation import RevisionCorrelationValidator
from discord_app.channel_router import COMMAND_CHANNEL_RULES
from conclusion.state import ConclusionWorkflowState
from deliberation.state import DeliberationWorkflowState
from playwright.state import PlaywrightWorkflowState
from producer.state import ProducerWorkflowState
from researcher.state import ResearcherWorkflowState
from storage.json_repository import JsonRepository
from storage.revision_exchange_repository import (
    RevisionBudgetExhausted,
    RevisionBudgetStore,
    RevisionExchangeRepository,
)


class RevisionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow_id = str(uuid4())
        self.base_artifact = RevisionArtifactRef(
            artifact_type="deliberation.result",
            artifact_id="deliberation_result_1",
            sha256="a" * 64,
        )

    def make_request(
        self,
        *,
        source_layer: LayerId = LayerId.CONCLUSION,
        target_layer: LayerId = LayerId.DELIBERATION,
        route: RevisionRoute = RevisionRoute.UPSTREAM,
        request_id: str | None = None,
    ) -> RevisionRequestV1:
        request_id = request_id or deterministic_revision_request_id(
            workflow_id=self.workflow_id,
            source_layer=source_layer,
            target_layer=target_layer,
            revision_epoch=1,
            source_review_id="conclusion_review_1",
            source_finding_ids=["finding_1"],
        )
        return RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=self.workflow_id,
            route=route,
            source_layer=source_layer,
            target_layer=target_layer,
            revision_epoch=1,
            root_revision_request_id=request_id,
            source_review_id="conclusion_review_1",
            source_finding_ids=["finding_1"],
            target_agent_ids=[f"{target_layer.value}.manager"],
            base_artifacts=[self.base_artifact],
            required_actions=["Rebuild the affected analysis"],
            acceptance_conditions=["finding_1 is resolved"],
        )

    def make_request_message(self, request: RevisionRequestV1) -> PMPMessage:
        sender_agent_id = f"{request.source_layer}.manager"
        receiver_agent_id = f"{request.target_layer}.manager"
        if request.route == RevisionRoute.INTERNAL.value:
            sender_agent_id = f"{request.source_layer}.quality_reviewer"
            receiver_agent_id = request.target_agent_ids[0]
        return PMPMessage.create(
            workflow_id=request.workflow_id,
            parent_message_id=str(uuid4()),
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            message_type=MessageType.REVISION_REQUEST,
            objective="Execute an audited upstream revision",
            payload=request.model_dump(mode="json"),
            routing=PMPRouting(
                revision_target=receiver_agent_id,
                reply_required=True,
            ),
            metadata=PMPMetadata(status=MessageStatus.REVISION_REQUIRED),
        )

    def make_result(
        self,
        request: RevisionRequestV1,
        request_message: PMPMessage,
    ) -> RevisionResultV1:
        return RevisionResultV1.create(
            revision_result_id="revision_result_1",
            revision_request_id=request.revision_request_id,
            request_message_id=request_message.message_id,
            workflow_id=request.workflow_id,
            requester_layer=request.source_layer,
            producer_layer=request.target_layer,
            revision_epoch=request.revision_epoch,
            status="completed",
            base_artifacts=request.base_artifacts,
            result_artifacts=[
                RevisionArtifactRef(
                    artifact_type="deliberation.result",
                    artifact_id="deliberation_result_2",
                    sha256="b" * 64,
                    version=2,
                )
            ],
            finding_dispositions=[
                RevisionFindingDisposition(
                    finding_id="finding_1",
                    outcome=RevisionFindingOutcome.RESOLVED,
                    reason="The affected analysis was rebuilt",
                    result_artifact_ids=["deliberation_result_2"],
                )
            ],
        )

    @staticmethod
    def make_result_message(
        request_message: PMPMessage,
        result: RevisionResultV1,
    ) -> PMPMessage:
        return PMPMessage.create(
            workflow_id=result.workflow_id,
            parent_message_id=request_message.message_id,
            sender_agent_id=request_message.receiver_agent_id,
            receiver_agent_id=request_message.sender_agent_id,
            message_type=MessageType.REVISION_RESULT,
            objective="Return the audited revision result",
            payload=result.model_dump(mode="json"),
            routing=PMPRouting(reply_required=False),
            metadata=PMPMetadata(status=MessageStatus.COMPLETED),
        )

    def test_internal_and_adjacent_upstream_routes_are_valid(self) -> None:
        upstream = self.make_request()
        self.assertEqual(upstream.target_layer, "deliberation")
        internal = self.make_request(
            source_layer=LayerId.PRODUCER,
            target_layer=LayerId.PRODUCER,
            route=RevisionRoute.INTERNAL,
        )
        self.assertEqual(internal.route, "internal")

    def test_producer_upstream_and_layer_skipping_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Producer cannot"):
            self.make_request(
                source_layer=LayerId.PRODUCER,
                target_layer=LayerId.RESEARCHER,
            )
        with self.assertRaisesRegex(ValidationError, "immediately preceding"):
            self.make_request(
                source_layer=LayerId.PLAYWRIGHT,
                target_layer=LayerId.DELIBERATION,
            )

    def test_request_and_result_correlation_is_strict(self) -> None:
        request = self.make_request()
        request_message = self.make_request_message(request)
        result = self.make_result(request, request_message)
        result_message = self.make_result_message(request_message, result)
        validator = RevisionCorrelationValidator()
        self.assertEqual(
            validator.validate_request_message(request_message),
            request,
        )
        self.assertEqual(
            validator.validate_result_message(request_message, result_message),
            result,
        )

        stale = result_message.model_copy(
            update={"parent_message_id": str(uuid4())}
        )
        with self.assertRaisesRegex(ValueError, "parent_message_id"):
            validator.validate_result_message(request_message, stale)

    def test_stale_base_artifact_is_rejected(self) -> None:
        request = self.make_request()
        with self.assertRaisesRegex(ValueError, "stale"):
            RevisionCorrelationValidator.validate_current_base_artifacts(
                request,
                {("deliberation.result", "deliberation_result_1"): "c" * 64},
            )

    def test_playwright_result_requires_human_selection_impact(self) -> None:
        request = self.make_request(
            source_layer=LayerId.PLAYWRIGHT,
            target_layer=LayerId.CONCLUSION,
        )
        request_message = self.make_request_message(request)
        with self.assertRaisesRegex(ValidationError, "selection impact"):
            RevisionResultV1.create(
                revision_result_id="revision_result_pw",
                revision_request_id=request.revision_request_id,
                request_message_id=request_message.message_id,
                workflow_id=request.workflow_id,
                requester_layer=request.source_layer,
                producer_layer=request.target_layer,
                revision_epoch=1,
                status="completed",
                base_artifacts=request.base_artifacts,
                result_artifacts=[self.base_artifact],
                finding_dispositions=[
                    RevisionFindingDisposition(
                        finding_id="finding_1",
                        outcome="resolved",
                        reason="Resolved",
                    )
                ],
            )

        result = RevisionResultV1.create(
            revision_result_id="revision_result_pw",
            revision_request_id=request.revision_request_id,
            request_message_id=request_message.message_id,
            workflow_id=request.workflow_id,
            requester_layer=request.source_layer,
            producer_layer=request.target_layer,
            revision_epoch=1,
            status="completed",
            base_artifacts=request.base_artifacts,
            result_artifacts=[self.base_artifact],
            finding_dispositions=[
                RevisionFindingDisposition(
                    finding_id="finding_1",
                    outcome="resolved",
                    reason="Resolved",
                )
            ],
            human_selection_impact=HumanSelectionImpact.RESELECTION_REQUIRED,
        )
        self.assertEqual(result.human_selection_impact, "reselection_required")

    def test_request_result_repository_is_create_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = RevisionExchangeRepository(Path(temporary))
            request = self.make_request()
            request_message = self.make_request_message(request)
            first = repository.create_request_once(request_message)
            replay = repository.create_request_once(request_message)
            self.assertEqual(first, replay)
            loaded = repository.load_request(
                target_layer=request.target_layer,
                workflow_id=request.workflow_id,
                revision_request_id=request.revision_request_id,
            )
            self.assertEqual(loaded, request_message)

            conflicting_request = request.model_copy(
                update={
                    "required_actions": ["A conflicting action"],
                }
            )
            conflicting_request = RevisionRequestV1.create(
                **{
                    key: value
                    for key, value in conflicting_request.model_dump(mode="json").items()
                    if key != "idempotency_key"
                }
            )
            conflicting_message = self.make_request_message(conflicting_request)
            with self.assertRaisesRegex(ValueError, "identity conflict"):
                repository.create_request_once(conflicting_message)

            result = self.make_result(request, request_message)
            result_message = self.make_result_message(request_message, result)
            result_path = repository.create_result_once(request_message, result_message)
            self.assertEqual(
                repository.load_result(
                    requester_layer=request.source_layer,
                    workflow_id=request.workflow_id,
                    revision_request_id=request.revision_request_id,
                    request_message=request_message,
                ),
                result_message,
            )

    def test_internal_revision_artifacts_never_enter_cross_layer_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = RevisionExchangeRepository(Path(temporary))
            request = self.make_request(
                source_layer=LayerId.PRODUCER,
                target_layer=LayerId.PRODUCER,
                route=RevisionRoute.INTERNAL,
            )
            request_message = self.make_request_message(request)
            path = repository.create_internal_request_once(request_message)
            self.assertIn("artifacts", path.parts)
            self.assertNotIn("outbox", path.parts)
            self.assertEqual(
                repository.load_internal_request(
                    layer=LayerId.PRODUCER,
                    workflow_id=self.workflow_id,
                    revision_request_id=request.revision_request_id,
                ),
                request_message,
            )

            result = self.make_result(request, request_message)
            result_message = self.make_result_message(request_message, result)
            result_path = repository.create_internal_result_once(
                request_message,
                result_message,
            )
            self.assertIn("artifacts", result_path.parts)
            self.assertNotIn("outbox", result_path.parts)
            self.assertTrue(result_path.exists())

    def test_budget_is_idempotent_separate_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = RevisionBudgetStore(Path(temporary))
            policy = RevisionBudgetPolicy(internal_limit=1, upstream_limit=1)
            first = store.consume(
                policy=policy,
                workflow_id=self.workflow_id,
                layer=LayerId.CONCLUSION,
                route=RevisionRoute.UPSTREAM,
                revision_request_id="request_1",
            )
            replay = store.consume(
                policy=policy,
                workflow_id=self.workflow_id,
                layer=LayerId.CONCLUSION,
                route=RevisionRoute.UPSTREAM,
                revision_request_id="request_1",
            )
            self.assertEqual(first, replay)
            with self.assertRaises(RevisionBudgetExhausted):
                store.consume(
                    policy=policy,
                    workflow_id=self.workflow_id,
                    layer=LayerId.CONCLUSION,
                    route=RevisionRoute.UPSTREAM,
                    revision_request_id="request_2",
                )
            internal = store.consume(
                policy=policy,
                workflow_id=self.workflow_id,
                layer=LayerId.CONCLUSION,
                route=RevisionRoute.INTERNAL,
                revision_request_id="request_internal",
            )
            self.assertEqual(internal.iteration, 1)

    def test_authorization_and_audit_are_durable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = RevisionExchangeRepository(Path(temporary))
            authorization = RevisionExecutionAuthorization(
                authorization_id="revision_auth_1",
                workflow_id=self.workflow_id,
                revision_request_id="request_1",
                executing_layer=LayerId.DELIBERATION,
                actor_id="operator_1",
                actor_source="CLI",
                reason="Approved one bounded Provider call",
                max_provider_calls=1,
            )
            repository.create_authorization_once(authorization)
            consumed = repository.consume_authorization(
                authorization,
                provider_reservation_ids=["provider_reservation_1"],
                retrieval_reservation_ids=[],
            )
            replay = repository.consume_authorization(
                consumed,
                provider_reservation_ids=["provider_reservation_1"],
                retrieval_reservation_ids=[],
            )
            self.assertEqual(consumed, replay)
            self.assertEqual(consumed.status, "consumed")

            event = RevisionAuditEvent(
                audit_event_id="audit_1",
                workflow_id=self.workflow_id,
                revision_request_id="request_1",
                layer=LayerId.DELIBERATION,
                event_type=RevisionAuditEventType.AUTHORIZATION_CONSUMED,
                actor_id="operator_1",
                reservation_ids=["provider_reservation_1"],
            )
            first = repository.create_audit_event_once(event)
            second = repository.create_audit_event_once(event)
            self.assertEqual(first, second)
            self.assertEqual(
                repository.list_audit_events(
                    workflow_id=self.workflow_id,
                    revision_request_id="request_1",
                ),
                [event],
            )

    def test_legacy_outbox_is_read_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            repository = RevisionExchangeRepository(data_dir)
            legacy = PMPMessage.create(
                workflow_id=self.workflow_id,
                parent_message_id=str(uuid4()),
                sender_agent_id="deliberation.manager",
                receiver_agent_id="researcher.manager",
                message_type=MessageType.RESEARCH_REVISION_REQUEST,
                objective="Legacy research revision",
                payload={"revision_requests": [{"revision_request_id": "legacy_1"}]},
                routing=PMPRouting(
                    revision_target="researcher.manager",
                    reply_required=True,
                ),
            )
            path = data_dir / "outbox" / "researcher_revision" / f"{self.workflow_id}.json"
            JsonRepository.write_json_atomic(path, legacy.model_dump(mode="json"))
            before = path.read_bytes()
            loaded = repository.load_legacy_request(
                target_layer=LayerId.RESEARCHER,
                workflow_id=self.workflow_id,
            )
            self.assertEqual(loaded, legacy)
            self.assertEqual(path.read_bytes(), before)

    def test_old_layer_states_load_with_idle_revision_control(self) -> None:
        states = [
            ProducerWorkflowState(
                workflow_id=self.workflow_id,
                initial_request={},
            ),
            ResearcherWorkflowState(
                workflow_id=self.workflow_id,
                producer_handoff={},
                research_plan={},
            ),
            DeliberationWorkflowState(
                workflow_id=self.workflow_id,
                researcher_handoff={},
                research_report={},
            ),
            ConclusionWorkflowState(
                workflow_id=self.workflow_id,
                deliberation_handoff={},
                deliberation_result={},
            ),
            PlaywrightWorkflowState(
                workflow_id=self.workflow_id,
                conclusion_handoff={},
                final_conclusion={},
                conclusion_package={},
                human_selection={},
                traceability_manifest={},
                final_conclusion_hash="legacy_hash",
            ),
        ]
        for state in states:
            with self.subTest(state=type(state).__name__):
                legacy_payload = state.model_dump(mode="json")
                legacy_payload.pop("revision_control")
                restored = type(state).model_validate(legacy_payload)
                self.assertEqual(restored.revision_control.phase, RevisionControlPhase.IDLE.value)

    def test_common_specifications_register_revision_result(self) -> None:
        checks = audit_common_specifications()
        failures = [check for check in checks if not check.passed]
        self.assertEqual(failures, [])
        self.assertEqual(COMMAND_CHANNEL_RULES["producer_revise"], "producer")


if __name__ == "__main__":
    unittest.main()
