from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.role_definitions.access_log import RDAccessLog
from common.role_definitions.cache import RoleDefinitionCache
from common.role_definitions.exceptions import (
    RoleDefinitionDuplicateAgentIDError,
    RoleDefinitionError,
    RoleDefinitionHashError,
    RoleDefinitionInvalidJSONError,
    RoleDefinitionSectionNotFoundError,
)
from common.role_definitions.models import (
    RoleDefinitionSnapshot,
    definition_body,
    definition_identity,
)
from common.role_definitions.registry import RoleDefinitionRegistry
from common.role_definitions.validator import RoleDefinitionValidator


ALLOWED_SECTIONS = {
    "mission",
    "responsibilities",
    "responsibility_boundaries",
    "decision_rules",
    "constraints",
    "prohibited_actions",
    "success_definition",
    "failure_conditions",
    "output_requirements",
    "revision_rules",
}


class RoleDefinitionLoader:
    def __init__(
        self,
        registry: RoleDefinitionRegistry,
        *,
        validator: RoleDefinitionValidator | None = None,
        cache: RoleDefinitionCache | None = None,
        access_log: RDAccessLog | None = None,
        reload_on_change: bool = False,
    ) -> None:
        self.registry = registry
        self.validator = validator or RoleDefinitionValidator()
        self.cache = cache or RoleDefinitionCache()
        self.access_log = access_log or RDAccessLog()
        self.reload_on_change = reload_on_change
        self.disabled_agents: dict[str, str] = {}

    @classmethod
    def from_project(
        cls,
        base_dir: Path,
        *,
        reload_on_change: bool = False,
        access_log_path: Path | None = None,
        preload: bool = True,
        strict: bool = True,
    ) -> "RoleDefinitionLoader":
        root = base_dir / "role_definitions"
        loader = cls(
            RoleDefinitionRegistry(root),
            reload_on_change=reload_on_change,
            access_log=RDAccessLog(access_log_path),
        )
        if preload:
            loader.preload(strict=strict)
        return loader

    def preload(self, *, strict: bool = True) -> dict[str, str]:
        failures: dict[str, str] = {}
        seen_internal_ids: dict[str, str] = {}
        for agent_id in sorted(self.registry.agent_ids):
            try:
                snapshot = self._load_from_disk(agent_id, event="role_definition_load_completed")
                prior = seen_internal_ids.get(snapshot.agent_id)
                if prior and prior != agent_id:
                    raise RoleDefinitionDuplicateAgentIDError(
                        f"RDs {prior} and {agent_id} declare the same internal agent_id",
                        agent_id=agent_id,
                    )
                seen_internal_ids[snapshot.agent_id] = agent_id
            except RoleDefinitionError as exc:
                failures[agent_id] = str(exc)
                self.disabled_agents[agent_id] = str(exc)
                self.access_log.record(
                    "role_definition_validation_failed",
                    agent_id=agent_id,
                    error_code=exc.error_code,
                )
        if strict and failures:
            details = "; ".join(f"{agent}: {message}" for agent, message in failures.items())
            raise RoleDefinitionError(f"STRICT RD preload failed: {details}")
        return failures

    def load(self, agent_id: str, *, agent_run_id: str | None = None) -> RoleDefinitionSnapshot:
        if agent_id in self.disabled_agents:
            raise RoleDefinitionError(self.disabled_agents[agent_id], agent_id=agent_id)
        path = self.registry.resolve(agent_id)
        fingerprint = self._fingerprint(path)
        cached = self.cache.get(agent_id)
        if cached and (not self.reload_on_change or cached.fingerprint == fingerprint):
            self.access_log.record(
                "role_definition_cache_hit",
                agent_run_id=agent_run_id,
                agent_id=agent_id,
                role_definition_id=cached.snapshot.role_definition_id,
                role_definition_version=cached.snapshot.role_definition_version,
                role_definition_hash=cached.snapshot.content_hash,
                source="cache",
            )
            return cached.snapshot
        self.access_log.record(
            "role_definition_cache_miss",
            agent_run_id=agent_run_id,
            agent_id=agent_id,
        )
        event = "role_definition_reloaded" if cached else "role_definition_load_completed"
        snapshot = self._load_from_disk(agent_id, event=event)
        self.access_log.record(
            "role_definition_snapshot_created",
            agent_run_id=agent_run_id,
            agent_id=agent_id,
            role_definition_id=snapshot.role_definition_id,
            role_definition_version=snapshot.role_definition_version,
            role_definition_hash=snapshot.content_hash,
            source="disk",
        )
        return snapshot

    def _load_from_disk(self, agent_id: str, *, event: str) -> RoleDefinitionSnapshot:
        path = self.registry.resolve(agent_id)
        self.access_log.record("role_definition_load_started", agent_id=agent_id)
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RoleDefinitionInvalidJSONError(
                f"RD for {agent_id} is invalid JSON: {exc}", agent_id=agent_id
            ) from exc
        self.access_log.record("role_definition_validation_started", agent_id=agent_id)
        self.validator.validate(content, expected_agent_id=agent_id, source_path=path)
        self.access_log.record("role_definition_validation_completed", agent_id=agent_id)
        body = definition_body(content)
        identity = definition_identity(content)
        try:
            canonical = json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except (TypeError, ValueError) as exc:
            raise RoleDefinitionHashError(
                f"Failed to hash RD for {agent_id}: {exc}", agent_id=agent_id
            ) from exc
        snapshot = RoleDefinitionSnapshot(
            agent_id=agent_id,
            layer_id=str(body.get("layer_id") or identity["layer_id"]),
            role_definition_id=str(body["role_definition_id"]),
            role_definition_version=str(body["role_definition_version"]),
            schema_version=str(body["schema_version"]),
            content=content,
            content_hash=f"sha256:{digest}",
            loaded_at=datetime.now(timezone.utc),
            source_path=str(path),
        )
        self.cache.set(agent_id, snapshot, self._fingerprint(path))
        self.access_log.record(
            event,
            agent_id=agent_id,
            role_definition_id=snapshot.role_definition_id,
            role_definition_version=snapshot.role_definition_version,
            role_definition_hash=snapshot.content_hash,
        )
        return snapshot

    def get_section(
        self,
        agent_id: str,
        section_name: str,
        *,
        requester_agent_id: str | None = None,
        agent_run_id: str | None = None,
        snapshot: RoleDefinitionSnapshot | None = None,
    ) -> object:
        if section_name not in ALLOWED_SECTIONS:
            raise RoleDefinitionSectionNotFoundError(
                f"Section is not available through the runtime API: {section_name}",
                agent_id=agent_id,
            )
        self._authorize_reference(requester_agent_id, agent_id)
        selected = snapshot or self.load(agent_id, agent_run_id=agent_run_id)
        if selected.agent_id != agent_id:
            raise RoleDefinitionSectionNotFoundError(
                "Snapshot agent_id does not match section target", agent_id=agent_id
            )
        body = definition_body(selected.content)
        aliases: dict[str, object] = {
            "responsibility_boundaries": body.get("responsibility_boundaries")
            or body.get("non_responsibilities"),
            "prohibited_actions": body.get("prohibited_actions")
            or body.get("non_responsibilities")
            or (body.get("decision_authority") or {}).get("prohibited", []),
            "constraints": body.get("constraints") or body.get("non_responsibilities"),
            "success_definition": (body.get("mission") or {}).get("success_definition")
            if isinstance(body.get("mission"), dict)
            else body.get("completion_criteria") or body.get("completion_conditions"),
            "output_requirements": body.get("completion_criteria")
            or body.get("completion_conditions")
            or body.get("quality_requirements"),
            "revision_rules": body.get("revision_policy"),
        }
        value = body.get(section_name, aliases.get(section_name))
        if value is None:
            raise RoleDefinitionSectionNotFoundError(
                f"RD section was not found: {section_name}", agent_id=agent_id
            )
        self.access_log.record(
            "role_definition_section_accessed",
            agent_run_id=agent_run_id,
            agent_id=agent_id,
            requester_agent_id=requester_agent_id or agent_id,
            section_name=section_name,
            role_definition_version=selected.role_definition_version,
            role_definition_hash=selected.content_hash,
        )
        return value

    def get_sections(
        self,
        agent_id: str,
        section_names: list[str],
        *,
        requester_agent_id: str | None = None,
        agent_run_id: str | None = None,
        snapshot: RoleDefinitionSnapshot | None = None,
    ) -> dict[str, object]:
        selected = snapshot or self.load(agent_id, agent_run_id=agent_run_id)
        return {
            name: self.get_section(
                agent_id,
                name,
                requester_agent_id=requester_agent_id,
                agent_run_id=agent_run_id,
                snapshot=selected,
            )
            for name in section_names
        }

    def get_version(self, agent_id: str) -> str:
        return self.load(agent_id).role_definition_version

    @staticmethod
    def _authorize_reference(requester_agent_id: str | None, target_agent_id: str) -> None:
        if requester_agent_id in (None, target_agent_id):
            return
        requester_layer = requester_agent_id.split(".", 1)[0]
        target_layer = target_agent_id.split(".", 1)[0]
        privileged = requester_agent_id.endswith(".manager") or requester_agent_id.endswith(
            ".quality_reviewer"
        )
        if requester_layer != target_layer or not privileged:
            raise RoleDefinitionSectionNotFoundError(
                f"{requester_agent_id} may not inspect {target_agent_id}",
                agent_id=requester_agent_id,
            )

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
