from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from dataclasses import dataclass

from common.role_definitions import RoleDefinitionLoader
from common.provider_model_compatibility import ProviderModelCompatibilityStore
from common.runtime_models import audit_runtime_models
from common.specifications import audit_common_specifications
from config.settings import BASE_DIR, Settings
from providers.openrouter_capabilities import (
    ModelCapabilityStatus,
    OpenRouterModelCapabilityClient,
)


RETRIEVAL_REQUIRED_AGENTS = (
    "producer.general_opinion_analyst",
    "researcher.expert_researcher",
    "researcher.academic_researcher",
    "researcher.government_researcher",
    "researcher.news_researcher",
    "researcher.public_opinion_researcher",
    "researcher.politician_researcher",
    "researcher.industry_researcher",
)


@dataclass(frozen=True)
class DiagnosticCheck:
    level: str
    name: str
    detail: str
    action: str | None = None


def run_doctor(
    settings: Settings,
    *,
    capability_client: OpenRouterModelCapabilityClient | None = None,
    runtime_managers: tuple[object, ...] | None = None,
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = [
        DiagnosticCheck(
            "PASS",
            "Effective Runtime Configuration",
            f"provider={settings.provider}, "
            f"demo_safe_mode={str(settings.demo_safe_mode).lower()}",
        )
    ]
    if settings.provider == "openrouter" and not settings.demo_safe_mode:
        checks.append(
            DiagnosticCheck(
                "WARN",
                "Runtime safety",
                "OpenRouter + Demo Safe Mode OFF",
                "Automatic revision and additional provider calls are enabled.",
            )
        )
    python_ok = sys.version_info >= (3, 11)
    checks.append(
        DiagnosticCheck(
            "PASS" if python_ok else "FAIL",
            "Python",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            None if python_ok else "Python 3.11以上を使用してください",
        )
    )

    for module_name, required in (("pydantic", True), ("discord", False)):
        installed = importlib.util.find_spec(module_name) is not None
        level = "PASS" if installed else ("FAIL" if required else "WARN")
        checks.append(
            DiagnosticCheck(
                level,
                f"dependency: {module_name}",
                "installed" if installed else "not installed",
                None if installed else "py -m pip install -r requirements.txt",
            )
        )

    checks.extend(_data_directory_checks(settings))

    for check in audit_common_specifications(BASE_DIR):
        checks.append(
            DiagnosticCheck(
                "PASS" if check.passed else "FAIL",
                f"contract: {check.name}",
                check.detail,
                None if check.passed else "specifications/common と実装Registryを同期してください",
            )
        )

    try:
        loader = RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=settings.data_dir / "logs" / "rd_access.jsonl",
            preload=True,
            strict=True,
        )
        checks.append(
            DiagnosticCheck(
                "PASS",
                "Role Definitions",
                f"{len(loader.registry.agent_ids)} RD loaded in STRICT mode",
            )
        )
    except Exception as exc:
        checks.append(
            DiagnosticCheck(
                "FAIL",
                "Role Definitions",
                str(exc),
                "role_definitions/registry.json と対象RDを確認してください",
            )
        )

    checks.extend(_provider_checks(settings))
    if runtime_managers is not None:
        checks.extend(_runtime_model_checks(settings, runtime_managers))
    checks.extend(_provider_model_compatibility_checks(settings))
    checks.extend(
        _provider_model_capability_checks(
            settings,
            capability_client=capability_client,
        )
    )
    checks.extend(
        _retrieval_capability_checks(
            settings,
            capability_client=capability_client,
        )
    )
    token_level = "PASS" if settings.discord_bot_token else "WARN"
    checks.append(
        DiagnosticCheck(
            token_level,
            "Discord",
            "token configured" if settings.discord_bot_token else "optional token is not configured",
            None if settings.discord_bot_token else "Discord利用時だけDISCORD_BOT_TOKENを設定してください",
        )
    )
    return checks


def _runtime_model_checks(
    settings: Settings,
    runtime_managers: tuple[object, ...],
) -> list[DiagnosticCheck]:
    audit = audit_runtime_models(settings, runtime_managers)
    if not audit.drifted:
        return [
            DiagnosticCheck(
                "PASS",
                "Runtime model snapshot",
                f"Configured/Runtime current: {len(audit.entries)}/{len(audit.entries)}",
            )
        ]
    return [
        DiagnosticCheck(
            "FAIL",
            "RUNTIME MODEL DRIFT",
            "; ".join(
                f"{item.agent_id}: configured={item.configured_model}, "
                f"runtime={item.runtime_model or '<missing>'}"
                for item in audit.drifted
            ),
            "Restart the Discord bot before starting a provider-backed layer.",
        )
    ]


