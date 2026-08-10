from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from common.models.pmp import MessageStatus, MessageType
from config.settings import BASE_DIR


@dataclass(frozen=True)
class ContractCheck:
    name: str
    passed: bool
    detail: str


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_common_specifications(base_dir: Path = BASE_DIR) -> list[ContractCheck]:
    """Detect drift between the machine-readable design and runtime files."""

    spec_dir = base_dir / "specifications" / "common"
    config_dir = base_dir / "config"
    rd_registry_path = base_dir / "role_definitions" / "registry.json"
    required_specs = {
        "agent_registry": spec_dir / "agent_registry.v2.json",
        "message_types": spec_dir / "message_type_registry.v2.json",
        "statuses": spec_dir / "status_registry.v2.json",
        "handoffs": spec_dir / "handoff_contracts.v2.json",
        "pmp_schema": spec_dir / "PMP v2.0.schema.json",
    }
    checks: list[ContractCheck] = []

    missing = [path.name for path in required_specs.values() if not path.is_file()]
    checks.append(
        ContractCheck(
            "canonical specification files",
            not missing,
            "all required files are present" if not missing else f"missing: {', '.join(missing)}",
        )
    )
    if missing:
        return checks

    try:
        agent_registry = _read_json(required_specs["agent_registry"])
        message_registry = _read_json(required_specs["message_types"])
        status_registry = _read_json(required_specs["statuses"])
        handoff_registry = _read_json(required_specs["handoffs"])
        _read_json(required_specs["pmp_schema"])
        configured_agents = _read_json(config_dir / "agents.json")
        configured_models = _read_json(config_dir / "models.json")
        overrides = _read_json(config_dir / "implementation_overrides.json")
        rd_registry = _read_json(rd_registry_path)["role_definitions"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        checks.append(ContractCheck("specification JSON", False, str(exc)))
        return checks

    checks.append(ContractCheck("specification JSON", True, "all registries parse as JSON"))

    canonical_agents = {item["agent_id"] for item in agent_registry["agents"]}
    canonical_endpoints = {item["endpoint_id"] for item in agent_registry.get("endpoints", [])}
    routing_ids = set(configured_agents)
    expected_routing_ids = canonical_agents
    checks.append(
        _set_check(
            "agent registry",
            routing_ids,
            expected_routing_ids,
            suffix=f" (+ {len(canonical_endpoints)} delivery endpoint)",
        )
    )

    excluded_agents = set(overrides.get("excluded_agents", {}))
    unknown_exclusions = excluded_agents - canonical_agents
    checks.append(
        ContractCheck(
            "documented implementation overrides",
            not unknown_exclusions,
            (
                f"{len(excluded_agents)} explicit exclusion(s)"
                if not unknown_exclusions
                else f"unknown exclusions: {', '.join(sorted(unknown_exclusions))}"
            ),
        )
    )
    implemented_agents = canonical_agents - excluded_agents
    checks.append(_set_check("model configuration", set(configured_models), implemented_agents))
    checks.append(_set_check("RD registry", set(rd_registry), implemented_agents))

    runtime_message_types = {item.value for item in MessageType}
    canonical_message_types = set(message_registry["values"])
    checks.append(
        _set_check("PMP message types", runtime_message_types, canonical_message_types)
    )

    runtime_message_statuses = {item.value for item in MessageStatus}
    canonical_message_statuses = set(status_registry["enums"]["message_metadata_status"])
    checks.append(
        _set_check("PMP metadata statuses", runtime_message_statuses, canonical_message_statuses)
    )

    contract_types = {
        contract["message_type"] for contract in handoff_registry["contracts"].values()
    }
    checks.append(
        ContractCheck(
            "cross-layer handoff message types",
            contract_types <= runtime_message_types,
            (
                f"{len(contract_types)} handoff types are registered"
                if contract_types <= runtime_message_types
                else "unregistered: "
                + ", ".join(sorted(contract_types - runtime_message_types))
            ),
        )
    )
    return checks


def _set_check(
    name: str,
    actual: set[str],
    expected: set[str],
    *,
    suffix: str = "",
) -> ContractCheck:
    missing = expected - actual
    extra = actual - expected
    if not missing and not extra:
        return ContractCheck(name, True, f"{len(actual)} entries match{suffix}")
    parts = []
    if missing:
        parts.append("missing=" + ",".join(sorted(missing)))
    if extra:
        parts.append("extra=" + ",".join(sorted(extra)))
    return ContractCheck(name, False, "; ".join(parts))
