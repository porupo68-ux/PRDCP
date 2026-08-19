from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from common.provider_capability_repair import (
    ProviderCapabilityRepairAuthorization,
    ProviderCapabilityRepairStatus,
)
from common.provider_contract_repair import (
    ProviderContractRepairAuthorization,
    ProviderContractRepairStatus,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VerifiedProviderModelCompatibility(BaseModel):
    """A validated replacement for one exact configured model contract."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    agent_id: str = Field(min_length=1)
    output_schema_id: str = Field(min_length=1)
    incompatible_model_id: str = Field(min_length=1)
    compatible_model_id: str = Field(min_length=1)
    source_workflow_id: str = Field(min_length=1)
    source_authorization_id: str = Field(min_length=1)
    source_repair_task_id: str = Field(min_length=1)
    source_result_message_id: str | None = None
    verification: str = Field(pattern=r"^pydantic_output_validated$")
    verified_at: datetime


class ProviderModelCompatibilityStore:
    """Append-only verified model bindings shared by future logical tasks."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "provider_model_compatibility"

    def record_verified_repair(
        self,
        authorization: (
            ProviderContractRepairAuthorization
            | ProviderCapabilityRepairAuthorization
        ),
        *,
        output_schema_id: str,
        result_task_id: str,
        result_message_id: str | None = None,
    ) -> VerifiedProviderModelCompatibility:
        if authorization.status not in {
            ProviderContractRepairStatus.CONSUMED.value,
            ProviderCapabilityRepairStatus.CONSUMED.value,
        }:
            raise ValueError("Model compatibility requires a consumed repair authorization")
        if result_task_id != authorization.repair_task_id:
            raise ValueError("Model compatibility repair task identity mismatch")
        if authorization.failed_model_id == authorization.repair_model_id:
            raise ValueError("Model compatibility requires distinct model IDs")

        binding = VerifiedProviderModelCompatibility(
            schema_version="1.0",
            provider_id=authorization.provider_id,
            agent_id=authorization.agent_id,
            output_schema_id=output_schema_id,
            incompatible_model_id=authorization.failed_model_id,
            compatible_model_id=authorization.repair_model_id,
            source_workflow_id=authorization.workflow_id,
            source_authorization_id=authorization.authorization_id,
            source_repair_task_id=authorization.repair_task_id,
            source_result_message_id=result_message_id,
            verification="pydantic_output_validated",
            verified_at=utc_now(),
        )
        path = self._binding_path(
            provider_id=binding.provider_id,
            agent_id=binding.agent_id,
            output_schema_id=binding.output_schema_id,
            incompatible_model_id=binding.incompatible_model_id,
        )
        if path.exists():
            existing = self._load(path)
            self._validate_same_contract(existing, binding)
            return existing
        self._write_exclusive(path, binding.model_dump(mode="json"))
        return binding

    def resolve(
        self,
        *,
        provider_id: str,
        agent_id: str,
        output_schema_id: str,
        configured_model_id: str,
    ) -> VerifiedProviderModelCompatibility | None:
        path = self._binding_path(
            provider_id=provider_id,
            agent_id=agent_id,
            output_schema_id=output_schema_id,
            incompatible_model_id=configured_model_id,
        )
        if not path.exists():
            return None
        binding = self._load(path)
        if (
            binding.provider_id != provider_id
            or binding.agent_id != agent_id
            or binding.output_schema_id != output_schema_id
            or binding.incompatible_model_id != configured_model_id
        ):
            raise ValueError("Persisted model compatibility binding identity mismatch")
        return binding

    def list_verified(
        self,
        *,
        provider_id: str | None = None,
    ) -> list[VerifiedProviderModelCompatibility]:
        root = self.root / provider_id if provider_id else self.root
        if not root.exists():
            return []
        return [self._load(path) for path in sorted(root.rglob("*.json"))]

    def _binding_path(
        self,
        *,
        provider_id: str,
        agent_id: str,
        output_schema_id: str,
        incompatible_model_id: str,
    ) -> Path:
        contract = "\n".join((output_schema_id, incompatible_model_id))
        contract_hash = hashlib.sha256(contract.encode("utf-8")).hexdigest()
        return (
            self.root
            / self._path_component(provider_id)
            / self._path_component(agent_id)
            / f"{contract_hash}.json"
        )

    @staticmethod
    def _path_component(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            return value
        return "id-" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_same_contract(
        existing: VerifiedProviderModelCompatibility,
        candidate: VerifiedProviderModelCompatibility,
    ) -> None:
        identity = (
            "provider_id",
            "agent_id",
            "output_schema_id",
            "incompatible_model_id",
            "compatible_model_id",
        )
        if any(getattr(existing, name) != getattr(candidate, name) for name in identity):
            raise ValueError(
                "A different verified model compatibility binding already exists"
            )

    @staticmethod
    def _load(path: Path) -> VerifiedProviderModelCompatibility:
        return VerifiedProviderModelCompatibility.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    @staticmethod
    def _write_exclusive(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