def _data_directory_checks(settings: Settings) -> list[DiagnosticCheck]:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix=".prdcp-doctor-", dir=settings.data_dir)
        os.close(fd)
        os.unlink(path)
        return [DiagnosticCheck("PASS", "data directory", str(settings.data_dir))]
    except OSError as exc:
        return [
            DiagnosticCheck(
                "FAIL",
                "data directory",
                str(exc),
                "PRDCP_DATA_DIRを読み書き可能なフォルダへ変更してください",
            )
        ]


def _provider_checks(settings: Settings) -> list[DiagnosticCheck]:
    if settings.provider == "mock":
        configured = sum(bool(value) for value in settings.models.values())
        return [
            DiagnosticCheck("PASS", "provider", "mock (APIなしE2Eを実行可能)"),
            DiagnosticCheck(
                "WARN",
                "OpenRouter readiness",
                f"model IDs configured: {configured}/{len(settings.models)}",
                "実API利用時はAPI keyと全MODEL_*を設定後、再度--doctorを実行してください",
            ),
        ]

    checks: list[DiagnosticCheck] = []
    checks.append(
        DiagnosticCheck(
            "PASS" if settings.openrouter_api_key else "FAIL",
            "OpenRouter API key",
            "configured" if settings.openrouter_api_key else "missing",
            None if settings.openrouter_api_key else "OPENROUTER_API_KEYを設定してください",
        )
    )
    missing_models = sorted(agent_id for agent_id, model in settings.models.items() if not model)
    checks.append(
        DiagnosticCheck(
            "PASS" if not missing_models else "FAIL",
            "OpenRouter model IDs",
            "all configured" if not missing_models else f"missing {len(missing_models)}: " + ", ".join(missing_models),
            None if not missing_models else "対応するMODEL_*環境変数へ実際のOpenRouter model IDを設定してください",
        )
    )
    checks.append(
        DiagnosticCheck(
            "WARN",
            "OpenRouter live request",
            "not executed by doctor (no cost is incurred)",
            "設定後に小規模な実APIワークフローで疎通確認してください",
        )
    )
    return checks


def _provider_model_compatibility_checks(
    settings: Settings,
) -> list[DiagnosticCheck]:
    try:
        bindings = ProviderModelCompatibilityStore(settings.data_dir).list_verified(
            provider_id=settings.provider
        )
    except Exception as exc:
        return [
            DiagnosticCheck(
                "FAIL",
                "Provider model compatibility",
                str(exc),
                "provider_model_compatibilityの監査記録を確認してください",
            )
        ]
    if not bindings:
        return []
    details = "; ".join(
        f"{item.agent_id}: {item.incompatible_model_id} -> {item.compatible_model_id}"
        for item in bindings
    )
    return [
        DiagnosticCheck(
            "PASS",
            "Provider model compatibility",
            f"{len(bindings)} verified binding(s): {details}",
        )
    ]


def _provider_model_capability_checks(
    settings: Settings,
    *,
    capability_client: OpenRouterModelCapabilityClient | None,
) -> list[DiagnosticCheck]:
    if settings.provider != "openrouter":
        return []
    client = capability_client or OpenRouterModelCapabilityClient(
        base_url=settings.openrouter_base_url,
    )
    try:
        bindings = ProviderModelCompatibilityStore(settings.data_dir).list_verified(
            provider_id="openrouter"
        )
    except Exception as exc:
        return [
            DiagnosticCheck(
                "FAIL",
                "MODEL CAPABILITY PREFLIGHT",
                f"UNKNOWN: verified binding metadata unavailable: {exc}",
                "provider_model_compatibilityの監査記録を確認してください",
            )
        ]

    compatible_count = 0
    unknown_count = 0
    failed_count = 0
    checks: list[DiagnosticCheck] = []
    for agent_id, configured_model in settings.models.items():
        effective_model, binding_note, binding_conflict = _effective_model_for_preflight(
            agent_id=agent_id,
            configured_model=configured_model,
            bindings=bindings,
        )
        if binding_conflict:
            unknown_count += 1
            checks.append(
                DiagnosticCheck(
                    "FAIL",
                    f"model capability: {agent_id}",
                    f"UNKNOWN configured={configured_model}: {binding_conflict}",
                    "競合するverified model bindingを確認してください",
                )
            )
            continue
        result = client.inspect(effective_model)
        detail = (
            f"{result.status.value} model={effective_model}, "
            f"resolved={result.resolved_model_id or '-'}, "
            f"endpoints={result.compatible_endpoint_count}/{result.endpoint_count}, "
            f"reason={result.reason}"
        )
        if binding_note:
            detail += f", {binding_note}"
        if result.status is ModelCapabilityStatus.COMPATIBLE:
            compatible_count += 1
            level = "PASS"
            action = None
        elif result.status is ModelCapabilityStatus.UNKNOWN:
            unknown_count += 1
            level = "FAIL"
            action = "OpenRouter metadataを再取得し、UNKNOWNを解消してください"
        else:
            failed_count += 1
            level = "FAIL"
            action = "strict Structured Output対応modelへ設定を変更してください"
        checks.append(
            DiagnosticCheck(
                level,
                f"model capability: {agent_id}",
                detail,
                action,
            )
        )

    total = len(settings.models)
    summary_level = "PASS" if failed_count == 0 and unknown_count == 0 else "FAIL"
    checks.append(
        DiagnosticCheck(
            summary_level,
            "MODEL CAPABILITY PREFLIGHT",
            f"Compatible: {compatible_count}/{total}, Unknown: {unknown_count}, "
            f"Failed: {failed_count}",
            (
                None
                if summary_level == "PASS"
                else "Failed/Unknownが0になるまでReal workflowを開始しないでください"
            ),
        )
    )
    return checks


