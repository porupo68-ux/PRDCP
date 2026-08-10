from __future__ import annotations

import json
from pathlib import Path

from common.role_definitions.exceptions import (
    RoleDefinitionDuplicateAgentIDError,
    RoleDefinitionInvalidJSONError,
    RoleDefinitionNotFoundError,
    RoleDefinitionValidationError,
)


class RoleDefinitionRegistry:
    def __init__(self, root_dir: Path, registry_path: Path | None = None) -> None:
        self.root_dir = root_dir.resolve()
        self.registry_path = (registry_path or root_dir / "registry.json").resolve()
        self._paths = self._read_registry()
        self._validate_inventory()

    def _read_registry(self) -> dict[str, Path]:
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RoleDefinitionNotFoundError(
                f"RD registry was not found: {self.registry_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RoleDefinitionInvalidJSONError(
                f"RD registry is not valid JSON: {exc}"
            ) from exc
        mappings = raw.get("role_definitions")
        if not isinstance(mappings, dict) or not mappings:
            raise RoleDefinitionValidationError("RD registry must contain role_definitions")
        resolved: dict[str, Path] = {}
        for agent_id, relative in mappings.items():
            if agent_id in resolved:
                raise RoleDefinitionDuplicateAgentIDError(
                    f"Duplicate agent_id in RD registry: {agent_id}", agent_id=agent_id
                )
            if not isinstance(relative, str) or not relative.endswith(".json"):
                raise RoleDefinitionValidationError(
                    f"Invalid RD path for {agent_id}: {relative!r}", agent_id=agent_id
                )
            path = (self.root_dir / relative).resolve()
            if self.root_dir not in path.parents:
                raise RoleDefinitionValidationError(
                    f"RD path escapes the role_definitions directory: {relative}",
                    agent_id=agent_id,
                )
            resolved[agent_id] = path
        return resolved

    def _validate_inventory(self) -> None:
        registered = {path for path in self._paths.values()}
        disk_files = {
            path.resolve()
            for layer in ("producer", "researcher", "deliberation")
            for path in (self.root_dir / layer).glob("*.json")
        }
        unregistered = disk_files - registered
        if unregistered:
            names = ", ".join(sorted(str(path.relative_to(self.root_dir)) for path in unregistered))
            raise RoleDefinitionValidationError(f"Unregistered RD files: {names}")

    def resolve(self, agent_id: str) -> Path:
        try:
            path = self._paths[agent_id]
        except KeyError as exc:
            raise RoleDefinitionNotFoundError(
                f"No Role Definition is registered for {agent_id}", agent_id=agent_id
            ) from exc
        if not path.is_file():
            raise RoleDefinitionNotFoundError(
                f"Role Definition file was not found for {agent_id}: {path}",
                agent_id=agent_id,
            )
        return path

    @property
    def agent_ids(self) -> set[str]:
        return set(self._paths)
