from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from dataclasses import dataclass

from common.role_definitions import RoleDefinitionLoader
from common.specifications import audit_common_specifications
from config.settings import BASE_DIR, Settings


@dataclass(frozen=True)
class DiagnosticCheck:
    level: str
    name: str
    detail: str
    action: str | None = None


def run_doctor(settings: Settings) -> list[DiagnosticCheck]:
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
