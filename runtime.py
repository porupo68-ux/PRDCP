from __future__ import annotations

from config.settings import Settings
from config.settings import BASE_DIR
from common.role_definitions import RoleDefinitionLoader
from deliberation.manager import DeliberationManager
from deliberation.registry import DeliberationRegistry
from conclusion.manager import ConclusionManager
from conclusion.registry import ConclusionRegistry
from playwright.manager import PlaywrightManager
from playwright.registry import PlaywrightRegistry
from producer.manager import ProducerManager
from producer.registry import ProducerRegistry
from providers import MockModelProvider, OpenRouterModelProvider
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from storage import (
    DeliberationWorkflowRepository,
    ConclusionWorkflowRepository,
    PlaywrightWorkflowRepository,
    ResearcherWorkflowRepository,
    WorkflowRepository,
)


def build_provider(settings: Settings):
    if settings.provider == "openrouter":
        if not settings.openrouter_api_key:
            raise ValueError("PRDCP_PROVIDER=openrouter requires OPENROUTER_API_KEY")
        return OpenRouterModelProvider(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )
    return MockModelProvider()


def build_role_definition_loader(settings: Settings) -> RoleDefinitionLoader:
    return RoleDefinitionLoader.from_project(
        BASE_DIR,
        reload_on_change=settings.rd_reload,
        access_log_path=settings.data_dir / "logs" / "rd_access.jsonl",
        preload=True,
        strict=settings.rd_strict,
    )


def build_producer_manager(
    settings: Settings,
    *,
    provider=None,
    rd_loader: RoleDefinitionLoader | None = None,
) -> ProducerManager:
    provider = provider or build_provider(settings)
    rd_loader = rd_loader or build_role_definition_loader(settings)
    registry = ProducerRegistry(provider, settings.models, rd_loader=rd_loader)
    repository = WorkflowRepository(settings.data_dir)
    return ProducerManager(registry, repository, rd_loader=rd_loader)


def build_researcher_manager(
    settings: Settings,
    *,
    provider=None,
    rd_loader: RoleDefinitionLoader | None = None,
) -> ResearcherManager:
    provider = provider or build_provider(settings)
    rd_loader = rd_loader or build_role_definition_loader(settings)
    registry = ResearcherRegistry(provider, settings.models, rd_loader=rd_loader)
    repository = ResearcherWorkflowRepository(settings.data_dir)
    return ResearcherManager(registry, repository, rd_loader=rd_loader)


def build_deliberation_manager(
    settings: Settings,
    *,
    provider=None,
    rd_loader: RoleDefinitionLoader | None = None,
) -> DeliberationManager:
    provider = provider or build_provider(settings)
    rd_loader = rd_loader or build_role_definition_loader(settings)
    registry = DeliberationRegistry(provider, settings.models, rd_loader=rd_loader)
    repository = DeliberationWorkflowRepository(settings.data_dir)
    return DeliberationManager(registry, repository, rd_loader=rd_loader)


def build_conclusion_manager(
    settings: Settings,
    *,
    provider=None,
    rd_loader: RoleDefinitionLoader | None = None,
) -> ConclusionManager:
    provider = provider or build_provider(settings)
    rd_loader = rd_loader or build_role_definition_loader(settings)
    registry = ConclusionRegistry(provider, settings.models, rd_loader=rd_loader)
    repository = ConclusionWorkflowRepository(settings.data_dir)
    return ConclusionManager(registry, repository, rd_loader=rd_loader)


def build_playwright_manager(
    settings: Settings,
    *,
    provider=None,
    rd_loader: RoleDefinitionLoader | None = None,
) -> PlaywrightManager:
    provider = provider or build_provider(settings)
    rd_loader = rd_loader or build_role_definition_loader(settings)
    registry = PlaywrightRegistry(provider, settings.models, rd_loader=rd_loader)
    repository = PlaywrightWorkflowRepository(settings.data_dir)
    return PlaywrightManager(
        registry,
        repository,
        max_revisions=settings.playwright_max_revisions,
        target_duration_seconds=settings.playwright_target_duration_seconds,
        rd_loader=rd_loader,
    )


def build_all_managers(
    settings: Settings,
    *,
    provider=None,
) -> tuple[
    ProducerManager,
    ResearcherManager,
    DeliberationManager,
    ConclusionManager,
    PlaywrightManager,
]:
    provider = provider or build_provider(settings)
    rd_loader = build_role_definition_loader(settings)
    return (
        build_producer_manager(settings, provider=provider, rd_loader=rd_loader),
        build_researcher_manager(settings, provider=provider, rd_loader=rd_loader),
        build_deliberation_manager(settings, provider=provider, rd_loader=rd_loader),
        build_conclusion_manager(settings, provider=provider, rd_loader=rd_loader),
        build_playwright_manager(settings, provider=provider, rd_loader=rd_loader),
    )


def build_managers(
    settings: Settings,
    *,
    provider=None,
) -> tuple[ProducerManager, ResearcherManager, DeliberationManager]:
    provider = provider or build_provider(settings)
    rd_loader = build_role_definition_loader(settings)
    return (
        build_producer_manager(settings, provider=provider, rd_loader=rd_loader),
        build_researcher_manager(settings, provider=provider, rd_loader=rd_loader),
        build_deliberation_manager(settings, provider=provider, rd_loader=rd_loader),
    )


def build_producer_researcher_managers(
    settings: Settings,
    *,
    provider=None,
) -> tuple[ProducerManager, ResearcherManager]:
    provider = provider or build_provider(settings)
    rd_loader = build_role_definition_loader(settings)
    return (
        build_producer_manager(settings, provider=provider, rd_loader=rd_loader),
        build_researcher_manager(settings, provider=provider, rd_loader=rd_loader),
    )


# Producer v1 compatibility.
build_manager = build_producer_manager