def _effective_model_for_preflight(
    *,
    agent_id: str,
    configured_model: str,
    bindings: list,
) -> tuple[str, str | None, str | None]:
    replacements = {
        item.compatible_model_id
        for item in bindings
        if item.agent_id == agent_id
        and item.incompatible_model_id == configured_model
    }
    if not replacements:
        return configured_model, None, None
    if len(replacements) > 1:
        return (
            configured_model,
            None,
            "multiple verified replacements exist for this configured model",
        )
    effective_model = next(iter(replacements))
    return (
        effective_model,
        f"verified binding: {configured_model} -> {effective_model}",
        None,
    )


def _retrieval_capability_checks(
    settings: Settings,
    *,
    capability_client: OpenRouterModelCapabilityClient | None,
) -> list[DiagnosticCheck]:
    if settings.provider == "mock":
        return [
            DiagnosticCheck(
                "PASS",
                "RETRIEVAL CAPABILITY PREFLIGHT",
                f"Retrieval Required: {len(RETRIEVAL_REQUIRED_AGENTS)}, "
                f"Retrieval Ready: {len(RETRIEVAL_REQUIRED_AGENTS)}/{len(RETRIEVAL_REQUIRED_AGENTS)} "
                "(mock)",
            )
        ]
    checks: list[DiagnosticCheck] = []
    ready = False
    reason = ""
    if settings.retrieval_provider != "openrouter":
        reason = f"unsupported runtime pairing: {settings.retrieval_provider}"
    elif not settings.openrouter_api_key:
        reason = "OPENROUTER_API_KEY missing"
    elif settings.retrieval_engine not in {
        "auto",
        "native",
        "exa",
        "firecrawl",
        "parallel",
        "perplexity",
    }:
        reason = f"unsupported engine={settings.retrieval_engine}"
    else:
        client = capability_client or OpenRouterModelCapabilityClient(
            base_url=settings.openrouter_base_url,
        )
        result = client.inspect(settings.retrieval_model)
        ready = result.status is ModelCapabilityStatus.COMPATIBLE
        reason = (
            f"provider=openrouter_web_search, engine={settings.retrieval_engine}, "
            f"executor_model={settings.retrieval_model}, model_status={result.status.value}, "
            f"reason={result.reason}"
        )
    for agent_id in RETRIEVAL_REQUIRED_AGENTS:
        checks.append(
            DiagnosticCheck(
                "PASS" if ready else "FAIL",
                f"retrieval capability: {agent_id}",
                ("PASS " if ready else "FAILED ") + reason,
                None if ready else "Retrieval provider設定とexecutor modelを確認してください",
            )
        )
    total = len(RETRIEVAL_REQUIRED_AGENTS)
    checks.append(
        DiagnosticCheck(
            "PASS" if ready else "FAIL",
            "RETRIEVAL CAPABILITY PREFLIGHT",
            f"Retrieval Required: {total}, Retrieval Ready: {total if ready else 0}/{total}",
            None if ready else "Retrieval Readyが8/8になるまでReal workflowを開始しないでください",
        )
    )
    return checks


def print_doctor_report(checks: list[DiagnosticCheck], *, json_output: bool = False) -> int:
    if json_output:
        import json

        print(json.dumps([check.__dict__ for check in checks], ensure_ascii=False, indent=2))
    else:
        print("PRDCP Doctor v2")
        for check in checks:
            print(f"[{check.level}] {check.name}: {check.detail}")
            if check.action:
                print(f"       action: {check.action}")
        failures = sum(check.level == "FAIL" for check in checks)
        warnings = sum(check.level == "WARN" for check in checks)
        result = "READY" if failures == 0 else "BLOCKED"
        print(f"Result: {result} (fail={failures}, warn={warnings})")
    return 1 if any(check.level == "FAIL" for check in checks) else 0
