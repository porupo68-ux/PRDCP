from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from common.provider_capability_repair import PROVIDER_CAPABILITY_REPAIR_SUFFIX
from common.provider_contract_repair import PROVIDER_CONTRACT_REPAIR_SUFFIX
from common.provider_output_repair import PROVIDER_OUTPUT_REPAIR_SUFFIX
from common.provider_retry import OPERATOR_RETRY_SUFFIX


RUNTIME_MODEL_REPAIR_SUFFIX = "_runtime_model_repair_1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeModelRepairStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class RuntimeModelRepairAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    authorization_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    agent_id: str = Field(min_length=1)
    original_task_id: str = Field(min_length=1)
    repair_task_id: str = Field(min_length=1)
    source_error_message_id: str = Field(min_length=1)
    source_error_class: str = Field(min_length=1)
    source_http_status: int = Field(ge=400, le=599)
    failure_signature: str = Field(pattern=r"^runtime_model_drift_endpoint_unavailable$")
    failed_model_id: str = Field(min_length=1)
    runtime_model_id: str = Field(min_length=1)
    capability_status: str = Field(pattern=r"^COMPATIBLE$")
    capability_reason: str = Field(min_length=1)
    retrieval_id: str = Field(min_length=1)
    retrieval_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_by: str = Field(min_length=1)
    status: RuntimeModelRepairStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    reservation_path: str | None = None


