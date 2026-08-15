from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEMO_SAFE_MODE_FALSE_VALUES = {"0", "false", "no", "off"}
SUPPORTED_PROVIDERS = {"mock", "openrouter"}


def demo_safe_mode_from_env() -> bool:
    value = os.getenv("PRDCP_DEMO_SAFE_MODE", "true").strip().lower()
    return value not in DEMO_SAFE_MODE_FALSE_VALUES


def load_env_file(path: Path | None = None) -> None:
    """Load a small .env file without adding a runtime dependency."""
    env_path = path or BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    provider: str
    discord_bot_token: str | None
    openrouter_api_key: str | None
    openrouter_base_url: str
    data_dir: Path
    log_level: str
    models: dict[str, str]
    demo_safe_mode: bool = True
    auto_start_researcher: bool = False
    auto_start_deliberation: bool = False
    auto_start_conclusion: bool = False
    auto_start_playwright: bool = False
    playwright_target_duration_seconds: int = 720
    playwright_max_revisions: int = 2
    playwright_max_title_candidates: int = 5
    playwright_max_thumbnail_candidates: int = 5
    playwright_require_all_citations: bool = True
    rd_reload: bool = False
    rd_strict: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        model_config = json.loads((BASE_DIR / "config" / "models.json").read_text(encoding="utf-8"))
        models = {
            agent_id: os.getenv(item["environment_key"], "").strip()
            for agent_id, item in model_config.items()
        }
        configured_data_dir = os.getenv("PRDCP_DATA_DIR", "").strip()
        data_dir = Path(configured_data_dir) if configured_data_dir else BASE_DIR / "storage" / "data"
        provider = os.getenv("PRDCP_PROVIDER", "mock").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError("PRDCP_PROVIDER must be 'mock' or 'openrouter'")
        return cls(
            provider=provider,
            discord_bot_token=os.getenv("DISCORD_BOT_TOKEN") or None,
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            data_dir=data_dir,
            log_level=os.getenv("PRDCP_LOG_LEVEL", "INFO").upper(),
            models=models,
            demo_safe_mode=demo_safe_mode_from_env(),
            auto_start_researcher=os.getenv("PRDCP_AUTO_START_RESEARCHER", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            auto_start_deliberation=os.getenv(
                "PRDCP_AUTO_START_DELIBERATION",
                "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            auto_start_conclusion=os.getenv(
                "PRDCP_AUTO_START_CONCLUSION",
                "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            auto_start_playwright=os.getenv(
                "PRDCP_AUTO_START_PLAYWRIGHT",
                "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            playwright_target_duration_seconds=int(
                os.getenv("PLAYWRIGHT_TARGET_DURATION_SECONDS", "720")
            ),
            playwright_max_revisions=int(os.getenv("PLAYWRIGHT_MAX_REVISIONS", "2")),
            playwright_max_title_candidates=int(
                os.getenv("PLAYWRIGHT_MAX_TITLE_CANDIDATES", "5")
            ),
            playwright_max_thumbnail_candidates=int(
                os.getenv("PLAYWRIGHT_MAX_THUMBNAIL_CANDIDATES", "5")
            ),
            playwright_require_all_citations=os.getenv(
                "PLAYWRIGHT_REQUIRE_ALL_CITATIONS",
                "true",
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            rd_reload=os.getenv("PRDCP_RD_RELOAD", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            rd_strict=os.getenv("PRDCP_RD_STRICT", "true").strip().lower()
            in {"1", "true", "yes", "on"},
        )


def apply_runtime_overrides(
    settings: Settings,
    *,
    provider: str | None = None,
    demo_safe_mode: bool | None = None,
) -> Settings:
    """Return one immutable effective configuration after optional CLI overrides."""
    effective_provider = settings.provider if provider is None else provider.strip().lower()
    if effective_provider not in SUPPORTED_PROVIDERS:
        raise ValueError("provider must be 'mock' or 'openrouter'")
    return replace(
        settings,
        provider=effective_provider,
        demo_safe_mode=(
            settings.demo_safe_mode if demo_safe_mode is None else demo_safe_mode
        ),
    )
