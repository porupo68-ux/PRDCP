from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from common.models.pmp import MessageType
from common.role_definitions.exceptions import (
    RoleDefinitionAgentIDMismatchError,
    RoleDefinitionValidationError,
    RoleDefinitionVersionUnsupportedError,
)
from common.role_definitions.models import definition_body, definition_identity


SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SECRET_KEYS = {"api_key", "discord_token", "openrouter_token", "password", "secret_url"}


class RoleDefinitionValidator:
    def __init__(self, supported_schema_major: int = 1) -> None:
        self.supported_schema_major = supported_schema_major
        self.valid_message_types = {item.value for item in MessageType}

    def validate(
        self,
        content: dict[str, Any],
        *,
        expected_agent_id: str,
        source_path: Path,
    ) -> None:
        if not isinstance(content, dict) or not content:
            self._fail(expected_agent_id, "RD root must be a non-empty object")
        body = definition_body(content)
        identity = definition_identity(content)
        agent_id = body.get("agent_id") or identity.get("agent_id")
        if agent_id != expected_agent_id:
            raise RoleDefinitionAgentIDMismatchError(
                f"RD agent_id {agent_id!r} does not match registry key {expected_agent_id!r}",
                agent_id=expected_agent_id,
            )
        layer_id = body.get("layer_id") or identity.get("layer_id")
        expected_layer = expected_agent_id.split(".", 1)[0]
        if layer_id != expected_layer:
            self._fail(expected_agent_id, f"layer_id must be {expected_layer!r}, got {layer_id!r}")
        for field in ("role_definition_id", "role_definition_version", "schema_version"):
            if not isinstance(body.get(field), str) or not body[field].strip():
                self._fail(expected_agent_id, f"Missing required field: {field}")
        schema_version = body["schema_version"]
        role_version = body["role_definition_version"]
        if not SEMVER.fullmatch(schema_version) or int(schema_version.split(".", 1)[0]) != self.supported_schema_major:
            raise RoleDefinitionVersionUnsupportedError(
                f"Unsupported RD schema_version for {expected_agent_id}: {schema_version}",
                agent_id=expected_agent_id,
            )
        if not SEMVER.fullmatch(role_version):
            self._fail(expected_agent_id, f"role_definition_version is not SemVer: {role_version}")
        mission = body.get("mission")
        if not mission and not body.get("purpose"):
            self._fail(expected_agent_id, "mission is required")
        if not self._has_entries(body.get("responsibilities")):
            self._fail(expected_agent_id, "responsibilities must not be empty")
        boundary_sources = (
            body.get("constraints"),
            body.get("non_responsibilities"),
            (body.get("decision_authority") or {}).get("prohibited")
            if isinstance(body.get("decision_authority"), dict)
            else None,
        )
        if not any(self._has_entries(value) for value in boundary_sources):
            self._fail(expected_agent_id, "constraints or prohibited actions are required")
        success = mission.get("success_definition") if isinstance(mission, dict) else None
        if not success and not self._has_entries(body.get("completion_criteria")) and not self._has_entries(body.get("completion_conditions")):
            self._fail(expected_agent_id, "success definition or completion conditions are required")
        runtime = body.get("runtime_contract")
        if not isinstance(runtime, dict):
            self._fail(expected_agent_id, "runtime_contract is required")
        accepted = runtime.get("accepted_message_types")
        generated = runtime.get("generated_message_types")
        if not isinstance(accepted, list) or not accepted:
            self._fail(expected_agent_id, "runtime accepted_message_types must not be empty")
        if not isinstance(generated, list) or not generated:
            self._fail(expected_agent_id, "runtime generated_message_types must not be empty")
        invalid_types = sorted((set(accepted) | set(generated)) - self.valid_message_types)
        if invalid_types:
            self._fail(expected_agent_id, f"Unknown PMP message types: {invalid_types}")
        for field in ("input_schema_id", "output_schema_id"):
            schema_id = runtime.get(field)
            if schema_id is not None:
                self._validate_schema_reference(expected_agent_id, schema_id, source_path)
        self._scan_secret_keys(content, expected_agent_id)

    @staticmethod
    def _has_entries(value: Any) -> bool:
        if isinstance(value, list):
            return bool(value)
        if isinstance(value, dict):
            return any(RoleDefinitionValidator._has_entries(v) for v in value.values())
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _validate_schema_reference(agent_id: str, schema_id: Any, source_path: Path) -> None:
        if not isinstance(schema_id, str) or "." not in schema_id:
            raise RoleDefinitionValidationError(
                f"Invalid schema reference for {agent_id}: {schema_id!r}", agent_id=agent_id
            )
        module_name, symbol_name = schema_id.rsplit(".", 1)
        try:
            symbol = getattr(importlib.import_module(module_name), symbol_name)
        except (ImportError, AttributeError) as exc:
            raise RoleDefinitionValidationError(
                f"Schema reference does not exist for {agent_id}: {schema_id} ({source_path})",
                agent_id=agent_id,
            ) from exc
        if not isinstance(symbol, type) or not issubclass(symbol, BaseModel):
            raise RoleDefinitionValidationError(
                f"Schema reference is not a Pydantic model: {schema_id}", agent_id=agent_id
            )

    def _scan_secret_keys(self, value: Any, agent_id: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in SECRET_KEYS:
                    self._fail(agent_id, f"Secret-bearing field is prohibited in an RD: {key}")
                self._scan_secret_keys(child, agent_id)
        elif isinstance(value, list):
            for child in value:
                self._scan_secret_keys(child, agent_id)

    @staticmethod
    def _fail(agent_id: str, message: str) -> None:
        raise RoleDefinitionValidationError(message, agent_id=agent_id)