class RuntimeModelRepairAuthorizationStore:
    """Persistent, retrieval-bound authorization for one runtime model repair."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "provider_runtime_model_repair_authorizations"

    @classmethod
    def from_reservation_root(
        cls,
        reservation_root: Path,
    ) -> "RuntimeModelRepairAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError("Runtime model repair requires the canonical reservation root")
        return cls(root.parent)

    def authorize_once(
        self,
        *,
        workflow_id: str,
        provider_id: str,
        agent_id: str,
        original_task_id: str,
        source_error_message_id: str,
        source_error_class: str,
        source_http_status: int,
        failed_model_id: str,
        runtime_model_id: str,
        capability_status: str,
        capability_reason: str,
        retrieval_id: str,
        retrieval_context_sha256: str,
        authorized_by: str = "cli.operator",
    ) -> RuntimeModelRepairAuthorization:
        self._validate_component(provider_id, "provider_id")
        if original_task_id.endswith(
            (
                OPERATOR_RETRY_SUFFIX,
                PROVIDER_CONTRACT_REPAIR_SUFFIX,
                PROVIDER_CAPABILITY_REPAIR_SUFFIX,
                PROVIDER_OUTPUT_REPAIR_SUFFIX,
                RUNTIME_MODEL_REPAIR_SUFFIX,
            )
        ):
            raise ValueError("Runtime model repair requires the original task ID")
        if source_http_status != 404:
            raise ValueError("Runtime model repair requires the original HTTP 404")
        if source_error_class not in {
            "ProviderCapabilityError",
            "NonRetryableAgentError",
        }:
            raise ValueError("Runtime model repair requires a capability failure")
        if failed_model_id == runtime_model_id:
            raise ValueError("Runtime model repair requires a changed runtime model")
        if capability_status != "COMPATIBLE":
            raise ValueError("Runtime model repair requires COMPATIBLE model metadata")
        if not capability_reason:
            raise ValueError("Runtime model repair requires a capability reason")

        original_reservation = self._reservation_path(
            provider_id,
            workflow_id,
            original_task_id,
        )
        if not original_reservation.exists():
            raise ValueError("Runtime model repair requires the original provider reservation")
        reservation = self._read_json(original_reservation)
        if (
            reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != original_task_id
            or reservation.get("agent_id") != agent_id
            or reservation.get("model_id") != failed_model_id
        ):
            raise ValueError("Original provider reservation does not match the failed task")
        self._require_retrieval_hash(
            workflow_id=workflow_id,
            retrieval_id=retrieval_id,
            expected_sha256=retrieval_context_sha256,
        )

        path = self._authorization_path(provider_id, workflow_id, original_task_id)
        identity = {
            "workflow_id": workflow_id,
            "provider_id": provider_id,
            "agent_id": agent_id,
            "original_task_id": original_task_id,
            "source_error_message_id": source_error_message_id,
            "source_error_class": source_error_class,
            "failed_model_id": failed_model_id,
            "runtime_model_id": runtime_model_id,
            "retrieval_id": retrieval_id,
            "retrieval_context_sha256": retrieval_context_sha256,
        }
        if path.exists():
            existing = self._load(path)
            self._validate_identity(existing, **identity)
            if existing.status == RuntimeModelRepairStatus.PENDING.value:
                return existing
            raise ValueError("The one-time runtime model repair was already consumed")

        authorization = RuntimeModelRepairAuthorization(
            schema_version="1.0",
            authorization_id=str(uuid4()),
            repair_task_id=f"{original_task_id}{RUNTIME_MODEL_REPAIR_SUFFIX}",
            source_http_status=source_http_status,
            failure_signature="runtime_model_drift_endpoint_unavailable",
            capability_status=capability_status,
            capability_reason=capability_reason,
            authorized_by=authorized_by,
            status=RuntimeModelRepairStatus.PENDING,
            authorized_at=utc_now(),
            **identity,
        )
        self._write_exclusive(path, authorization.model_dump(mode="json"))
        return authorization

    def for_original_task(
        self,
        *,
        workflow_id: str,
        provider_id: str,
        original_task_id: str,
    ) -> RuntimeModelRepairAuthorization | None:
        path = self._authorization_path(provider_id, workflow_id, original_task_id)
        return self._load(path) if path.exists() else None

    def require_pending_repair(
        self,
        *,
        workflow_id: str,
        provider_id: str,
        agent_id: str,
        repair_task_id: str,
        runtime_model_id: str,
    ) -> RuntimeModelRepairAuthorization:
        original_task_id = self._original_task_id(repair_task_id)
        authorization = self.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Runtime model repair task has no persisted authorization")
        if (
            authorization.repair_task_id != repair_task_id
            or authorization.agent_id != agent_id
            or authorization.runtime_model_id != runtime_model_id
            or authorization.status != RuntimeModelRepairStatus.PENDING.value
        ):
            raise ValueError("Runtime model repair authorization is not pending for this task")
        self._require_retrieval_hash(
            workflow_id=workflow_id,
            retrieval_id=authorization.retrieval_id,
            expected_sha256=authorization.retrieval_context_sha256,
        )
        return authorization

    def consume(
        self,
        authorization: RuntimeModelRepairAuthorization,
        *,
        reservation_path: Path,
    ) -> RuntimeModelRepairAuthorization:
        if authorization.status != RuntimeModelRepairStatus.PENDING.value:
            raise ValueError("Runtime model repair authorization is not pending")
        if not reservation_path.exists():
            raise ValueError("Runtime model repair reservation must exist before consumption")
        updated = authorization.model_copy(
            update={
                "status": RuntimeModelRepairStatus.CONSUMED.value,
                "consumed_at": utc_now(),
                "reservation_path": str(reservation_path),
            }
        )
        self._write_atomic(
            self._authorization_path(
                authorization.provider_id,
                authorization.workflow_id,
                authorization.original_task_id,
            ),
            updated.model_dump(mode="json"),
        )
        return updated

    def reservation_path(
        self,
        *,
        provider_id: str,
        workflow_id: str,
        task_id: str,
    ) -> Path:
        return self._reservation_path(provider_id, workflow_id, task_id)

    def _require_retrieval_hash(
        self,
        *,
        workflow_id: str,
        retrieval_id: str,
        expected_sha256: str,
    ) -> None:
        path = self._retrieval_path(workflow_id, retrieval_id)
        if not path.exists():
            raise ValueError("Authorized saved Retrieval Context is missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError("Authorized saved Retrieval Context hash changed")

    def _retrieval_path(self, workflow_id: str, retrieval_id: str) -> Path:
        return (
            self.data_dir
            / "retrieval_contexts"
            / self._path_component(workflow_id)
            / f"{self._path_component(retrieval_id)}.json"
        )

    def _reservation_path(self, provider_id: str, workflow_id: str, task_id: str) -> Path:
        return (
            self.data_dir
            / "provider_call_reservations"
            / self._path_component(provider_id)
            / self._path_component(workflow_id)
            / f"{self._path_component(task_id)}.json"
        )

    def _authorization_path(
        self,
        provider_id: str,
        workflow_id: str,
        original_task_id: str,
    ) -> Path:
        return (
            self.root
            / self._path_component(provider_id)
            / self._path_component(workflow_id)
            / f"{self._path_component(original_task_id)}.json"
        )

    @staticmethod
    def _original_task_id(repair_task_id: str) -> str:
        if not repair_task_id.endswith(RUNTIME_MODEL_REPAIR_SUFFIX):
            raise ValueError("Task is not a runtime model repair task")
        return repair_task_id[: -len(RUNTIME_MODEL_REPAIR_SUFFIX)]

    @staticmethod
    def _validate_identity(
        authorization: RuntimeModelRepairAuthorization,
        **expected: str,
    ) -> None:
        for field, value in expected.items():
            if getattr(authorization, field) != value:
                raise ValueError("Existing runtime model repair has different identity")

    @staticmethod
    def _validate_component(value: str, field_name: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            raise ValueError(f"Invalid {field_name}")

    @staticmethod
    def _path_component(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            return value
        return "id-" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _load(self, path: Path) -> RuntimeModelRepairAuthorization:
        return RuntimeModelRepairAuthorization.model_validate(self._read_json(path))

    @staticmethod
    def _write_exclusive(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _write_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
