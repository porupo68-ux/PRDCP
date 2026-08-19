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

from common.provider_retry import (
    OPERATOR_RETRY_SUFFIX,
    ProviderRetryAuthorizationStore,
    ProviderRetryStatus,
)


PROVIDER_OUTPUT_REPAIR_SUFFIX = "_provider_output_repair_1"
RETRIEVAL_METADATA_HYDRATION_FAILURE = "retrieval_metadata_hydration_contract"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProviderOutputRepairStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class ProviderOutputRepairAuthorization(BaseModel):
    """One-shot authorization after a persisted output-adapter defect.

    This is intentionally distinct from an operator retry.  It may be issued
    only after that retry was consumed and the provider returned an output
    which a now-repaired deterministic hydration contract rejected.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    authorization_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    agent_id: str = Field(min_length=1)
    original_task_id: str = Field(min_length=1)
    retry_task_id: str = Field(min_length=1)
    repair_task_id: str = Field(min_length=1)
    source_retry_authorization_id: str = Field(min_length=1)
    source_error_message_id: str = Field(min_length=1)
    source_error_class: str = Field(pattern=r"^NonRetryableAgentError$")
    failure_signature: str = Field(
        pattern=r"^retrieval_metadata_hydration_contract$"
    )
    model_id: str = Field(min_length=1)
    retrieval_id: str = Field(min_length=1)
    retrieval_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_by: str = Field(min_length=1)
    status: ProviderOutputRepairStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    reservation_path: str | None = None


class ProviderOutputRepairAuthorizationStore:
    """Persistent authorization tied to the immutable saved Retrieval input."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "provider_output_repair_authorizations"

    @classmethod
    def from_reservation_root(
        cls,
        reservation_root: Path,
    ) -> "ProviderOutputRepairAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError(
                "Provider output repair requires the canonical reservation root"
            )
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
        model_id: str,
        retrieval_id: str,
        retrieval_context_sha256: str,
        failure_signature: str = RETRIEVAL_METADATA_HYDRATION_FAILURE,
        authorized_by: str = "cli.operator",
    ) -> ProviderOutputRepairAuthorization:
        self._validate_component(provider_id, "provider_id")
        if original_task_id.endswith(
            (OPERATOR_RETRY_SUFFIX, PROVIDER_OUTPUT_REPAIR_SUFFIX)
        ):
            raise ValueError("Provider output repair requires the original task ID")
        if source_error_class != "NonRetryableAgentError":
            raise ValueError(
                "Provider output repair requires a deterministic non-retryable failure"
            )
        if failure_signature != RETRIEVAL_METADATA_HYDRATION_FAILURE:
            raise ValueError("Provider output repair failure signature is not supported")
        if not re.fullmatch(r"[0-9a-f]{64}", retrieval_context_sha256):
            raise ValueError("Retrieval Context SHA-256 is invalid")

        retry_store = ProviderRetryAuthorizationStore(self.data_dir)
        retry_authorization = retry_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        retry_task_id = f"{original_task_id}{OPERATOR_RETRY_SUFFIX}"
        if (
            retry_authorization is None
            or retry_authorization.status != ProviderRetryStatus.CONSUMED.value
            or retry_authorization.agent_id != agent_id
            or retry_authorization.retry_task_id != retry_task_id
        ):
            raise ValueError(
                "Provider output repair requires a consumed one-shot retry authorization"
            )

        for task_id in (original_task_id, retry_task_id):
            reservation_path = self._reservation_path(
                provider_id,
                workflow_id,
                task_id,
            )
            if not reservation_path.exists():
                raise ValueError(
                    "Provider output repair requires both prior provider reservations"
                )
            reservation = self._read_json(reservation_path)
            if (
                reservation.get("workflow_id") != workflow_id
                or reservation.get("task_id") != task_id
                or reservation.get("agent_id") != agent_id
                or reservation.get("model_id") != model_id
            ):
                raise ValueError(
                    "Provider output repair reservation does not match the failed task"
                )

        self._require_retrieval_hash(
            workflow_id=workflow_id,
            retrieval_id=retrieval_id,
            expected_sha256=retrieval_context_sha256,
        )
        path = self._authorization_path(provider_id, workflow_id, original_task_id)
        if path.exists():
            existing = self._load(path)
            self._validate_identity(
                existing,
                workflow_id=workflow_id,
                provider_id=provider_id,
                agent_id=agent_id,
                original_task_id=original_task_id,
                source_error_message_id=source_error_message_id,
                source_error_class=source_error_class,
                model_id=model_id,
                retrieval_id=retrieval_id,
                retrieval_context_sha256=retrieval_context_sha256,
                failure_signature=failure_signature,
            )
            if existing.status == ProviderOutputRepairStatus.PENDING.value:
                return existing
            raise ValueError("The one-time provider output repair was already consumed")

        authorization = ProviderOutputRepairAuthorization(
            authorization_id=str(uuid4()),
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=agent_id,
            original_task_id=original_task_id,
            retry_task_id=retry_task_id,
            repair_task_id=f"{original_task_id}{PROVIDER_OUTPUT_REPAIR_SUFFIX}",
            source_retry_authorization_id=retry_authorization.authorization_id,
            source_error_message_id=source_error_message_id,
            source_error_class=source_error_class,
            failure_signature=failure_signature,
            model_id=model_id,
            retrieval_id=retrieval_id,
            retrieval_context_sha256=retrieval_context_sha256,
            authorized_by=authorized_by,
            status=ProviderOutputRepairStatus.PENDING,
            authorized_at=utc_now(),
        )
        self._write_exclusive(path, authorization.model_dump(mode="json"))
        return authorization

    def for_original_task(
        self,
        *,
        workflow_id: str,
        provider_id: str,
        original_task_id: str,
    ) -> ProviderOutputRepairAuthorization | None:
        path = self._authorization_path(provider_id, workflow_id, original_task_id)
        return self._load(path) if path.exists() else None

    def require_pending_repair(
        self,
        *,
        workflow_id: str,
        provider_id: str,
        agent_id: str,
        repair_task_id: str,
        model_id: str,
    ) -> ProviderOutputRepairAuthorization:
        original_task_id = self._original_task_id(repair_task_id)
        authorization = self.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Provider output repair task has no persisted authorization")
        if (
            authorization.repair_task_id != repair_task_id
            or authorization.agent_id != agent_id
            or authorization.model_id != model_id
            or authorization.status != ProviderOutputRepairStatus.PENDING.value
        ):
            raise ValueError(
                "Provider output repair authorization is not pending for this task and model"
            )
        self._require_retrieval_hash(
            workflow_id=workflow_id,
            retrieval_id=authorization.retrieval_id,
            expected_sha256=authorization.retrieval_context_sha256,
        )
        return authorization

    def consume(
        self,
        authorization: ProviderOutputRepairAuthorization,
        *,
        reservation_path: Path,
    ) -> ProviderOutputRepairAuthorization:
        if authorization.status != ProviderOutputRepairStatus.PENDING.value:
            raise ValueError("Provider output repair authorization is not pending")
        if not reservation_path.exists():
            raise ValueError(
                "Provider output repair reservation must exist before authorization use"
            )
        updated = authorization.model_copy(
            update={
                "status": ProviderOutputRepairStatus.CONSUMED.value,
                "consumed_at": utc_now(),
                "reservation_path": str(reservation_path),
            }
        )
        path = self._authorization_path(
            authorization.provider_id,
            authorization.workflow_id,
            authorization.original_task_id,
        )
        self._write_atomic(path, updated.model_dump(mode="json"))
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
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError("Authorized saved Retrieval Context hash changed")

    def _retrieval_path(self, workflow_id: str, retrieval_id: str) -> Path:
        return (
            self.data_dir
            / "retrieval_contexts"
            / self._path_component(workflow_id)
            / f"{self._path_component(retrieval_id)}.json"
        )

    def _reservation_path(
        self,
        provider_id: str,
        workflow_id: str,
        task_id: str,
    ) -> Path:
        return (
            self.data_dir
            / "provider_call_reservations"
            / provider_id
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
            / provider_id
            / self._path_component(workflow_id)
            / f"{self._path_component(original_task_id)}.json"
        )

    @staticmethod
    def _original_task_id(repair_task_id: str) -> str:
        if not repair_task_id.endswith(PROVIDER_OUTPUT_REPAIR_SUFFIX):
            raise ValueError("Task is not a provider output repair task")
        return repair_task_id[: -len(PROVIDER_OUTPUT_REPAIR_SUFFIX)]

    @staticmethod
    def _validate_identity(
        authorization: ProviderOutputRepairAuthorization,
        **expected: str,
    ) -> None:
        for field, value in expected.items():
            if getattr(authorization, field) != value:
                raise ValueError(
                    "Existing provider output repair authorization has different identity"
                )

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

    def _load(self, path: Path) -> ProviderOutputRepairAuthorization:
        return ProviderOutputRepairAuthorization.model_validate(self._read_json(path))

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
