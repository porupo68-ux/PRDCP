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


RETRIEVAL_PROVIDER_RETRY_SUFFIX = "_retrieval_provider_retry_1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RetrievalProviderRetryStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class RetrievalProviderRetryAuthorization(BaseModel):
    """One replacement search after a correlated terminal Batch failure."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    authorization_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    retrieval_provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    agent_id: str = Field(min_length=1)
    original_task_id: str = Field(min_length=1)
    retry_task_id: str = Field(min_length=1)
    original_retrieval_id: str = Field(pattern=r"^retrieval_[0-9a-f]{24}$")
    retry_retrieval_id: str = Field(pattern=r"^retrieval_[0-9a-f]{24}$")
    source_error_message_id: str = Field(min_length=1)
    source_error_class: str = Field(pattern=r"^NonRetryableAgentError$")
    failure_signature: str = Field(pattern=r"^openrouter_batch_terminal_retrieval_failure$")
    failed_model_id: str = Field(min_length=1)
    runtime_model_id: str = Field(min_length=1)
    retrieval_strategy: str = Field(min_length=1)
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_results: int = Field(ge=1)
    original_reservation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_sidecar_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_by: str = Field(min_length=1)
    status: RetrievalProviderRetryStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    retry_reservation_path: str | None = None
    retrieval_context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_saved_at: datetime | None = None


class RetrievalProviderRetryAuthorizationStore:
    """Fail-closed authorization for one synchronous replacement Retrieval call."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "retrieval_provider_retry_authorizations"

    def authorize_once(
        self,
        *,
        workflow_id: str,
        retrieval_provider_id: str,
        agent_id: str,
        original_task_id: str,
        original_retrieval_id: str,
        retry_retrieval_id: str,
        source_error_message_id: str,
        source_error_class: str,
        failed_model_id: str,
        runtime_model_id: str,
        retrieval_strategy: str,
        query_sha256: str,
        max_results: int,
        authorized_by: str = "cli.operator",
    ) -> RetrievalProviderRetryAuthorization:
        self._validate_component(retrieval_provider_id, "retrieval_provider_id")
        if original_task_id.endswith(RETRIEVAL_PROVIDER_RETRY_SUFFIX):
            raise ValueError("A Retrieval retry cannot authorize another retry")
        if source_error_class != "NonRetryableAgentError":
            raise ValueError("Retrieval retry requires a non-retryable saved failure")
        if not failed_model_id.lower().endswith(":batch"):
            raise ValueError("Retrieval retry requires a failed Batch model")
        if runtime_model_id.lower().endswith(":batch") or runtime_model_id == failed_model_id:
            raise ValueError("Retrieval retry requires a changed synchronous runtime model")
        if original_retrieval_id == retry_retrieval_id:
            raise ValueError("Retrieval retry requires a new retrieval identity")
        if not re.fullmatch(r"[0-9a-f]{64}", query_sha256):
            raise ValueError("Retrieval retry requires a query SHA-256")

        original_reservation = self.reservation_path(
            provider_id=retrieval_provider_id,
            workflow_id=workflow_id,
            retrieval_id=original_retrieval_id,
        )
        if not original_reservation.exists():
            raise ValueError("Retrieval retry requires the original reservation")
        reservation = self._read_json(original_reservation)
        if (
            reservation.get("retrieval_id") != original_retrieval_id
            or reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != original_task_id
            or reservation.get("agent_id") != agent_id
            or reservation.get("strategy") != retrieval_strategy
            or reservation.get("provider_id") != retrieval_provider_id
        ):
            raise ValueError("Original Retrieval reservation identity mismatch")

        sidecars = []
        sidecar_root = (
            self.data_dir
            / "openrouter_batch_jobs"
            / "retrieval_call_reservations"
            / retrieval_provider_id
            / self._path_component(workflow_id)
        )
        for path in sorted(sidecar_root.glob(f"{original_retrieval_id}.*.json")):
            payload = self._read_json(path)
            if (
                payload.get("model_id") == failed_model_id
                and isinstance(payload.get("batch_id"), str)
                and payload.get("status") in {"failed", "expired", "cancelled"}
            ):
                sidecars.append(path)
        if len(sidecars) != 1:
            raise ValueError(
                "Retrieval retry requires exactly one correlated terminal Batch sidecar"
            )
        sidecar = sidecars[0]
        retry_task_id = f"{original_task_id}{RETRIEVAL_PROVIDER_RETRY_SUFFIX}"
        identity = {
            "workflow_id": workflow_id,
            "retrieval_provider_id": retrieval_provider_id,
            "agent_id": agent_id,
            "original_task_id": original_task_id,
            "retry_task_id": retry_task_id,
            "original_retrieval_id": original_retrieval_id,
            "retry_retrieval_id": retry_retrieval_id,
            "source_error_message_id": source_error_message_id,
            "source_error_class": source_error_class,
            "failed_model_id": failed_model_id,
            "runtime_model_id": runtime_model_id,
            "retrieval_strategy": retrieval_strategy,
            "query_sha256": query_sha256,
            "max_results": max_results,
            "original_reservation_sha256": self._sha256(original_reservation),
            "failure_sidecar_sha256": self._sha256(sidecar),
        }
        path = self._authorization_path(
            retrieval_provider_id, workflow_id, original_task_id
        )
        if path.exists():
            existing = self._load(path)
            for field, expected in identity.items():
                if getattr(existing, field) != expected:
                    raise ValueError("Existing Retrieval retry authorization identity changed")
            return existing

        authorization = RetrievalProviderRetryAuthorization(
            schema_version="1.0",
            authorization_id=str(uuid4()),
            failure_signature="openrouter_batch_terminal_retrieval_failure",
            authorized_by=authorized_by,
            status=RetrievalProviderRetryStatus.PENDING,
            authorized_at=utc_now(),
            **identity,
        )
        self._write_exclusive(path, authorization.model_dump(mode="json"))
        return authorization

    def for_original_task(
        self,
        *,
        workflow_id: str,
        retrieval_provider_id: str,
        original_task_id: str,
    ) -> RetrievalProviderRetryAuthorization | None:
        path = self._authorization_path(
            retrieval_provider_id, workflow_id, original_task_id
        )
        return self._load(path) if path.exists() else None

    def require_for_retry_task(
        self,
        *,
        workflow_id: str,
        retrieval_provider_id: str,
        agent_id: str,
        retry_task_id: str,
        runtime_model_id: str,
    ) -> RetrievalProviderRetryAuthorization:
        if not retry_task_id.endswith(RETRIEVAL_PROVIDER_RETRY_SUFFIX):
            raise ValueError("Task is not a Retrieval provider retry")
        original_task_id = retry_task_id[: -len(RETRIEVAL_PROVIDER_RETRY_SUFFIX)]
        authorization = self.for_original_task(
            workflow_id=workflow_id,
            retrieval_provider_id=retrieval_provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Retrieval provider retry has no saved authorization")
        if (
            authorization.retry_task_id != retry_task_id
            or authorization.agent_id != agent_id
            or authorization.runtime_model_id != runtime_model_id
        ):
            raise ValueError("Retrieval provider retry identity mismatch")
        return authorization

    def consume(
        self,
        authorization: RetrievalProviderRetryAuthorization,
        *,
        reservation_path: Path,
    ) -> RetrievalProviderRetryAuthorization:
        if authorization.status != RetrievalProviderRetryStatus.PENDING.value:
            raise ValueError("Retrieval provider retry authorization is not pending")
        if not reservation_path.exists():
            raise ValueError("Retry reservation must exist before authorization use")
        reservation = self._read_json(reservation_path)
        if (
            reservation.get("retrieval_id") != authorization.retry_retrieval_id
            or reservation.get("workflow_id") != authorization.workflow_id
            or reservation.get("task_id") != authorization.retry_task_id
            or reservation.get("agent_id") != authorization.agent_id
            or reservation.get("strategy") != authorization.retrieval_strategy
            or reservation.get("provider_id") != authorization.retrieval_provider_id
        ):
            raise ValueError("Retry Retrieval reservation identity mismatch")
        updated = authorization.model_copy(
            update={
                "status": RetrievalProviderRetryStatus.CONSUMED.value,
                "consumed_at": utc_now(),
                "retry_reservation_path": str(reservation_path),
            }
        )
        self._write_atomic(
            self._authorization_path(
                authorization.retrieval_provider_id,
                authorization.workflow_id,
                authorization.original_task_id,
            ),
            updated.model_dump(mode="json"),
        )
        return updated

    def record_context(
        self, authorization: RetrievalProviderRetryAuthorization
    ) -> RetrievalProviderRetryAuthorization:
        current = self.for_original_task(
            workflow_id=authorization.workflow_id,
            retrieval_provider_id=authorization.retrieval_provider_id,
            original_task_id=authorization.original_task_id,
        )
        if current is None or current.status != RetrievalProviderRetryStatus.CONSUMED.value:
            raise ValueError("Consumed Retrieval provider retry authorization is required")
        context_path = (
            self.data_dir
            / "retrieval_contexts"
            / self._path_component(current.workflow_id)
            / f"{current.retry_retrieval_id}.json"
        )
        if not context_path.exists():
            raise ValueError("Retry Retrieval Context is missing")
        context = self._read_json(context_path)
        if (
            context.get("retrieval_id") != current.retry_retrieval_id
            or context.get("workflow_id") != current.workflow_id
            or context.get("task_id") != current.retry_task_id
            or context.get("agent_id") != current.agent_id
            or context.get("retrieval_strategy") != current.retrieval_strategy
        ):
            raise ValueError("Retry Retrieval Context identity mismatch")
        digest = self._sha256(context_path)
        if current.retrieval_context_sha256 not in {None, digest}:
            raise ValueError("Retry Retrieval Context hash changed")
        if current.retrieval_context_sha256 == digest:
            return current
        updated = current.model_copy(
            update={
                "retrieval_context_sha256": digest,
                "context_saved_at": utc_now(),
            }
        )
        self._write_atomic(
            self._authorization_path(
                current.retrieval_provider_id,
                current.workflow_id,
                current.original_task_id,
            ),
            updated.model_dump(mode="json"),
        )
        return updated

    def reservation_path(
        self, *, provider_id: str, workflow_id: str, retrieval_id: str
    ) -> Path:
        return (
            self.data_dir
            / "retrieval_call_reservations"
            / provider_id
            / self._path_component(workflow_id)
            / f"{self._path_component(retrieval_id)}.json"
        )

    def _authorization_path(self, provider_id: str, workflow_id: str, task_id: str) -> Path:
        return (
            self.root
            / provider_id
            / self._path_component(workflow_id)
            / f"{self._path_component(task_id)}.json"
        )

    @staticmethod
    def _path_component(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            return value
        return "id-" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_component(value: str, field_name: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            raise ValueError(f"Invalid {field_name}")

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _load(self, path: Path) -> RetrievalProviderRetryAuthorization:
        return RetrievalProviderRetryAuthorization.model_validate(self._read_json(path))

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
