from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationSeverity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    finding_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    severity: ValidationSeverity
    message: str = Field(min_length=1)
    target_agent_id: str | None = None
    upstream_required: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class DeterministicValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validation_id: str = Field(min_length=1)
    is_valid: bool
    findings: list[ValidationFinding] = Field(default_factory=list)
    checked_counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def match_validity(self) -> "DeterministicValidationResult":
        has_error = any(item.severity == ValidationSeverity.ERROR.value for item in self.findings)
        if self.is_valid == has_error:
            raise ValueError("is_valid must be false exactly when ERROR findings exist")
        return self

