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


PROVIDER_CONTRACT_REPAIR_SUFFIX = "_provider_contract_repair_1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProviderContractRepairStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"


class ProviderContractRepairAuthorization(BaseModel):
    """One-shot authorization to repair a repeated provider contract failure.

    A contract repair is deliberately not a retry of the same logical task. It
    is allowed only after the original call and its one operator retry both
    produced a contract-invalid billed response. Response-contract failures
    require a distinct model. A request-schema failure may use the same model
    only when tied to a named wire-schema revision.
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
    source_error_class: str = Field(min_length=1)
    failed_model_id: str = Field(min_length=1)
    retry_failed_model_id: str | None = None
    repair_model_id: str = Field(min_length=1)
    repair_kind: str = Field(
        default="distinct_model",
        pattern=r"^(distinct_model|same_model_schema)$",
    )
    repair_contract_revision: str | None = None
    authorized_by: str = Field(min_length=1)
    status: ProviderContractRepairStatus
    authorized_at: datetime
    consumed_at: datetime | None = None
    reservation_path: str | None = None


class ProviderContractRepairAuthorizationStore:
    """Persistent, auditable one-shot model repair authorization."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "provider_contract_repair_authorizations"

    @classmethod
    def from_reservation_root(
        cls,
        reservation_root: Path,
    ) -> "ProviderContractRepairAuthorizationStore":
        root = Path(reservation_root)
        if root.name != "provider_call_reservations":
            raise ValueError(
                "Provider contract repair requires the canonical reservation root"
            )
        return cls(root.parent)

    def authorize_once(
        self,
        *,
        workflow_id: str,
        provider_id: str,
        agent_id: str,
        original_task_id: str,
        retry_task_id: str,
        source_error_message_id: str,
        failed_model_id: str,
        repair_model_id: str,
        retry_failed_model_id: str | None = None,
        source_error_class: str = "ProviderResponseContractError",
        repair_contract_revision: str | None = None,
        authorized_by: str = "cli.operator",
    ) -> ProviderContractRepairAuthorization:
        self._validate_component(provider_id, "provider_id")
        if original_task_id.endswith(
            (OPERATOR_RETRY_SUFFIX, PROVIDER_CONTRACT_REPAIR_SUFFIX)
        ):
            raise ValueError("Provider contract repair requires the original task ID")
        if retry_task_id != f"{original_task_id}{OPERATOR_RETRY_SUFFIX}":
            raise ValueError("Provider contract repair retry task identity mismatch")
        if not failed_model_id or not repair_model_id:
            raise ValueError("Provider contract repair requires both model IDs")
        if not re.fullmatch(
            r"~?[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*",
            repair_model_id,
        ):
            raise ValueError("Provider contract repair model ID is invalid")
        effective_retry_model_id = retry_failed_model_id or failed_model_id
        if source_error_class not in {
            "ProviderResponseContractError",
            "ProviderRequestSchemaError",
            "NonRetryableAgentError",
        }:
            raise ValueError("Provider contract repair source failure is unsupported")
        same_model_schema_repair = repair_model_id in {
            failed_model_id,
            effective_retry_model_id,
        }
        if same_model_schema_repair:
            if (
                source_error_class != "ProviderRequestSchemaError"
                or not repair_contract_revision
            ):
                raise ValueError(
                    "Provider contract repair must use a different model unless "
                    "repairing an identified request-schema revision"
                )
            repair_kind = "same_model_schema"
        else:
            repair_kind = "distinct_model"

        retry_store = ProviderRetryAuthorizationStore(self.data_dir)
        retry_authorization = retry_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if (
            retry_authorization is None
            or retry_authorization.status != ProviderRetryStatus.CONSUMED.value
            or retry_authorization.agent_id != agent_id
            or retry_authorization.retry_task_id != retry_task_id
        ):
            raise ValueError(
                "Provider contract repair requires a consumed one-shot retry authorization"
            )

        for task_id, expected_model_id in (
            (original_task_id, failed_model_id),
            (retry_task_id, effective_retry_model_id),
        ):
            reservation_path = self._reservation_path(
                provider_id,
                workflow_id,
                task_id,
            )
            if not reservation_path.exists():
                raise ValueError(
                    "Provider contract repair requires both prior provider reservations"
                )
            reservation = self._read_json(reservation_path)
            if (
                reservation.get("workflow_id") != workflow_id
                or reservation.get("task_id") != task_id
                or reservation.get("agent_id") != agent_id
                or reservation.get("model_id") != expected_model_id
            ):
                raise ValueError(
                    "Provider contract repair reservation does not match the failed task"
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
                retry_task_id=retry_task_id,
                source_error_message_id=source_error_message_id,
                failed_model_id=failed_model_id,
                retry_failed_model_id=effective_retry_model_id,
                repair_model_id=repair_model_id,
                source_error_class=source_error_class,
                repair_kind=repair_kind,
                repair_contract_revision=repair_contract_revision,
            )
            if existing.status == ProviderContractRepairStatus.PENDING.value:
                return existing
            raise ValueError("The one-time provider contract repair was already consumed")

        authorization = ProviderContractRepairAuthorization(
            authorization_id=str(uuid4()),
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=agent_id,
            original_task_id=original_task_id,
            retry_task_id=retry_task_id,
            repair_task_id=(
                f"{original_task_id}{PROVIDER_CONTRACT_REPAIR_SUFFIX}"
            ),
            source_retry_authorization_id=retry_authorization.authorization_id,
            source_error_message_id=source_error_message_id,
            source_error_class=source_error_class,
            failed_model_id=failed_model_id,
            retry_failed_model_id=effective_retry_model_id,
            repair_model_id=repair_model_id,
            repair_kind=repair_kind,
            repair_contract_revision=repair_contract_revision,
            authorized_by=authorized_by,
            status=ProviderContractRepairStatus.PENDING,
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
    ) -> ProviderContractRepairAuthorization | None:
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
    ) -> ProviderContractRepairAuthorization:
        original_task_id = self._original_task_id(repair_task_id)
        authorization = self.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Provider contract repair task has no persisted authorization")
        if (
            authorization.repair_task_id != repair_task_id
            or authorization.agent_id != agent_id
            or authorization.repair_model_id != repair_model_id
            or authorization.status != ProviderContractRepairStatus.PENDING.value
        ):
            raise ValueError(
                "Provider contract repair authorization is not pending for this task and model"
            )
        return authorization

    def consume(
        self,
        authorization: ProviderContractRepairAuthorization,
        *,
        reservation_path: Path,
    ) -> ProviderContractRepairAuthorization:
        if authorization.status != ProviderContractRepairStatus.PENDING.value:
            raise ValueError("Provider contract repair authorization is not pending")
        if not reservation_path.exists():
            raise ValueError(
                "Provider contract repair reservation must exist before authorization use"
            )
        updated = authorization.model_copy(
            update={
                "status": ProviderContractRepairStatus.CONSUMED.value,
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
        if not repair_task_id.endswith(PROVIDER_CONTRACT_REPAIR_SUFFIX):
            raise ValueError("Task is not a provider contract repair task")
        return repair_task_id[: -len(PROVIDER_CONTRACT_REPAIR_SUFFIX)]

    @staticmethod
    def _validate_identity(
        authorization: ProviderContractRepairAuthorization,
        **expected: str,
    ) -> None:
        for field, value in expected.items():
            if getattr(authorization, field) != value:
                raise ValueError(
                    "Existing provider contract repair authorization has different identity"
                )

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

    def _load(self, path: Path) -> ProviderContractRepairAuthorization:
        return ProviderContractRepairAuthorization.model_validate(
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
        # Keep the sibling temp name short.  Long logical task filenames can
        # otherwise cross Windows MAX_PATH only during the atomic update even
        # though the canonical authorization file itself is valid.
        temporary = path.with_name(f".contract-{uuid4().hex}.tmp")
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
