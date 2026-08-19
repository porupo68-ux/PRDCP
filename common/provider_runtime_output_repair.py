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

from common.provider_runtime_model_repair import (
    RUNTIME_MODEL_REPAIR_SUFFIX,
    RuntimeModelRepairAuthorizationStore,
    RuntimeModelRepairStatus,
)


RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX = "_runtime_model_output_repair_1"
RETRIEVAL_EXCERPT_HYDRATION_FAILURE = "retrieval_excerpt_hydration_contract"
RUNTIME_ADAPTER_REPAIR_SUFFIX = "_runtime_adapter_repair_1"
SOURCE_IDENTITY_CANONICALIZATION_FAILURE = (
    "research_source_identity_canonicalization_contract"
)
RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX = "_runtime_identity_repair_1"
SOURCE_REDUNDANT_IDENTITY_HYDRATION_FAILURE = (
    "research_source_redundant_identity_hydration_contract"
)
RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX = "_runtime_provenance_repair_1"
SOURCE_PROVENANCE_OWNERSHIP_FAILURE = "research_source_provenance_ownership_contract"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeModelOutputRepairStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class RuntimeModelOutputRepairAuthorization(BaseModel):
    """One paid output-contract repair after a consumed runtime-model repair."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    authorization_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    agent_id: str = Field(min_length=1)
    original_task_id: str = Field(min_length=1)
    runtime_repair_task_id: str = Field(min_length=1)
    output_repair_task_id: str = Field(min_length=1)
    source_runtime_authorization_id: str = Field(min_length=1)
    source_error_message_id: str = Field(min_length=1)
    source_error_class: str = Field(pattern=r"^NonRetryableAgentError$")
    failure_signature: str = Field(
        pattern=r"^retrieval_excerpt_hydration_contract$"
    )
    model_id: str = Field(min_length=1)
    retrieval_id: str = Field(min_length=1)
    retrieval_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_by: str = Field(min_length=1)
    status: RuntimeModelOutputRepairStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    reservation_path: str | None = None


class RuntimeModelOutputRepairAuthorizationStore:
    """Persist a single hash-bound repair without reopening the consumed task."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "provider_runtime_output_repair_authorizations"

    @classmethod
    def from_reservation_root(
        cls,
        reservation_root: Path,
    ) -> "RuntimeModelOutputRepairAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError(
                "Runtime output repair requires the canonical reservation root"
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
        failure_signature: str = RETRIEVAL_EXCERPT_HYDRATION_FAILURE,
        authorized_by: str = "cli.operator",
    ) -> RuntimeModelOutputRepairAuthorization:
        self._validate_component(provider_id, "provider_id")
        if original_task_id.endswith(
            (RUNTIME_MODEL_REPAIR_SUFFIX, RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX)
        ):
            raise ValueError("Runtime output repair requires the original task ID")
        if source_error_class != "NonRetryableAgentError":
            raise ValueError(
                "Runtime output repair requires a deterministic non-retryable failure"
            )
        if failure_signature != RETRIEVAL_EXCERPT_HYDRATION_FAILURE:
            raise ValueError("Runtime output repair failure signature is not supported")
        if not re.fullmatch(r"[0-9a-f]{64}", retrieval_context_sha256):
            raise ValueError("Retrieval Context SHA-256 is invalid")

        runtime_store = RuntimeModelRepairAuthorizationStore(self.data_dir)
        runtime_authorization = runtime_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        runtime_repair_task_id = f"{original_task_id}{RUNTIME_MODEL_REPAIR_SUFFIX}"
        if (
            runtime_authorization is None
            or runtime_authorization.status != RuntimeModelRepairStatus.CONSUMED.value
            or runtime_authorization.agent_id != agent_id
            or runtime_authorization.repair_task_id != runtime_repair_task_id
            or runtime_authorization.runtime_model_id != model_id
            or runtime_authorization.retrieval_id != retrieval_id
            or runtime_authorization.retrieval_context_sha256
            != retrieval_context_sha256
        ):
            raise ValueError(
                "Runtime output repair requires the matching consumed runtime repair"
            )

        runtime_reservation = self._reservation_path(
            provider_id,
            workflow_id,
            runtime_repair_task_id,
        )
        if not runtime_reservation.exists():
            raise ValueError(
                "Runtime output repair requires the runtime repair reservation"
            )
        reservation = self._read_json(runtime_reservation)
        if (
            reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != runtime_repair_task_id
            or reservation.get("agent_id") != agent_id
            or reservation.get("model_id") != model_id
        ):
            raise ValueError(
                "Runtime repair reservation does not match the failed invocation"
            )
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
            "model_id": model_id,
            "retrieval_id": retrieval_id,
            "retrieval_context_sha256": retrieval_context_sha256,
            "failure_signature": failure_signature,
        }
        if path.exists():
            existing = self._load(path)
            self._validate_identity(existing, **identity)
            if existing.status == RuntimeModelOutputRepairStatus.PENDING.value:
                return existing
            raise ValueError("The one-time runtime output repair was already consumed")

        authorization = RuntimeModelOutputRepairAuthorization(
            schema_version="1.0",
            authorization_id=str(uuid4()),
            runtime_repair_task_id=runtime_repair_task_id,
            output_repair_task_id=(
                f"{original_task_id}{RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX}"
            ),
            source_runtime_authorization_id=runtime_authorization.authorization_id,
            authorized_by=authorized_by,
            status=RuntimeModelOutputRepairStatus.PENDING,
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
    ) -> RuntimeModelOutputRepairAuthorization | None:
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
    ) -> RuntimeModelOutputRepairAuthorization:
        original_task_id = self._original_task_id(repair_task_id)
        authorization = self.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Runtime output repair task has no persisted authorization")
        if (
            authorization.output_repair_task_id != repair_task_id
            or authorization.agent_id != agent_id
            or authorization.model_id != model_id
            or authorization.status != RuntimeModelOutputRepairStatus.PENDING.value
        ):
            raise ValueError(
                "Runtime output repair authorization is not pending for this task"
            )
        self._require_retrieval_hash(
            workflow_id=workflow_id,
            retrieval_id=authorization.retrieval_id,
            expected_sha256=authorization.retrieval_context_sha256,
        )
        return authorization

    def consume(
        self,
        authorization: RuntimeModelOutputRepairAuthorization,
        *,
        reservation_path: Path,
    ) -> RuntimeModelOutputRepairAuthorization:
        if authorization.status != RuntimeModelOutputRepairStatus.PENDING.value:
            raise ValueError("Runtime output repair authorization is not pending")
        if not reservation_path.exists():
            raise ValueError("Runtime output repair reservation must exist before use")
        updated = authorization.model_copy(
            update={
                "status": RuntimeModelOutputRepairStatus.CONSUMED.value,
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
        if not repair_task_id.endswith(RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX):
            raise ValueError("Task is not a runtime output repair task")
        return repair_task_id[: -len(RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX)]

    @staticmethod
    def _validate_identity(
        authorization: RuntimeModelOutputRepairAuthorization,
        **expected: str,
    ) -> None:
        for field, value in expected.items():
            if getattr(authorization, field) != value:
                raise ValueError(
                    "Existing runtime output repair has different identity"
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

    def _load(self, path: Path) -> RuntimeModelOutputRepairAuthorization:
        return RuntimeModelOutputRepairAuthorization.model_validate(
            self._read_json(path)
        )

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


class RuntimeAdapterRepairStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class RuntimeAdapterRepairAuthorization(BaseModel):
    """One successor repair for a proven deterministic adapter contract."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    authorization_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    agent_id: str = Field(min_length=1)
    original_task_id: str = Field(min_length=1)
    predecessor_task_id: str = Field(min_length=1)
    repair_task_id: str = Field(min_length=1)
    predecessor_authorization_id: str = Field(min_length=1)
    source_error_message_id: str = Field(min_length=1)
    source_error_class: str = Field(pattern=r"^NonRetryableAgentError$")
    failure_signature: str = Field(
        pattern=(
            r"^(research_source_identity_canonicalization_contract|"
            r"research_source_redundant_identity_hydration_contract|"
            r"research_source_provenance_ownership_contract)$"
        )
    )
    model_id: str = Field(min_length=1)
    retrieval_id: str = Field(min_length=1)
    retrieval_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_by: str = Field(min_length=1)
    status: RuntimeAdapterRepairStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    reservation_path: str | None = None


class RuntimeAdapterRepairAuthorizationStore(
    RuntimeModelOutputRepairAuthorizationStore
):
    """Chain one adapter repair to the consumed Cycle 034 invocation."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.root = self.data_dir / "provider_runtime_adapter_repair_authorizations"

    @classmethod
    def from_reservation_root(
        cls,
        reservation_root: Path,
    ) -> "RuntimeAdapterRepairAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError(
                "Runtime adapter repair requires the canonical reservation root"
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
        failure_signature: str = SOURCE_IDENTITY_CANONICALIZATION_FAILURE,
        authorized_by: str = "cli.operator",
    ) -> RuntimeAdapterRepairAuthorization:
        self._validate_component(provider_id, "provider_id")
        if original_task_id.endswith(
            (
                RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX,
                RUNTIME_ADAPTER_REPAIR_SUFFIX,
            )
        ):
            raise ValueError("Runtime adapter repair requires the original task ID")
        if source_error_class != "NonRetryableAgentError":
            raise ValueError(
                "Runtime adapter repair requires a deterministic non-retryable failure"
            )
        if failure_signature != SOURCE_IDENTITY_CANONICALIZATION_FAILURE:
            raise ValueError("Runtime adapter repair failure signature is not supported")

        predecessor_store = RuntimeModelOutputRepairAuthorizationStore(self.data_dir)
        predecessor = predecessor_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        predecessor_task_id = (
            f"{original_task_id}{RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX}"
        )
        if (
            predecessor is None
            or predecessor.status != RuntimeModelOutputRepairStatus.CONSUMED.value
            or predecessor.agent_id != agent_id
            or predecessor.output_repair_task_id != predecessor_task_id
            or predecessor.model_id != model_id
            or predecessor.retrieval_id != retrieval_id
            or predecessor.retrieval_context_sha256 != retrieval_context_sha256
        ):
            raise ValueError(
                "Runtime adapter repair requires the matching consumed output repair"
            )
        predecessor_reservation = self._reservation_path(
            provider_id,
            workflow_id,
            predecessor_task_id,
        )
        if not predecessor_reservation.exists():
            raise ValueError(
                "Runtime adapter repair requires the predecessor reservation"
            )
        reservation = self._read_json(predecessor_reservation)
        if (
            reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != predecessor_task_id
            or reservation.get("agent_id") != agent_id
            or reservation.get("model_id") != model_id
        ):
            raise ValueError("Predecessor reservation identity changed")
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
            "model_id": model_id,
            "retrieval_id": retrieval_id,
            "retrieval_context_sha256": retrieval_context_sha256,
            "failure_signature": failure_signature,
        }
        if path.exists():
            existing = self._load(path)
            self._validate_identity(existing, **identity)
            if existing.status == RuntimeAdapterRepairStatus.PENDING.value:
                return existing
            raise ValueError("The one-time runtime adapter repair was already consumed")

        authorization = RuntimeAdapterRepairAuthorization(
            schema_version="1.0",
            authorization_id=str(uuid4()),
            predecessor_task_id=predecessor_task_id,
            repair_task_id=f"{original_task_id}{RUNTIME_ADAPTER_REPAIR_SUFFIX}",
            predecessor_authorization_id=predecessor.authorization_id,
            authorized_by=authorized_by,
            status=RuntimeAdapterRepairStatus.PENDING,
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
    ) -> RuntimeAdapterRepairAuthorization | None:
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
    ) -> RuntimeAdapterRepairAuthorization:
        original_task_id = self._original_task_id(repair_task_id)
        authorization = self.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Runtime adapter repair has no persisted authorization")
        if (
            authorization.repair_task_id != repair_task_id
            or authorization.agent_id != agent_id
            or authorization.model_id != model_id
            or authorization.status != RuntimeAdapterRepairStatus.PENDING.value
        ):
            raise ValueError(
                "Runtime adapter repair authorization is not pending for this task"
            )
        self._require_retrieval_hash(
            workflow_id=workflow_id,
            retrieval_id=authorization.retrieval_id,
            expected_sha256=authorization.retrieval_context_sha256,
        )
        return authorization

    def consume(
        self,
        authorization: RuntimeAdapterRepairAuthorization,
        *,
        reservation_path: Path,
    ) -> RuntimeAdapterRepairAuthorization:
        if authorization.status != RuntimeAdapterRepairStatus.PENDING.value:
            raise ValueError("Runtime adapter repair authorization is not pending")
        if not reservation_path.exists():
            raise ValueError("Runtime adapter repair reservation must exist before use")
        updated = authorization.model_copy(
            update={
                "status": RuntimeAdapterRepairStatus.CONSUMED.value,
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

    @staticmethod
    def _original_task_id(repair_task_id: str) -> str:
        if not repair_task_id.endswith(RUNTIME_ADAPTER_REPAIR_SUFFIX):
            raise ValueError("Task is not a runtime adapter repair task")
        return repair_task_id[: -len(RUNTIME_ADAPTER_REPAIR_SUFFIX)]

    def _load(self, path: Path) -> RuntimeAdapterRepairAuthorization:
        return RuntimeAdapterRepairAuthorization.model_validate(
            self._read_json(path)
        )


class RuntimeIdentityHydrationRepairAuthorizationStore(
    RuntimeAdapterRepairAuthorizationStore
):
    """Chain one redundant-identity hydration repair to consumed Cycle 035."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.root = self.data_dir / "provider_runtime_identity_repair_authorizations"

    @classmethod
    def from_reservation_root(
        cls,
        reservation_root: Path,
    ) -> "RuntimeIdentityHydrationRepairAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError(
                "Runtime identity repair requires the canonical reservation root"
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
        failure_signature: str = SOURCE_REDUNDANT_IDENTITY_HYDRATION_FAILURE,
        authorized_by: str = "cli.operator",
    ) -> RuntimeAdapterRepairAuthorization:
        self._validate_component(provider_id, "provider_id")
        if original_task_id.endswith(
            (
                RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX,
                RUNTIME_ADAPTER_REPAIR_SUFFIX,
                RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX,
            )
        ):
            raise ValueError("Runtime identity repair requires the original task ID")
        if source_error_class != "NonRetryableAgentError":
            raise ValueError(
                "Runtime identity repair requires a deterministic non-retryable failure"
            )
        if failure_signature != SOURCE_REDUNDANT_IDENTITY_HYDRATION_FAILURE:
            raise ValueError("Runtime identity repair failure signature is not supported")

        predecessor_store = RuntimeAdapterRepairAuthorizationStore(self.data_dir)
        predecessor = predecessor_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        predecessor_task_id = f"{original_task_id}{RUNTIME_ADAPTER_REPAIR_SUFFIX}"
        if (
            predecessor is None
            or predecessor.status != RuntimeAdapterRepairStatus.CONSUMED.value
            or predecessor.agent_id != agent_id
            or predecessor.repair_task_id != predecessor_task_id
            or predecessor.model_id != model_id
            or predecessor.retrieval_id != retrieval_id
            or predecessor.retrieval_context_sha256 != retrieval_context_sha256
        ):
            raise ValueError(
                "Runtime identity repair requires the matching consumed adapter repair"
            )
        predecessor_reservation = self._reservation_path(
            provider_id,
            workflow_id,
            predecessor_task_id,
        )
        if not predecessor_reservation.exists():
            raise ValueError(
                "Runtime identity repair requires the predecessor reservation"
            )
        reservation = self._read_json(predecessor_reservation)
        if (
            reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != predecessor_task_id
            or reservation.get("agent_id") != agent_id
            or reservation.get("model_id") != model_id
        ):
            raise ValueError("Predecessor reservation identity changed")
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
            "model_id": model_id,
            "retrieval_id": retrieval_id,
            "retrieval_context_sha256": retrieval_context_sha256,
            "failure_signature": failure_signature,
        }
        if path.exists():
            existing = self._load(path)
            self._validate_identity(existing, **identity)
            if existing.status == RuntimeAdapterRepairStatus.PENDING.value:
                return existing
            raise ValueError("The one-time runtime identity repair was already consumed")

        authorization = RuntimeAdapterRepairAuthorization(
            schema_version="1.0",
            authorization_id=str(uuid4()),
            predecessor_task_id=predecessor_task_id,
            repair_task_id=(
                f"{original_task_id}{RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX}"
            ),
            predecessor_authorization_id=predecessor.authorization_id,
            authorized_by=authorized_by,
            status=RuntimeAdapterRepairStatus.PENDING,
            authorized_at=utc_now(),
            **identity,
        )
        self._write_exclusive(path, authorization.model_dump(mode="json"))
        return authorization

    @staticmethod
    def _original_task_id(repair_task_id: str) -> str:
        if not repair_task_id.endswith(RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX):
            raise ValueError("Task is not a runtime identity repair task")
        return repair_task_id[: -len(RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX)]


class RuntimeProvenanceHydrationRepairAuthorizationStore(
    RuntimeIdentityHydrationRepairAuthorizationStore
):
    """Chain one provenance-ownership repair to the consumed Cycle 036 call."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.root = self.data_dir / "provider_runtime_provenance_repair_authorizations"

    @classmethod
    def from_reservation_root(
        cls,
        reservation_root: Path,
    ) -> "RuntimeProvenanceHydrationRepairAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError(
                "Runtime provenance repair requires the canonical reservation root"
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
        failure_signature: str = SOURCE_PROVENANCE_OWNERSHIP_FAILURE,
        authorized_by: str = "cli.operator",
    ) -> RuntimeAdapterRepairAuthorization:
        self._validate_component(provider_id, "provider_id")
        if original_task_id.endswith(
            (
                RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX,
                RUNTIME_ADAPTER_REPAIR_SUFFIX,
                RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX,
                RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX,
            )
        ):
            raise ValueError("Runtime provenance repair requires the original task ID")
        if source_error_class != "NonRetryableAgentError":
            raise ValueError(
                "Runtime provenance repair requires a deterministic non-retryable failure"
            )
        if failure_signature != SOURCE_PROVENANCE_OWNERSHIP_FAILURE:
            raise ValueError("Runtime provenance repair failure signature is not supported")

        predecessor_store = RuntimeIdentityHydrationRepairAuthorizationStore(self.data_dir)
        predecessor = predecessor_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        predecessor_task_id = (
            f"{original_task_id}{RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX}"
        )
        if (
            predecessor is None
            or predecessor.status != RuntimeAdapterRepairStatus.CONSUMED.value
            or predecessor.agent_id != agent_id
            or predecessor.repair_task_id != predecessor_task_id
            or predecessor.model_id != model_id
            or predecessor.retrieval_id != retrieval_id
            or predecessor.retrieval_context_sha256 != retrieval_context_sha256
        ):
            raise ValueError(
                "Runtime provenance repair requires the matching consumed identity repair"
            )
        predecessor_reservation = self._reservation_path(
            provider_id,
            workflow_id,
            predecessor_task_id,
        )
        if not predecessor_reservation.exists():
            raise ValueError(
                "Runtime provenance repair requires the predecessor reservation"
            )
        reservation = self._read_json(predecessor_reservation)
        if (
            reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != predecessor_task_id
            or reservation.get("agent_id") != agent_id
            or reservation.get("model_id") != model_id
        ):
            raise ValueError("Predecessor reservation identity changed")
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
            "model_id": model_id,
            "retrieval_id": retrieval_id,
            "retrieval_context_sha256": retrieval_context_sha256,
            "failure_signature": failure_signature,
        }
        if path.exists():
            existing = self._load(path)
            self._validate_identity(existing, **identity)
            if existing.status == RuntimeAdapterRepairStatus.PENDING.value:
                return existing
            raise ValueError("The one-time runtime provenance repair was already consumed")

        authorization = RuntimeAdapterRepairAuthorization(
            schema_version="1.0",
            authorization_id=str(uuid4()),
            predecessor_task_id=predecessor_task_id,
            repair_task_id=(
                f"{original_task_id}{RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX}"
            ),
            predecessor_authorization_id=predecessor.authorization_id,
            authorized_by=authorized_by,
            status=RuntimeAdapterRepairStatus.PENDING,
            authorized_at=utc_now(),
            **identity,
        )
        self._write_exclusive(path, authorization.model_dump(mode="json"))
        return authorization

    @staticmethod
    def _original_task_id(repair_task_id: str) -> str:
        if not repair_task_id.endswith(RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX):
            raise ValueError("Task is not a runtime provenance repair task")
        return repair_task_id[: -len(RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX)]
