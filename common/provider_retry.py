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


OPERATOR_RETRY_SUFFIX = "_operator_retry_1"
OPERATOR_RETRYABLE_ERROR_CLASSES = {
    "RetryableAgentError",
    "ProviderResponseContractError",
    "PayloadValidationError",
    # A provider rejected the request schema before generation.  This label is
    # assigned only after a Manager correlates the persisted 400 failure with
    # its original reservation and a code-level compatibility repair exists.
    "ProviderRequestSchemaError",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProviderRetryStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class ProviderRetryAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    authorization_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    agent_id: str = Field(min_length=1)
    original_task_id: str = Field(min_length=1)
    retry_task_id: str = Field(min_length=1)
    source_error_message_id: str = Field(min_length=1)
    source_error_class: str = Field(min_length=1)
    authorized_by: str = Field(min_length=1)
    status: ProviderRetryStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    reservation_path: str | None = None


class ProviderRetryAuthorizationStore:
    """Persistent, one-shot operator authorization for a retryable provider task."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "provider_retry_authorizations"

    @classmethod
    def from_reservation_root(cls, reservation_root: Path) -> "ProviderRetryAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError(
                "Provider retry authorization requires the canonical reservation root"
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
        authorized_by: str = "cli.operator",
    ) -> ProviderRetryAuthorization:
        if source_error_class not in OPERATOR_RETRYABLE_ERROR_CLASSES:
            raise ValueError(
                "Operator provider retry is allowed only for RetryableAgentError "
                "or a persisted Provider request/output contract failure"
            )
        if original_task_id.endswith(OPERATOR_RETRY_SUFFIX):
            raise ValueError("An operator retry task cannot authorize another retry")
        self._validate_component(provider_id, "provider_id")
        original_reservation = self._reservation_path(
            provider_id,
            workflow_id,
            original_task_id,
        )
        if not original_reservation.exists():
            raise ValueError(
                "Operator provider retry requires the original provider reservation"
            )
        reservation = self._read_json(original_reservation)
        if (
            reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != original_task_id
            or reservation.get("agent_id") != agent_id
        ):
            raise ValueError("Original provider reservation does not match the failed task")

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
            )
            if existing.status == ProviderRetryStatus.PENDING.value:
                return existing
            raise ValueError("The one-time operator provider retry was already consumed")

        authorization = ProviderRetryAuthorization(
            authorization_id=str(uuid4()),
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=agent_id,
            original_task_id=original_task_id,
            retry_task_id=f"{original_task_id}{OPERATOR_RETRY_SUFFIX}",
            source_error_message_id=source_error_message_id,
            source_error_class=source_error_class,
            authorized_by=authorized_by,
            status=ProviderRetryStatus.PENDING,
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
    ) -> ProviderRetryAuthorization | None:
        path = self._authorization_path(provider_id, workflow_id, original_task_id)
        return self._load(path) if path.exists() else None

    def reservation_path(
        self,
        *,
        provider_id: str,
        workflow_id: str,
        task_id: str,
    ) -> Path:
        return self._reservation_path(provider_id, workflow_id, task_id)

    def require_pending_retry(
        self,
        *,
        workflow_id: str,
        provider_id: str,
        agent_id: str,
        retry_task_id: str,
    ) -> ProviderRetryAuthorization:
        original_task_id = self._original_task_id(retry_task_id)
        authorization = self.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Operator retry task has no persisted authorization")
        if (
            authorization.retry_task_id != retry_task_id
            or authorization.agent_id != agent_id
            or authorization.status != ProviderRetryStatus.PENDING.value
        ):
            raise ValueError("Operator retry authorization is not pending for this task")
        return authorization

    def consume(
        self,
        authorization: ProviderRetryAuthorization,
        *,
        reservation_path: Path,
    ) -> ProviderRetryAuthorization:
        if authorization.status != ProviderRetryStatus.PENDING.value:
            raise ValueError("Operator retry authorization is not pending")
        if not reservation_path.exists():
            raise ValueError("Operator retry reservation must exist before authorization use")
        updated = authorization.model_copy(
            update={
                "status": ProviderRetryStatus.CONSUMED.value,
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
    def _validate_identity(
        authorization: ProviderRetryAuthorization,
        *,
        workflow_id: str,
        provider_id: str,
        agent_id: str,
        original_task_id: str,
        source_error_message_id: str,
    ) -> None:
        expected = (
            workflow_id,
            provider_id,
            agent_id,
            original_task_id,
            source_error_message_id,
        )
        actual = (
            authorization.workflow_id,
            authorization.provider_id,
            authorization.agent_id,
            authorization.original_task_id,
            authorization.source_error_message_id,
        )
        if actual != expected:
            raise ValueError("Existing provider retry authorization has different identity")

    @staticmethod
    def _original_task_id(retry_task_id: str) -> str:
        if not retry_task_id.endswith(OPERATOR_RETRY_SUFFIX):
            raise ValueError("Task is not an operator retry task")
        return retry_task_id[: -len(OPERATOR_RETRY_SUFFIX)]

    @staticmethod
    def _validate_component(value: str, field_name: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            raise ValueError(f"Invalid {field_name}")

    @staticmethod
    def _path_component(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"id-{digest}"

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _load(self, path: Path) -> ProviderRetryAuthorization:
        return ProviderRetryAuthorization.model_validate(self._read_json(path))

    @staticmethod
    def _write_exclusive(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise

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
