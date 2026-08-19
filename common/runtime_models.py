from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence
from uuid import uuid4

from common.provider_model_compatibility import ProviderModelCompatibilityStore
from config.settings import Settings


LAYER_NAMES = ("producer", "researcher", "deliberation", "conclusion", "playwright")


class RuntimeModelDriftError(RuntimeError):
    """Raised before reservation when configured and live agent models differ."""


@dataclass(frozen=True)
class RuntimeModelEntry:
    agent_id: str
    configured_model: str
    runtime_model: str | None
    compatibility_binding_models: tuple[str, ...]
    resolved_model: str | None
    drifted: bool


@dataclass(frozen=True)
class RuntimeModelAudit:
    provider_id: str
    checked_at: str
    entries: tuple[RuntimeModelEntry, ...]

    @property
    def drifted(self) -> tuple[RuntimeModelEntry, ...]:
        return tuple(item for item in self.entries if item.drifted)

    def for_layer(self, layer: str) -> "RuntimeModelAudit":
        prefix = f"{layer}."
        return RuntimeModelAudit(
            provider_id=self.provider_id,
            checked_at=self.checked_at,
            entries=tuple(item for item in self.entries if item.agent_id.startswith(prefix)),
        )

    def model_dump(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "checked_at": self.checked_at,
            "entries": [asdict(item) for item in self.entries],
            "drift_count": len(self.drifted),
        }


def collect_runtime_models(managers: Iterable[object]) -> dict[str, str]:
    """Read the models actually held by the five long-lived manager registries."""

    runtime: dict[str, str] = {}
    for manager in managers:
        registry = getattr(manager, "registry", None)
        if registry is None:
            continue
        manager_id = getattr(manager, "agent_id", None)
        models = getattr(registry, "models", {})
        if isinstance(manager_id, str) and isinstance(models, dict):
            model = models.get(manager_id)
            if isinstance(model, str):
                runtime[manager_id] = model
        agent_ids = getattr(registry, "agent_ids", set())
        for agent_id in agent_ids:
            agent = registry.get(agent_id)
            model = getattr(agent, "model", None)
            if isinstance(model, str):
                runtime[agent_id] = model
    return runtime


def audit_runtime_models(
    settings: Settings,
    managers: Sequence[object],
) -> RuntimeModelAudit:
    runtime = collect_runtime_models(managers)
    try:
        bindings = ProviderModelCompatibilityStore(settings.data_dir).list_verified(
            provider_id=settings.provider
        )
    except Exception:
        # Corrupt binding metadata must not disguise base-model drift.  Doctor
        # reports that storage error separately; the runtime comparison remains
        # deterministic and fail-closed for missing runtime models.
        bindings = []
    entries: list[RuntimeModelEntry] = []
    for agent_id, configured_model in sorted(settings.models.items()):
        runtime_model = runtime.get(agent_id)
        binding_models = tuple(
            sorted(
                {
                    item.compatible_model_id
                    for item in bindings
                    if item.agent_id == agent_id
                    and item.incompatible_model_id == configured_model
                }
            )
        )
        # A verified provider-agent-schema binding is an invocation-time model
        # resolution.  It is deliberately not compared as the registry's base
        # model, otherwise valid Conclusion/Playwright bindings look like drift.
        resolved_model = (
            binding_models[0]
            if len(binding_models) == 1
            else runtime_model
        )
        entries.append(
            RuntimeModelEntry(
                agent_id=agent_id,
                configured_model=configured_model,
                runtime_model=runtime_model,
                compatibility_binding_models=binding_models,
                resolved_model=resolved_model,
                drifted=(runtime_model != configured_model),
            )
        )
    return RuntimeModelAudit(
        provider_id=settings.provider,
        checked_at=datetime.now(timezone.utc).isoformat(),
        entries=tuple(entries),
    )


class RuntimeModelGuard:
    """Execution-boundary guard for long-lived Discord managers."""

    def __init__(
        self,
        managers: Sequence[object],
        *,
        settings_loader: Callable[[], Settings],
    ) -> None:
        self.managers = tuple(managers)
        self.settings_loader = settings_loader

    def inspect(self, *, layer: str | None = None) -> RuntimeModelAudit:
        settings = self.settings_loader()
        audit = audit_runtime_models(settings, self.managers)
        if layer is not None:
            if layer not in LAYER_NAMES:
                raise ValueError(f"Unknown PRDCP layer: {layer}")
            audit = audit.for_layer(layer)
        return audit

    def require_current(
        self,
        *,
        layer: str,
        workflow_id: str | None = None,
        operation: str | None = None,
    ) -> RuntimeModelAudit:
        if layer not in LAYER_NAMES:
            raise ValueError(f"Unknown PRDCP layer: {layer}")
        settings = self.settings_loader()
        audit = audit_runtime_models(settings, self.managers).for_layer(layer)
        if audit.drifted:
            detail = "; ".join(
                f"{item.agent_id}: configured={item.configured_model}, "
                f"runtime={item.runtime_model or '<missing>'}"
                for item in audit.drifted
            )
            raise RuntimeModelDriftError(
                "RUNTIME_MODEL_DRIFT: provider call blocked before reservation. "
                f"{detail}. Restart the Discord bot."
            )
        self._persist_snapshot(
            settings.data_dir,
            audit,
            workflow_id=workflow_id,
            operation=operation or layer,
        )
        return audit

    @staticmethod
    def _persist_snapshot(
        data_dir: Path,
        audit: RuntimeModelAudit,
        *,
        workflow_id: str | None,
        operation: str,
    ) -> Path:
        payload = audit.model_dump()
        payload.update(
            {
                "workflow_id": workflow_id,
                "operation": operation,
            }
        )
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(serialized).hexdigest()[:16]
        raw_component = workflow_id or "pre_workflow"
        component = (
            raw_component
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", raw_component)
            else "id-" + hashlib.sha256(raw_component.encode("utf-8")).hexdigest()
        )
        operation_component = (
            operation
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", operation)
            else "operation-" + hashlib.sha256(operation.encode("utf-8")).hexdigest()
        )
        root = Path(data_dir) / "runtime_model_snapshots" / component
        path = root / f"{operation_component}_{digest}.json"
        if path.exists():
            return path
        root.mkdir(parents=True, exist_ok=True)
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
        return path


def format_runtime_model_audit(audit: RuntimeModelAudit) -> str:
    current = len(audit.entries) - len(audit.drifted)
    lines = [f"Runtime models: {current}/{len(audit.entries)} current"]
    if audit.drifted:
        lines.append("RUNTIME_MODEL_DRIFT")
        for item in audit.drifted:
            lines.extend(
                (
                    f"Agent: {item.agent_id}",
                    f"Configured: {item.configured_model}",
                    f"Runtime: {item.runtime_model or '<missing>'}",
                )
            )
        lines.append("Action: Discord Bot restart required")
    return "\n".join(lines)
