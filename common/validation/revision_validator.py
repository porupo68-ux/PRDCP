from __future__ import annotations

from collections.abc import Mapping

from common.models.pmp import MessageType, PMPMessage
from common.models.revision import (
    LayerId,
    RevisionArtifactRef,
    RevisionFindingOutcome,
    RevisionRequestV1,
    RevisionResultV1,
    revision_idempotency_key,
)
from common.validation.pmp_validator import PMPValidator


class RevisionCorrelationValidator:
    """Validate canonical Revision payloads together with their PMP envelope."""

    def __init__(self, pmp_validator: PMPValidator | None = None) -> None:
        self.pmp_validator = pmp_validator or PMPValidator()

    def validate_request_message(self, message: PMPMessage | dict) -> RevisionRequestV1:
        envelope = self.pmp_validator.validate(message)
        if envelope.message_type != MessageType.REVISION_REQUEST.value:
            raise ValueError("Canonical Revision Request must use revision_request")
        if envelope.parent_message_id is None:
            raise ValueError("Revision Request must reference its source review message")
        request = RevisionRequestV1.model_validate(envelope.payload)
        if request.workflow_id != envelope.workflow_id:
            raise ValueError("Revision Request workflow_id does not match PMP workflow_id")
        self._validate_agent_layer(
            envelope.sender_agent_id,
            LayerId(request.source_layer),
            "sender_agent_id",
        )
        self._validate_agent_layer(
            envelope.receiver_agent_id,
            LayerId(request.target_layer),
            "receiver_agent_id",
        )
        if envelope.routing.revision_target != envelope.receiver_agent_id:
            raise ValueError("Revision Request routing target must match its receiver")
        if not envelope.routing.reply_required:
            raise ValueError("Revision Request must require a reply")
        self._validate_idempotency(request)
        return request

    def validate_result_message(
        self,
        request_message: PMPMessage | dict,
        result_message: PMPMessage | dict,
    ) -> RevisionResultV1:
        request_envelope = self.pmp_validator.validate(request_message)
        request = self.validate_request_message(request_envelope)
        result_envelope = self.pmp_validator.validate(result_message)
        if result_envelope.message_type != MessageType.REVISION_RESULT.value:
            raise ValueError("Canonical Revision Result must use revision_result")
        result = RevisionResultV1.model_validate(result_envelope.payload)
        if result_envelope.parent_message_id != request_envelope.message_id:
            raise ValueError("Revision Result parent_message_id does not match its request")
        if result.request_message_id != request_envelope.message_id:
            raise ValueError("Revision Result payload does not identify its request message")
        if result.workflow_id != request.workflow_id or result_envelope.workflow_id != request.workflow_id:
            raise ValueError("Revision Result workflow_id does not match its request")
        if result.revision_request_id != request.revision_request_id:
            raise ValueError("Revision Result request ID does not match its request")
        if result.revision_epoch != request.revision_epoch:
            raise ValueError("Revision Result epoch does not match its request")
        if result.requester_layer != request.source_layer:
            raise ValueError("Revision Result requester_layer does not match its request")
        if result.producer_layer != request.target_layer:
            raise ValueError("Revision Result producer_layer does not match its request")
        if (
            request.source_layer == LayerId.PLAYWRIGHT.value
            and result.human_selection_impact
            != request.expected_human_selection_impact
        ):
            raise ValueError(
                "Conclusion Revision Result human-selection impact does not match its request"
            )
        if result_envelope.sender_agent_id != request_envelope.receiver_agent_id:
            raise ValueError("Revision Result sender must be the request receiver")
        if result_envelope.receiver_agent_id != request_envelope.sender_agent_id:
            raise ValueError("Revision Result receiver must be the request sender")
        if result_envelope.routing.reply_required:
            raise ValueError("Revision Result must not require a further direct reply")
        self._validate_artifact_identity(request.base_artifacts, result.base_artifacts)
        expected_findings = set(request.source_finding_ids)
        actual_findings = {item.finding_id for item in result.finding_dispositions}
        if actual_findings != expected_findings:
            raise ValueError("Revision Result must disposition every source finding exactly once")
        if result.status == "completed" and any(
            item.outcome
            in {
                RevisionFindingOutcome.UNRESOLVED.value,
                RevisionFindingOutcome.ROUTED_UPSTREAM.value,
            }
            for item in result.finding_dispositions
        ):
            raise ValueError("Completed Revision Result contains unresolved findings")
        self._validate_idempotency(result)
        return result

    @staticmethod
    def validate_current_base_artifacts(
        request: RevisionRequestV1,
        current_hashes: Mapping[tuple[str, str], str],
    ) -> None:
        missing: list[str] = []
        stale: list[str] = []
        for artifact in request.base_artifacts:
            key = (artifact.artifact_type, artifact.artifact_id)
            current = current_hashes.get(key)
            label = f"{key[0]}:{key[1]}"
            if current is None:
                missing.append(label)
            elif current != artifact.sha256:
                stale.append(label)
        if missing:
            raise ValueError("Revision base artifact is missing: " + ", ".join(missing))
        if stale:
            raise ValueError("Revision request is stale for: " + ", ".join(stale))

    @staticmethod
    def _validate_artifact_identity(
        expected: list[RevisionArtifactRef],
        actual: list[RevisionArtifactRef],
    ) -> None:
        expected_payload = [item.model_dump(mode="json") for item in expected]
        actual_payload = [item.model_dump(mode="json") for item in actual]
        if expected_payload != actual_payload:
            raise ValueError("Revision Result base artifacts do not match its request")

    @staticmethod
    def _validate_agent_layer(agent_id: str, layer: LayerId, field_name: str) -> None:
        if not agent_id.startswith(f"{layer.value}."):
            raise ValueError(f"{field_name} does not belong to {layer.value}")

    @staticmethod
    def _validate_idempotency(payload) -> None:
        data = payload.model_dump(mode="json")
        expected = revision_idempotency_key(data)
        if payload.idempotency_key != expected:
            raise ValueError("Revision payload idempotency_key does not match its content")
