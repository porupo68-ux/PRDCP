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


RETRIEVAL_RECONSTRUCTION_SUFFIX = "_retrieval_reconstruction_1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RetrievalReconstructionStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class RetrievalReconstructionAuthorization(BaseModel):
    """Durable identity for one operator-authorized replacement Retrieval call."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    authorization_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    failed_provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    retrieval_provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    retrieval_model_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    original_task_id: str = Field(min_length=1)
    reconstruction_task_id: str = Field(min_length=1)
    source_error_message_id: str = Field(min_length=1)
    source_error_class: str = Field(min_length=1)
    source_http_status: int = Field(ge=400, le=599)
    failure_signature: str = Field(pattern=r"^runtime_model_drift_retrieval_missing$")
    failed_model_id: str = Field(min_length=1)
    runtime_model_id: str = Field(min_length=1)
    research_question_id: str | None
    retrieval_strategy: str = Field(min_length=1)
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_results: int = Field(ge=1)
    retrieval_id: str = Field(pattern=r"^retrieval_[0-9a-f]{24}$")
    authorized_by: str = Field(min_length=1)
    status: RetrievalReconstructionStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    reservation_path: str | None = None
    retrieval_context_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    context_saved_at: datetime | None = None


class RetrievalReconstructionAuthorizationStore:
    """One-shot authorization store for missing Researcher Retrieval Contexts.

    The authorization is consumed after the Retrieval reservation is durable and
    before the provider is invoked.  A consumed authorization without a saved
    context is deliberately ambiguous and cannot be retried automatically.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "retrieval_reconstruction_authorizations"

    def authorize_once(
        self,
        *,
        workflow_id: str,
        failed_provider_id: str,
        retrieval_provider_id: str,
        retrieval_model_id: str,
        agent_id: str,
        original_task_id: str,
        source_error_message_id: str,
        source_error_class: str,
        source_http_status: int,
        failed_model_id: str,
        runtime_model_id: str,
        research_question_id: str | None,
        retrieval_strategy: str,
        query_sha256: str,
        max_results: int,
        retrieval_id: str,
        authorized_by: str = "cli.operator",
    ) -> RetrievalReconstructionAuthorization:
        self._validate_component(failed_provider_id, "failed_provider_id")
        self._validate_component(retrieval_provider_id, "retrieval_provider_id")
        if original_task_id.endswith(RETRIEVAL_RECONSTRUCTION_SUFFIX):
            raise ValueError("Retrieval reconstruction requires the original task ID")
        if source_http_status != 404:
            raise ValueError("Retrieval reconstruction requires the original HTTP 404")
        if source_error_class not in {
            "ProviderCapabilityError",
            "NonRetryableAgentError",
        }:
            raise ValueError("Retrieval reconstruction requires a capability failure")
        if failed_model_id == runtime_model_id:
            raise ValueError("Retrieval reconstruction requires a changed runtime model")
        if not re.fullmatch(r"[0-9a-f]{64}", query_sha256):
            raise ValueError("Retrieval reconstruction requires a query SHA-256")

        original_reservation = self._provider_reservation_path(
            failed_provider_id,
            workflow_id,
            original_task_id,
        )
        if not original_reservation.exists():
            raise ValueError(
                "Retrieval reconstruction requires the original provider reservation"
            )
        reservation = self._read_json(original_reservation)
        if (
            reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != original_task_id
            or reservation.get("agent_id") != agent_id
            or reservation.get("model_id") != failed_model_id
        ):
            raise ValueError("Original provider reservation does not match the failed task")

        reconstruction_task_id = (
            f"{original_task_id}{RETRIEVAL_RECONSTRUCTION_SUFFIX}"
        )
        identity = {
            "workflow_id": workflow_id,
            "failed_provider_id": failed_provider_id,
            "retrieval_provider_id": retrieval_provider_id,
            "retrieval_model_id": retrieval_model_id,
            "agent_id": agent_id,
            "original_task_id": original_task_id,
            "reconstruction_task_id": reconstruction_task_id,
            "source_error_message_id": source_error_message_id,
            "source_error_class": source_error_class,
            "source_http_status": source_http_status,
            "failed_model_id": failed_model_id,
            "runtime_model_id": runtime_model_id,
            "research_question_id": research_question_id,
            "retrieval_strategy": retrieval_strategy,
            "query_sha256": query_sha256,
            "max_results": max_results,
            "retrieval_id": retrieval_id,
        }
        path = self._authorization_path(
            retrieval_provider_id,
            workflow_id,
            original_task_id,
        )
        if path.exists():
            existing = self._load(path)
            self._validate_identity(existing, **identity)
            return existing

        authorization = RetrievalReconstructionAuthorization(
            schema_version="1.0",
            authorization_id=str(uuid4()),
            failure_signature="runtime_model_drift_retrieval_missing",
            authorized_by=authorized_by,
            status=RetrievalReconstructionStatus.PENDING,
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
    ) -> RetrievalReconstructionAuthorization | None:
        path = self._authorization_path(
            retrieval_provider_id,
            workflow_id,
            original_task_id,
        )
        return self._load(path) if path.exists() else None

    def consume(
        self,
        authorization: RetrievalReconstructionAuthorization,
        *,
        reservation_path: Path,
    ) -> RetrievalReconstructionAuthorization:
        if authorization.status != RetrievalReconstructionStatus.PENDING.value:
            raise ValueError("Retrieval reconstruction authorization is not pending")
        if not reservation_path.exists():
            raise ValueError("Retrieval reservation must exist before authorization use")
        reservation = self._read_json(reservation_path)
        if (
            reservation.get("retrieval_id") != authorization.retrieval_id
            or reservation.get("workflow_id") != authorization.workflow_id
            or reservation.get("task_id") != authorization.reconstruction_task_id
            or reservation.get("agent_id") != authorization.agent_id
            or reservation.get("strategy") != authorization.retrieval_strategy
            or reservation.get("provider_id")
            != authorization.retrieval_provider_id
        ):
            raise ValueError("Retrieval reservation does not match the authorization")
        updated = authorization.model_copy(
            update={
                "status": RetrievalReconstructionStatus.CONSUMED.value,
                "consumed_at": utc_now(),
                "reservation_path": str(reservation_path),
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
        self,
        authorization: RetrievalReconstructionAuthorization,
    ) -> RetrievalReconstructionAuthorization:
        current = self.for_original_task(
            workflow_id=authorization.workflow_id,
            retrieval_provider_id=authorization.retrieval_provider_id,
            original_task_id=authorization.original_task_id,
        )
        if current is None or current.status != RetrievalReconstructionStatus.CONSUMED.value:
            raise ValueError("Consumed Retrieval authorization is required")
        context_path = self.context_path(
            workflow_id=current.workflow_id,
            retrieval_id=current.retrieval_id,
        )
        if not context_path.exists():
            raise ValueError("Reconstructed Retrieval Context is missing")
        context = self._read_json(context_path)
        if (
            context.get("retrieval_id") != current.retrieval_id
            or context.get("workflow_id") != current.workflow_id
            or context.get("task_id") != current.reconstruction_task_id
            or context.get("research_question_id") != current.research_question_id
            or context.get("agent_id") != current.agent_id
            or context.get("retrieval_strategy") != current.retrieval_strategy
        ):
            raise ValueError("Reconstructed Retrieval Context identity mismatch")
        digest = hashlib.sha256(context_path.read_bytes()).hexdigest()
        if (
            current.retrieval_context_sha256 is not None
            and current.retrieval_context_sha256 != digest
        ):
            raise ValueError("Reconstructed Retrieval Context hash changed")
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

    def context_path(self, *, workflow_id: str, retrieval_id: str) -> Path:
        return (
            self.data_dir
            / "retrieval_contexts"
            / self._path_component(workflow_id)
            / f"{self._path_component(retrieval_id)}.json"
        )

    def _provider_reservation_path(
        self,
        provider_id: str,
        workflow_id: str,
        task_id: str,
    ) -> Path:
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
    def _validate_identity(
        authorization: RetrievalReconstructionAuthorization,
        **expected: object,
    ) -> None:
        for field, value in expected.items():
            if getattr(authorization, field) != value:
                raise ValueError(
                    "Existing Retrieval reconstruction has a different identity"
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

    def _load(self, path: Path) -> RetrievalReconstructionAuthorization:
        return RetrievalReconstructionAuthorization.model_validate(
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
