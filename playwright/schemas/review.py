from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlaywrightGateStatus(str, Enum):
    APPROVED = "APPROVED"
    APPROVED_WITH_LIMITATIONS = "APPROVED_WITH_LIMITATIONS"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    UPSTREAM_REVISION_REQUIRED = "UPSTREAM_REVISION_REQUIRED"
    BLOCKED = "BLOCKED"


class PlaywrightFinalGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    final_gate_result_id: str = Field(min_length=1)
    status: PlaywrightGateStatus
    findings: list[dict[str, Any]] = Field(default_factory=list)
    blocking_finding_ids: list[str] = Field(default_factory=list)
    revision_targets: list[str] = Field(default_factory=list)
    upstream_revision_requests: list[dict[str, Any]] = Field(default_factory=list)
    limitations_to_disclose: list[str] = Field(default_factory=list)
    delivery_readiness: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_status_payload(self) -> "PlaywrightFinalGateResult":
        if self.status == PlaywrightGateStatus.REVISION_REQUIRED.value and not self.revision_targets:
            raise ValueError("REVISION_REQUIRED requires revision_targets")
        if self.status == PlaywrightGateStatus.UPSTREAM_REVISION_REQUIRED.value and not self.upstream_revision_requests:
            raise ValueError("UPSTREAM_REVISION_REQUIRED requires requests")
        return self

