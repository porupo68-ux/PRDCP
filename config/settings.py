from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DEMO_SAFE_MODE_FALSE_VALUES = {"0", "false", "no", "off"}
SUPPORTED_PROVIDERS = {"mock", "openrouter"}
SUPPORTED_RETRIEVAL_PROVIDERS = {"mock", "openrouter"}
_DOTENV_MANAGED_VALUES: dict[str, str] = {}


def demo_safe_mode_from_env() -> bool:
    value = os.getenv("PRDCP_DEMO_SAFE_MODE", "true").strip().lower()
    return value not in DEMO_SAFE_MODE_FALSE_VALUES


def load_env_file(path: Path | None = None, *, refresh: bool = False) -> None:
    """Load a small .env file without overriding operator-owned environment values.

    ``refresh`` updates only values that this loader previously installed.  This
    lets a long-running Discord process observe a changed .env file while still
    preserving values supplied by the parent process or changed explicitly at
    runtime.
    """
    env_path = path or BASE_DIR / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        raw_lines = []
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    if refresh:
        for key, previous in list(_DOTENV_MANAGED_VALUES.items()):
            if key not in values:
                if os.environ.get(key) == previous:
                    os.environ.pop(key, None)
                _DOTENV_MANAGED_VALUES.pop(key, None)

    for key, value in values.items():
        previous = _DOTENV_MANAGED_VALUES.get(key)
        current = os.environ.get(key)
        if current is None or (refresh and previous is not None and current == previous):
            os.environ[key] = value
            _DOTENV_MANAGED_VALUES[key] = value
        elif previous is not None and current != previous:
            # A caller deliberately replaced a loader-owned value.  From this
            # point it is operator-owned and must not be overwritten by .env.
            _DOTENV_MANAGED_VALUES.pop(key, None)


@dataclass(frozen=True)
class Settings:
    provider: str
    discord_bot_token: str | None
    openrouter_api_key: str | None
    openrouter_base_url: str
    data_dir: Path
    log_level: str
    models: dict[str, str]
    retrieval_provider: str = "mock"
    retrieval_model: str = "google/gemini-3.7-flash"
    retrieval_engine: str = "exa"
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
    def from_env(cls, *, refresh_dotenv: bool = False) -> "Settings":
        load_env_file(refresh=refresh_dotenv)
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
        retrieval_provider = os.getenv("PRDCP_RETRIEVAL_PROVIDER", provider).strip().lower()
        if retrieval_provider not in SUPPORTED_RETRIEVAL_PROVIDERS:
            raise ValueError("PRDCP_RETRIEVAL_PROVIDER must be 'mock' or 'openrouter'")
        return cls(
            provider=provider,
            discord_bot_token=os.getenv("DISCORD_BOT_TOKEN") or None,
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            data_dir=data_dir,
            log_level=os.getenv("PRDCP_LOG_LEVEL", "INFO").upper(),
            models=models,
            retrieval_provider=retrieval_provider,
            retrieval_model=os.getenv(
                "OPENROUTER_RETRIEVAL_MODEL", "google/gemini-3.7-flash"
            ).strip(),
            retrieval_engine=os.getenv("OPENROUTER_RETRIEVAL_ENGINE", "exa").strip(),
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
