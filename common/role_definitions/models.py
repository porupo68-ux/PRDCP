from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


MIN_AGENT_TIMEOUT_SECONDS = 600


class RoleDefinitionSnapshot(BaseModel):
    """Immutable reference to the RD selected at the start of one agent run."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    layer_id: str
    role_definition_id: str
    role_definition_version: str
    schema_version: str
    content: dict[str, Any]
    content_hash: str
    loaded_at: datetime
    source_path: str

    def trace(self) -> dict[str, str]:
        return {
            "agent_id": self.agent_id,
            "role_definition_id": self.role_definition_id,
            "role_definition_version": self.role_definition_version,
            "role_definition_hash": self.content_hash,
        }


class RoleContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    display_name: str
    description: str = ""
    mission: str
    responsibilities: list[str] = Field(default_factory=list)
    responsibility_boundaries: list[str] = Field(default_factory=list)
    decision_rules: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    prohibited_actions: list[str] = Field(default_factory=list)
    success_definition: str
    failure_conditions: list[str] = Field(default_factory=list)
    output_requirements: list[str] = Field(default_factory=list)
    revision_rules: list[str] = Field(default_factory=list)
    uncertainty_rules: list[str] = Field(default_factory=list)


class RoleRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    layer_id: str
    accepted_message_types: list[str] = Field(default_factory=list)
    generated_message_types: list[str] = Field(default_factory=list)
    input_schema_id: str | None = None
    output_schema_id: str | None = None
    timeout_seconds: int = Field(ge=MIN_AGENT_TIMEOUT_SECONDS)
    technical_retry_limit: int = Field(default=2, ge=0)
    revision_limit: int | None = Field(default=None, ge=0)
    parallel_execution_allowed: bool = False
    allowed_tools: list[str] = Field(default_factory=list)
    prohibited_tools: list[str] = Field(default_factory=list)
    prohibited_requested_actions: list[str] = Field(default_factory=list)


def definition_body(content: dict[str, Any]) -> dict[str, Any]:
    body = content.get("role_definition", content)
    if not isinstance(body, dict):
        return {}
    return body


def definition_identity(content: dict[str, Any]) -> dict[str, Any]:
    body = definition_body(content)
    identity = body.get("agent_identity", {})
    return identity if isinstance(identity, dict) else {}
