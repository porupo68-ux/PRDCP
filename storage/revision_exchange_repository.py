from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from common.models.pmp import PMPMessage
from common.models.revision import (
    LayerId,
    RevisionAuditEvent,
    RevisionAuthorizationStatus,
    RevisionBudgetConsumption,
    RevisionBudgetPolicy,
    RevisionExecutionAuthorization,
    RevisionRoute,
    canonical_sha256,
    safe_path_component,
    utc_now,
)
from common.validation.revision_validator import RevisionCorrelationValidator
from storage.json_repository import JsonRepository


LEGACY_REQUEST_DIRECTORY: dict[LayerId, str] = {
    LayerId.RESEARCHER: "researcher_revision",
    LayerId.DELIBERATION: "deliberation_revision",
    LayerId.CONCLUSION: "conclusion_revision",
}


class RevisionBudgetExhausted(ValueError):
    pass


class RevisionBudgetStore(JsonRepository):
    """Race-safe append-only budget slots, separate from retry/repair budgets."""

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "artifacts" / "revision_budget"

    def consume(
        self,
        *,
        policy: RevisionBudgetPolicy,
        workflow_id: str,
        layer: LayerId | str,
        route: RevisionRoute | str,
        revision_request_id: str,
    ) -> RevisionBudgetConsumption:
        layer_id = LayerId(layer)
        route_id = RevisionRoute(route)
        limit = policy.limit_for(route_id)
        directory = (
            self.root
            / layer_id.value
            / safe_path_component(workflow_id)
            / route_id.value
        )
        directory.mkdir(parents=True, exist_ok=True)

        existing = self.for_request(
            workflow_id=workflow_id,
            layer=layer_id,
            route=route_id,
            revision_request_id=revision_request_id,
        )
        if existing is not None:
            return existing
        if limit == 0:
            raise RevisionBudgetExhausted(
                f"{layer_id.value} {route_id.value} revision budget is disabled"
            )

        for iteration in range(1, limit + 1):
            record = RevisionBudgetConsumption(
                workflow_id=workflow_id,
                layer=layer_id,
                route=route_id,
                revision_request_id=revision_request_id,
                iteration=iteration,
            )
            path = directory / f"slot_{iteration}.json"
            try:
                self._write_json_exclusive(path, record.model_dump(mode="json"))
                return record
            except FileExistsError:
                current = RevisionBudgetConsumption.model_validate(self.read_json(path))
                if current.revision_request_id == revision_request_id:
                    return current
                continue
        raise RevisionBudgetExhausted(
            f"{layer_id.value} {route_id.value} revision budget {limit} is exhausted"
        )

    def list_consumptions(
        self,
        *,
        workflow_id: str,
        layer: LayerId | str,
        route: RevisionRoute | str,
    ) -> list[RevisionBudgetConsumption]:
        directory = (
            self.root
            / LayerId(layer).value
            / safe_path_component(workflow_id)
            / RevisionRoute(route).value
        )
        if not directory.exists():
            return []
        return [
            RevisionBudgetConsumption.model_validate(self.read_json(path))
            for path in sorted(directory.glob("slot_*.json"))
        ]

    def for_request(
        self,
        *,
        workflow_id: str,
        layer: LayerId | str,
        route: RevisionRoute | str,
        revision_request_id: str,
    ) -> RevisionBudgetConsumption | None:
        return next(
            (
                item
                for item in self.list_consumptions(
                    workflow_id=workflow_id,
                    layer=layer,
                    route=route,
                )
                if item.revision_request_id == revision_request_id
            ),
            None,
        )

    @staticmethod
    def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class RevisionExchangeRepository(JsonRepository):
    """Canonical request/result Outbox with legacy single-file read fallback."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.request_root = data_dir / "outbox" / "revision_requests"
        self.result_root = data_dir / "outbox" / "revision_results"
        self.internal_request_root = data_dir / "artifacts" / "revision_requests" / "internal"
        self.internal_result_root = data_dir / "artifacts" / "revision_results" / "internal"
        self.audit_root = data_dir / "artifacts" / "revision_audit"
        self.authorization_root = data_dir / "revision_authorizations"
        self.legacy_outbox_root = data_dir / "outbox"
        self.validator = RevisionCorrelationValidator()
        self.budget_store = RevisionBudgetStore(data_dir)

    def create_internal_request_once(self, message: PMPMessage) -> Path:
        """Persist an in-process request as an audit artifact, never as transport."""

        request = self.validator.validate_request_message(message)
        if request.route != RevisionRoute.INTERNAL.value:
            raise ValueError("Internal request storage accepts only internal revisions")
        path = (
            self.internal_request_root
            / LayerId(request.source_layer).value
            / safe_path_component(request.workflow_id)
            / f"{safe_path_component(request.revision_request_id)}.json"
        )
        self._write_json_once_or_exact(
            path,
            message.model_dump(mode="json"),
            "Internal Revision Request identity conflict",
        )
        return path

    def load_internal_request(
        self,
        *,
        layer: LayerId | str,
        workflow_id: str,
        revision_request_id: str,
    ) -> PMPMessage:
        path = (
            self.internal_request_root
            / LayerId(layer).value
            / safe_path_component(workflow_id)
            / f"{safe_path_component(revision_request_id)}.json"
        )
        if not path.exists():
            raise FileNotFoundError(
                f"Internal Revision Request not found: {workflow_id}/{revision_request_id}"
            )
        message = PMPMessage.model_validate(self.read_json(path))
        request = self.validator.validate_request_message(message)
        if request.route != RevisionRoute.INTERNAL.value:
            raise ValueError("Stored internal request has a non-internal route")
        return message

    def create_internal_result_once(
        self,
        request_message: PMPMessage,
        result_message: PMPMessage,
    ) -> Path:
        request = self.validator.validate_request_message(request_message)
        if request.route != RevisionRoute.INTERNAL.value:
            raise ValueError("Internal result storage accepts only internal revisions")
        result = self.validator.validate_result_message(request_message, result_message)
        path = (
            self.internal_result_root
            / LayerId(result.requester_layer).value
            / safe_path_component(result.workflow_id)
            / f"{safe_path_component(result.revision_request_id)}.json"
        )
        self._write_json_once_or_exact(
            path,
            result_message.model_dump(mode="json"),
            "Internal Revision Result identity conflict",
        )
        return path

    def create_request_once(
        self,
        message: PMPMessage,
        *,
        budget_policy: RevisionBudgetPolicy | None = None,
    ) -> Path:
        request = self.validator.validate_request_message(message)
        if budget_policy is not None:
            self.budget_store.consume(
                policy=budget_policy,
                workflow_id=request.workflow_id,
                layer=request.source_layer,
                route=request.route,
                revision_request_id=request.revision_request_id,
            )
        path = self._request_path(
            request.target_layer,
            request.workflow_id,
            request.revision_request_id,
        )
        self._write_json_once_or_exact(
            path,
            message.model_dump(mode="json"),
            "Revision Request identity conflict",
        )
        return path

    def load_request(
        self,
        *,
        target_layer: LayerId | str,
        workflow_id: str,
        revision_request_id: str,
    ) -> PMPMessage:
        path = self._request_path(target_layer, workflow_id, revision_request_id)
        if not path.exists():
            raise FileNotFoundError(
                f"Revision Request not found: {workflow_id}/{revision_request_id}"
            )
        message = PMPMessage.model_validate(self.read_json(path))
        self.validator.validate_request_message(message)
        return message

    def list_requests(
        self,
        *,
        target_layer: LayerId | str,
        workflow_id: str,
    ) -> list[PMPMessage]:
        directory = (
            self.request_root
            / LayerId(target_layer).value
            / safe_path_component(workflow_id)
        )
        if not directory.exists():
            return []
        messages = [
            PMPMessage.model_validate(self.read_json(path))
            for path in sorted(directory.glob("*.json"))
        ]
        for message in messages:
            self.validator.validate_request_message(message)
        return messages

    def load_legacy_request(
        self,
        *,
        target_layer: LayerId | str,
        workflow_id: str,
    ) -> PMPMessage:
        layer = LayerId(target_layer)
        directory_name = LEGACY_REQUEST_DIRECTORY.get(layer)
        if directory_name is None:
            raise FileNotFoundError(
                f"No legacy Revision Request route exists for {layer.value}"
            )
        path = self.legacy_outbox_root / directory_name / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Legacy Revision Request not found: {workflow_id}")
        return PMPMessage.model_validate(self.read_json(path))

    def create_result_once(
        self,
        request_message: PMPMessage,
        result_message: PMPMessage,
    ) -> Path:
        result = self.validator.validate_result_message(request_message, result_message)
        path = self._result_path(
            result.requester_layer,
            result.workflow_id,
            result.revision_request_id,
        )
        self._write_json_once_or_exact(
            path,
            result_message.model_dump(mode="json"),
            "Revision Result identity conflict",
        )
        return path

    def load_result(
        self,
        *,
        requester_layer: LayerId | str,
        workflow_id: str,
        revision_request_id: str,
        request_message: PMPMessage,
    ) -> PMPMessage:
        path = self._result_path(requester_layer, workflow_id, revision_request_id)
        if not path.exists():
            raise FileNotFoundError(
                f"Revision Result not found: {workflow_id}/{revision_request_id}"
            )
        message = PMPMessage.model_validate(self.read_json(path))
        self.validator.validate_result_message(request_message, message)
        return message

    def create_authorization_once(
        self,
        authorization: RevisionExecutionAuthorization,
    ) -> Path:
        if authorization.status != RevisionAuthorizationStatus.PENDING.value:
            raise ValueError("New Revision authorization must be pending")
        path = self._authorization_path(authorization)
        self._write_json_once_or_exact(
            path,
            authorization.model_dump(mode="json"),
            "Revision authorization identity conflict",
        )
        return path

    def load_authorization(
        self,
        *,
        executing_layer: LayerId | str,
        workflow_id: str,
        revision_request_id: str,
    ) -> RevisionExecutionAuthorization:
        path = self._authorization_path_values(
            executing_layer,
            workflow_id,
            revision_request_id,
        )
        if not path.exists():
            raise FileNotFoundError(
                f"Revision authorization not found: {workflow_id}/{revision_request_id}"
            )
        return RevisionExecutionAuthorization.model_validate(self.read_json(path))

    def consume_authorization(
        self,
        authorization: RevisionExecutionAuthorization,
        *,
        provider_reservation_ids: list[str],
        retrieval_reservation_ids: list[str],
    ) -> RevisionExecutionAuthorization:
        path = self._authorization_path(authorization)
        current = RevisionExecutionAuthorization.model_validate(self.read_json(path))
        expected_identity = (
            authorization.authorization_id,
            authorization.workflow_id,
            authorization.revision_request_id,
            authorization.executing_layer,
        )
        current_identity = (
            current.authorization_id,
            current.workflow_id,
            current.revision_request_id,
            current.executing_layer,
        )
        if current_identity != expected_identity:
            raise ValueError("Revision authorization identity changed")

        claim = {
            "authorization_id": authorization.authorization_id,
            "provider_reservation_ids": list(provider_reservation_ids),
            "retrieval_reservation_ids": list(retrieval_reservation_ids),
            "claimed_at": utc_now().isoformat(),
        }
        claim_path = path.with_suffix(".consume.json")
        self._write_json_once_or_exact(
            claim_path,
            claim,
            "Revision authorization was consumed with different reservations",
            ignore_keys={"claimed_at"},
        )
        persisted_claim = self.read_json(claim_path)
        if current.status == RevisionAuthorizationStatus.CONSUMED.value:
            if (
                current.provider_reservation_ids != provider_reservation_ids
                or current.retrieval_reservation_ids != retrieval_reservation_ids
            ):
                raise ValueError("Revision authorization was already consumed differently")
            return current

        consumed_payload = current.model_dump(mode="json")
        consumed_payload.update(
            {
                "status": RevisionAuthorizationStatus.CONSUMED.value,
                "consumed_at": persisted_claim["claimed_at"],
                "provider_reservation_ids": list(provider_reservation_ids),
                "retrieval_reservation_ids": list(retrieval_reservation_ids),
            }
        )
        consumed = RevisionExecutionAuthorization.model_validate(consumed_payload)
        self.write_json_atomic(path, consumed.model_dump(mode="json"))
        return consumed

    def create_audit_event_once(self, event: RevisionAuditEvent) -> Path:
        path = (
            self.audit_root
            / safe_path_component(event.workflow_id)
            / safe_path_component(event.revision_request_id)
            / f"{safe_path_component(event.audit_event_id)}.json"
        )
        self._write_json_once_or_exact(
            path,
            event.model_dump(mode="json"),
            "Revision audit event identity conflict",
        )
        return path

    def list_audit_events(
        self,
        *,
        workflow_id: str,
        revision_request_id: str,
    ) -> list[RevisionAuditEvent]:
        directory = (
            self.audit_root
            / safe_path_component(workflow_id)
            / safe_path_component(revision_request_id)
        )
        if not directory.exists():
            return []
        return [
            RevisionAuditEvent.model_validate(self.read_json(path))
            for path in sorted(directory.glob("*.json"))
        ]

    def _request_path(
        self,
        target_layer: LayerId | str,
        workflow_id: str,
        revision_request_id: str,
    ) -> Path:
        return (
            self.request_root
            / LayerId(target_layer).value
            / safe_path_component(workflow_id)
            / f"{safe_path_component(revision_request_id)}.json"
        )

    def _result_path(
        self,
        requester_layer: LayerId | str,
        workflow_id: str,
        revision_request_id: str,
    ) -> Path:
        return (
            self.result_root
            / LayerId(requester_layer).value
            / safe_path_component(workflow_id)
            / f"{safe_path_component(revision_request_id)}.json"
        )

    def _authorization_path(
        self,
        authorization: RevisionExecutionAuthorization,
    ) -> Path:
        return self._authorization_path_values(
            authorization.executing_layer,
            authorization.workflow_id,
            authorization.revision_request_id,
        )

    def _authorization_path_values(
        self,
        executing_layer: LayerId | str,
        workflow_id: str,
        revision_request_id: str,
    ) -> Path:
        return (
            self.authorization_root
            / LayerId(executing_layer).value
            / safe_path_component(workflow_id)
            / f"{safe_path_component(revision_request_id)}.json"
        )

    def _write_json_once_or_exact(
        self,
        path: Path,
        payload: dict[str, Any],
        conflict_message: str,
        *,
        ignore_keys: set[str] | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            return
        except FileExistsError:
            existing = self.read_json(path)
        if ignore_keys:
            expected_hash = canonical_sha256(
                {key: value for key, value in payload.items() if key not in ignore_keys}
            )
            existing_hash = canonical_sha256(
                {key: value for key, value in existing.items() if key not in ignore_keys}
            )
            if existing_hash == expected_hash:
                return
        elif existing == payload:
            return
        raise ValueError(conflict_message)
