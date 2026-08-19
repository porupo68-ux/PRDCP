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

from common.provider_contract_repair import PROVIDER_CONTRACT_REPAIR_SUFFIX
from common.provider_retry import OPERATOR_RETRY_SUFFIX


PROVIDER_CAPABILITY_REPAIR_SUFFIX = "_provider_capability_repair_1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProviderCapabilityRepairStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class ProviderCapabilityRepairAuthorization(BaseModel):
    """One-shot authorization to replace an incapable configured model."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    authorization_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    agent_id: str = Field(min_length=1)
    original_task_id: str = Field(min_length=1)
    repair_task_id: str = Field(min_length=1)
    source_error_message_id: str = Field(min_length=1)
    source_error_class: str = Field(min_length=1)
    source_http_status: int = Field(ge=400, le=599)
    failure_signature: str = Field(
        pattern=r"^structured_output_endpoint_unavailable$"
    )
    failed_model_id: str = Field(min_length=1)
    repair_model_id: str = Field(min_length=1)
    authorized_by: str = Field(min_length=1)
    status: ProviderCapabilityRepairStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    reservation_path: str | None = None


class ProviderCapabilityRepairAuthorizationStore:
    """Persistent one-shot authorization for a model capability mismatch."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "provider_capability_repair_authorizations"

    @classmethod
    def from_reservation_root(
        cls,
        reservation_root: Path,
    ) -> "ProviderCapabilityRepairAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError(
                "Provider capability repair requires the canonical reservation root"
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
        source_http_status: int,
        failed_model_id: str,
        repair_model_id: str,
        authorized_by: str = "cli.operator",
    ) -> ProviderCapabilityRepairAuthorization:
        self._validate_component(provider_id, "provider_id")
        if original_task_id.endswith(
            (
                OPERATOR_RETRY_SUFFIX,
                PROVIDER_CONTRACT_REPAIR_SUFFIX,
                PROVIDER_CAPABILITY_REPAIR_SUFFIX,
            )
        ):
            raise ValueError(
                "Provider capability repair requires the original task ID"
            )
        if source_http_status != 404:
            raise ValueError("Provider capability repair requires HTTP 404")
        if source_error_class not in {
            "ProviderCapabilityError",
            "NonRetryableAgentError",
        }:
            raise ValueError(
                "Provider capability repair requires a capability failure"
            )
        if not failed_model_id or not repair_model_id:
            raise ValueError("Provider capability repair requires both model IDs")
        if not re.fullmatch(
            r"~?[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*",
            repair_model_id,
        ):
            raise ValueError("Provider capability repair model ID is invalid")
        if failed_model_id == repair_model_id:
            raise ValueError("Provider capability repair must use a different model")

        reservation_path = self._reservation_path(
            provider_id,
            workflow_id,
            original_task_id,
        )
        if not reservation_path.exists():
            raise ValueError(
                "Provider capability repair requires the original provider reservation"
            )
        reservation = self._read_json(reservation_path)
        if (
            reservation.get("workflow_id") != workflow_id
            or reservation.get("task_id") != original_task_id
            or reservation.get("agent_id") != agent_id
            or reservation.get("model_id") != failed_model_id
        ):
            raise ValueError(
                "Provider capability repair reservation does not match the failed task"
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
                failed_model_id=failed_model_id,
                repair_model_id=repair_model_id,
            )
            if existing.status == ProviderCapabilityRepairStatus.PENDING.value:
                return existing
            raise ValueError("The one-time provider capability repair was already consumed")

        authorization = ProviderCapabilityRepairAuthorization(
            authorization_id=str(uuid4()),
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=agent_id,
            original_task_id=original_task_id,
            repair_task_id=(
                f"{original_task_id}{PROVIDER_CAPABILITY_REPAIR_SUFFIX}"
            ),
            source_error_message_id=source_error_message_id,
            source_error_class=source_error_class,
            source_http_status=source_http_status,
            failure_signature="structured_output_endpoint_unavailable",
            failed_model_id=failed_model_id,
            repair_model_id=repair_model_id,
            authorized_by=authorized_by,
            status=ProviderCapabilityRepairStatus.PENDING,
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
    ) -> ProviderCapabilityRepairAuthorization | None:
        path = self._authorization_path(provider_id, workflow_id, original_task_id)
        return self._load(path) if path.exists() else None

    def require_pending_repair(
        self,
        *,
        workflow_id: str,
        provider_id: str,
        agent_id: str,
        repair_task_id: str,
        repair_model_id: str,
    ) -> ProviderCapabilityRepairAuthorization:
        original_task_id = self._original_task_id(repair_task_id)
        authorization = self.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError(
                "Provider capability repair task has no persisted authorization"
            )
        if (
            authorization.repair_task_id != repair_task_id
            or authorization.agent_id != agent_id
            or authorization.repair_model_id != repair_model_id
            or authorization.status != ProviderCapabilityRepairStatus.PENDING.value
        ):
            raise ValueError(
                "Provider capability repair authorization is not pending for this task and model"
            )
        return authorization

    def consume(
        self,
        authorization: ProviderCapabilityRepairAuthorization,
        *,
        reservation_path: Path,
    ) -> ProviderCapabilityRepairAuthorization:
        if authorization.status != ProviderCapabilityRepairStatus.PENDING.value:
            raise ValueError("Provider capability repair authorization is not pending")
        if not reservation_path.exists():
            raise ValueError(
                "Provider capability repair reservation must exist before authorization use"
            )
        updated = authorization.model_copy(
            update={
                "status": ProviderCapabilityRepairStatus.CONSUMED.value,
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
    def _original_task_id(repair_task_id: str) -> str:
        if not repair_task_id.endswith(PROVIDER_CAPABILITY_REPAIR_SUFFIX):
            raise ValueError("Task is not a provider capability repair task")
        return repair_task_id[: -len(PROVIDER_CAPABILITY_REPAIR_SUFFIX)]

    @staticmethod
    def _validate_identity(
        authorization: ProviderCapabilityRepairAuthorization,
        **expected: str,
    ) -> None:
        for field, value in expected.items():
            if getattr(authorization, field) != value:
                raise ValueError(
                    "Existing provider capability repair authorization has different identity"
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

    def _load(self, path: Path) -> ProviderCapabilityRepairAuthorization:
        return ProviderCapabilityRepairAuthorization.model_validate(
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
