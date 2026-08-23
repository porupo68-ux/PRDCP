"""Canonical router-aware Discord bot entry point for all PRDCP layers."""

from __future__ import annotations

import logging

from conclusion.manager import ConclusionManager
from common.runtime_models import (
    RuntimeModelDriftError,
    RuntimeModelGuard,
    format_runtime_model_audit,
)
from config.settings import Settings
from deliberation.manager import DeliberationManager
from discord_app.channel_router import ChannelRouter, ChannelRoutingError
from discord_app.commands import (
    decide_researcher_evidence,
    inspect_researcher_evidence,
    execute_researcher_revision,
    integrate_conclusion_candidates,
    load_conclusion_package,
    load_conclusion_status,
    load_deliberation_result,
    load_deliberation_status,
    load_final_conclusion,
    load_playwright_result,
    load_playwright_status,
    load_producer_status,
    load_researcher_result,
    load_researcher_status,
    resume_conclusion,
    revise_conclusion,
    resume_deliberation,
    revise_deliberation,
    resume_playwright,
    revise_playwright,
    recover_researcher_evidence,
    run_conclusion,
    run_deliberation,
    run_playwright,
    run_producer,
    revise_producer,
    run_researcher,
    select_conclusion,
)
from discord_app.message_formatter import (
    format_conclusion_options,
    format_conclusion_result,
    format_conclusion_status,
    format_deliberation_result,
    format_deliberation_status,
    format_playwright_result,
    format_playwright_status,
    format_researcher_result,
    format_researcher_evidence,
    format_researcher_sources,
    format_researcher_status,
    format_result,
    format_status,
)
from researcher.schemas.human_evidence import HumanEvidenceDecisionType
from playwright.manager import PlaywrightManager
from producer.manager import ProducerManager
from researcher.manager import ResearcherManager


logger = logging.getLogger(__name__)
DISCORD_OPERATIONAL_ERROR_LIMIT = 700


def summarize_operational_error(
    error: BaseException,
    *,
    max_length: int = DISCORD_OPERATIONAL_ERROR_LIMIT,
) -> str:
    """Return a single bounded Discord-safe summary without traceback details."""

    summary = " ".join(f"{type(error).__name__}: {error}".splitlines()).strip()
    if not summary:
        summary = type(error).__name__
    if len(summary) > max_length:
        return summary[: max_length - 3] + "..."
    return summary


async def report_execution_error(
    ctx,
    layer: str,
    workflow_id: str,
    error: BaseException,
    route_error_status,
) -> None:
    """Log the traceback, notify Discord safely, and close RUNNING as ERROR."""

    logger.error(
        "Discord execution failed: layer=%s workflow=%s",
        layer,
        workflow_id,
        exc_info=(type(error), error, error.__traceback__),
    )
    try:
        await ctx.send(
            f"{layer} execution failed: {summarize_operational_error(error)}"
        )
    except Exception:
        logger.exception(
            "Failed to send execution error summary for workflow %s", workflow_id
        )
    await route_error_status(ctx.guild, workflow_id, layer, "ERROR")


def create_bot(
    manager: ProducerManager,
    researcher_manager: ResearcherManager | None = None,
    deliberation_manager: DeliberationManager | None = None,
    conclusion_manager: ConclusionManager | None = None,
    playwright_manager: PlaywrightManager | None = None,
    *,
    auto_start_researcher: bool = False,
    auto_start_deliberation: bool = False,
    auto_start_conclusion: bool = False,
    auto_start_playwright: bool = False,
    channel_router: ChannelRouter | None = None,
    settings: Settings | None = None,
    runtime_model_guard: RuntimeModelGuard | None = None,
):
    try:
        import discord
        from discord.ext import commands
    except ImportError as exc:
        raise RuntimeError("Discord利用には `py -m pip install -r requirements.txt` が必要です") from exc

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
    router = channel_router or ChannelRouter()
    bot.channel_router = router
    managers = tuple(
        item
        for item in (
            manager,
            researcher_manager,
            deliberation_manager,
            conclusion_manager,
            playwright_manager,
        )
        if item is not None
    )
    if runtime_model_guard is None and settings is not None:
        runtime_model_guard = RuntimeModelGuard(
            managers,
            settings_loader=lambda: Settings.from_env(refresh_dotenv=True),
        )
    bot.runtime_model_guard = runtime_model_guard

    async def _safe_route_status(
        guild,
        workflow_id: str,
        layer: str,
        status: str,
    ) -> None:
        try:
            await route_layer_status(guild, workflow_id, layer, status)
        except Exception:
            logger.exception(
                "Failed to update layer status: layer=%s workflow=%s status=%s",
                layer,
                workflow_id,
                status,
            )

    async def _report_execution_error(
        ctx,
        layer: str,
        workflow_id: str,
        error: BaseException,
    ) -> None:
        await report_execution_error(
            ctx,
            layer,
            workflow_id,
            error,
            _safe_route_status,
        )

    async def _send_operational_error(
        ctx,
        prefix: str,
        error: BaseException,
    ) -> None:
        logger.error(
            "Discord command failed: %s",
            prefix,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            await ctx.send(f"{prefix}: {summarize_operational_error(error)}")
        except Exception:
            logger.exception("Failed to send Discord operational error summary")

    @bot.check
    async def require_routed_command_channel(ctx):
        command_name = getattr(getattr(ctx, "command", None), "name", "")
        try:
            return await router.require_command_channel(ctx, command_name)
        except ChannelRoutingError as exc:
            await _send_operational_error(ctx, "Channel routing failed", exc)
            return False

    @bot.event
    async def on_command_error(ctx, error):
        original = getattr(error, "original", error)
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(original, ChannelRoutingError):
            await _send_operational_error(ctx, "Channel routing failed", original)
            return
        await _send_operational_error(ctx, "Command failed", original)
        command_name = getattr(getattr(ctx, "command", None), "name", "")
        command_layer = {
            "deliberation": "Deliberation",
            "deliberation_resume": "Deliberation",
            "deliberation_revise": "Deliberation",
            "researcher": "Researcher",
            "researcher_accept": "Researcher",
            "researcher_accept_limitations": "Researcher",
            "researcher_revise": "Researcher",
            "researcher_revision_execute": "Researcher",
            "researcher_recover": "Researcher",
            "conclusion": "Conclusion",
            "conclusion_resume": "Conclusion",
            "conclusion_revise": "Conclusion",
            "conclusion_select": "Conclusion",
            "conclusion_integrate": "Conclusion",
            "playwright": "Playwright",
            "playwright_resume": "Playwright",
            "playwright_revise": "Playwright",
            "producer": "Producer",
            "producer_topic": "Producer",
            "producer_revise": "Producer",
        }.get(command_name)
        if command_layer is None:
            return
        args = getattr(ctx, "args", [])
        if len(args) >= 2 and isinstance(args[1], str):
            await _safe_route_status(ctx.guild, args[1], command_layer, "ERROR")

    @bot.event
    async def on_ready():
        print(f"ログインしました: {bot.user}")

    async def route_layer_status(guild, workflow_id: str, layer: str, status: str) -> None:
        marker = {
            "RUNNING": "🟡",
            "COMPLETED": "✅",
            "WAITING_HUMAN_SELECTION": "🟠",
            "WAITING_HUMAN_EVIDENCE_REVIEW": "🟠",
            "ERROR": "🔴",
            "FAILED": "🔴",
        }.get(status, "⚪")
        await router.send_chunks(
            guild,
            "status",
            f"PRDCP Workflow\nID: {workflow_id}\n\n{layer}\n{marker} {status}",
        )

    async def require_current_models(
        ctx,
        layer: str,
        *,
        workflow_id: str | None = None,
        operation: str | None = None,
    ) -> bool:
        if runtime_model_guard is None:
            return True
        try:
            runtime_model_guard.require_current(
                layer=layer,
                workflow_id=workflow_id,
                operation=operation,
            )
        except RuntimeModelDriftError as exc:
            await _send_operational_error(ctx, "Runtime model guard failed", exc)
            return False
        return True

    async def execute(ctx, topic: str | None = None):
        if not await require_current_models(ctx, "producer", operation="producer"):
            return
        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "producer", message)

        async with ctx.typing():
            state = await run_producer(manager, topic=topic, progress_callback=progress)
        await router.send_chunks(
            ctx.guild,
            "producer",
            format_result(state) if state.status == "COMPLETED" else format_status(state),
        )
        await route_layer_status(ctx.guild, state.workflow_id, "Producer", str(state.status))
        if state.status == "COMPLETED" and auto_start_researcher and researcher_manager is not None:
            if not await require_current_models(
                ctx,
                "researcher",
                workflow_id=state.workflow_id,
                operation="auto_start_researcher",
            ):
                return
            async def researcher_progress(message: str) -> None:
                await router.send_chunks(ctx.guild, "researcher", message)

            await route_layer_status(ctx.guild, state.workflow_id, "Researcher", "RUNNING")
            try:
                async with ctx.typing():
                    research_state = await run_researcher(
                        researcher_manager,
                        workflow_id=state.workflow_id,
                        progress_callback=researcher_progress,
                    )
            except Exception as exc:
                await _report_execution_error(ctx, "Researcher", state.workflow_id, exc)
                return
            await router.send_chunks(
                ctx.guild,
                "researcher",
                format_researcher_result(research_state)
                if research_state.status == "COMPLETED"
                else format_researcher_status(research_state),
            )
            await route_layer_status(
                ctx.guild,
                state.workflow_id,
                "Researcher",
                str(research_state.status),
            )
            if research_state.status == "COMPLETED" and research_state.research_report:
                await router.send_chunks(
                    ctx.guild,
                    "sources",
                    format_researcher_sources(research_state.research_report),
                )
            if (
                research_state.status == "COMPLETED"
                and auto_start_deliberation
                and deliberation_manager is not None
            ):
                if not await require_current_models(
                    ctx,
                    "deliberation",
                    workflow_id=state.workflow_id,
                    operation="auto_start_deliberation",
                ):
                    return
                async def deliberation_progress(message: str) -> None:
                    await router.send_chunks(ctx.guild, "deliberation", message)

                await route_layer_status(ctx.guild, state.workflow_id, "Deliberation", "RUNNING")
                try:
                    async with ctx.typing():
                        deliberation_state = await run_deliberation(
                            deliberation_manager,
                            workflow_id=state.workflow_id,
                            progress_callback=deliberation_progress,
                        )
                except Exception as exc:
                    await _report_execution_error(
                        ctx, "Deliberation", state.workflow_id, exc
                    )
                    return
                await router.send_chunks(
                    ctx.guild,
                    "deliberation",
                    format_deliberation_result(deliberation_state)
                    if deliberation_state.status == "COMPLETED"
                    else format_deliberation_status(deliberation_state),
                )
                await route_layer_status(
                    ctx.guild,
                    state.workflow_id,
                    "Deliberation",
                    str(deliberation_state.status),
                )
                if (
                    deliberation_state.status == "COMPLETED"
                    and auto_start_conclusion
                    and conclusion_manager is not None
                ):
                    if not await require_current_models(
                        ctx,
                        "conclusion",
                        workflow_id=state.workflow_id,
                        operation="auto_start_conclusion",
                    ):
                        return
                    async def conclusion_progress(message: str) -> None:
                        await router.send_chunks(ctx.guild, "conclusion", message)

                    await route_layer_status(ctx.guild, state.workflow_id, "Conclusion", "RUNNING")
                    try:
                        async with ctx.typing():
                            conclusion_state = await run_conclusion(
                                conclusion_manager,
                                workflow_id=state.workflow_id,
                                progress_callback=conclusion_progress,
                            )
                    except Exception as exc:
                        await _report_execution_error(
                            ctx, "Conclusion", state.workflow_id, exc
                        )
                        return
                    await router.send_chunks(
                        ctx.guild,
                        "conclusion",
                        format_conclusion_options(conclusion_state)
                        if conclusion_state.status == "WAITING_HUMAN_SELECTION"
                        else format_conclusion_status(conclusion_state),
                    )
                    await route_layer_status(
                        ctx.guild,
                        state.workflow_id,
                        "Conclusion",
                        str(conclusion_state.status),
                    )

    @bot.command(name="producer")
    async def producer_command(ctx):
        await execute(ctx)

    @bot.command(name="producer_topic")
    async def producer_topic_command(ctx, *, topic: str = ""):
        if not topic.strip():
            await ctx.send("使い方: !producer_topic <Topic>")
            return
        await execute(ctx, topic.strip())

    @bot.command(name="producer_status")
    async def producer_status_command(ctx, workflow_id: str = ""):
        if not workflow_id:
            await ctx.send("使い方: !producer_status <workflow_id>")
            return
        try:
            state = load_producer_status(manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Workflowが見つかりません: {workflow_id}")
            return
        await router.send_chunks(ctx.guild, "producer", format_status(state))

    @bot.command(name="producer_revise")
    async def producer_revise_command(
        ctx,
        workflow_id: str = "",
        *,
        reason: str = "Discord operator authorized one Producer internal Revision cycle",
    ):
        if not workflow_id:
            await ctx.send("使い方: !producer_revise <workflow_id> [reason]")
            return
        try:
            state = await revise_producer(
                manager,
                workflow_id=workflow_id,
                actor_id=f"discord.user.{getattr(ctx.author, 'id', 'unknown')}",
                reason=reason,
            )
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Producer Revision failed", exc)
            return
        await router.send_chunks(ctx.guild, "producer", format_status(state))
        await route_layer_status(
            ctx.guild, workflow_id, "Producer", str(state.status)
        )

    @bot.command(name="runtime_models")
    async def runtime_models_command(ctx, layer: str = ""):
        if runtime_model_guard is None:
            await ctx.send("Runtime model auditing is not configured")
            return
        try:
            audit = runtime_model_guard.inspect(layer=layer or None)
        except ValueError as exc:
            await _send_operational_error(ctx, "Runtime model audit failed", exc)
            return
        await ctx.send(format_runtime_model_audit(audit))

    @bot.command(name="researcher")
    async def researcher_command(ctx, workflow_id: str = ""):
        if researcher_manager is None:
            await ctx.send("Researcher Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !researcher <workflow_id>")
            return
        if not await require_current_models(
            ctx,
            "researcher",
            workflow_id=workflow_id,
            operation="researcher",
        ):
            return

        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "researcher", message)

        try:
            await route_layer_status(ctx.guild, workflow_id, "Researcher", "RUNNING")
            async with ctx.typing():
                state = await run_researcher(
                    researcher_manager,
                    workflow_id=workflow_id,
                    progress_callback=progress,
                )
        except Exception as exc:
            await _report_execution_error(ctx, "Researcher", workflow_id, exc)
            return
        await router.send_chunks(
            ctx.guild,
            "researcher",
            format_researcher_result(state)
            if state.status == "COMPLETED"
            else format_researcher_status(state),
        )
        await route_layer_status(ctx.guild, workflow_id, "Researcher", str(state.status))
        if state.status == "COMPLETED" and state.research_report:
            await router.send_chunks(
                ctx.guild,
                "sources",
                format_researcher_sources(state.research_report),
            )

    @bot.command(name="researcher_status")
    async def researcher_status_command(ctx, workflow_id: str = ""):
        if researcher_manager is None:
            await ctx.send("Researcher Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !researcher_status <workflow_id>")
            return
        try:
            state = load_researcher_status(researcher_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Researcher Workflowが見つかりません: {workflow_id}")
            return
        await router.send_chunks(ctx.guild, "researcher", format_researcher_status(state))

    @bot.command(name="researcher_result")
    async def researcher_result_command(ctx, workflow_id: str = ""):
        if researcher_manager is None:
            await ctx.send("Researcher Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !researcher_result <workflow_id>")
            return
        try:
            report = load_researcher_result(researcher_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Research Reportが見つかりません: {workflow_id}")
            return
        await router.send_chunks(
            ctx.guild,
            "researcher",
            "```json\n" + report.model_dump_json(indent=2) + "\n```",
        )

    @bot.command(name="researcher_evidence")
    async def researcher_evidence_command(ctx, workflow_id: str = ""):
        if researcher_manager is None:
            await ctx.send("Researcher Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !researcher_evidence <workflow_id>")
            return
        try:
            summary = inspect_researcher_evidence(researcher_manager, workflow_id)
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Researcher evidence inspection failed", exc)
            return
        await router.send_chunks(
            ctx.guild, "researcher", format_researcher_evidence(summary)
        )

    async def decide_researcher_gate(ctx, workflow_id, decision, reason):
        if researcher_manager is None:
            await ctx.send("Researcher Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("workflow_idを指定してください")
            return
        try:
            state = decide_researcher_evidence(
                researcher_manager,
                workflow_id,
                decision,
                reason=reason,
            )
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Researcher Human Evidence decision failed", exc)
            return
        await router.send_chunks(
            ctx.guild, "researcher", format_researcher_status(state)
        )
        await route_layer_status(
            ctx.guild, workflow_id, "Researcher", str(state.status)
        )

    @bot.command(name="researcher_accept")
    async def researcher_accept_command(
        ctx, workflow_id: str = "", *, reason: str = "Human Operator accepted the evidence stopping point"
    ):
        await decide_researcher_gate(
            ctx, workflow_id, HumanEvidenceDecisionType.ACCEPT, reason
        )

    @bot.command(name="researcher_accept_limitations")
    async def researcher_accept_limitations_command(
        ctx, workflow_id: str = "", *, reason: str = "Human Operator accepted disclosed evidence gaps as unresolved limitations"
    ):
        await decide_researcher_gate(
            ctx,
            workflow_id,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS,
            reason,
        )

    @bot.command(name="researcher_revise")
    async def researcher_revise_command(
        ctx, workflow_id: str = "", *, reason: str = "Human Operator requested additional evidence research"
    ):
        await decide_researcher_gate(
            ctx, workflow_id, HumanEvidenceDecisionType.REVISE, reason
        )

    @bot.command(name="researcher_revision_execute")
    async def researcher_revision_execute_command(
        ctx,
        workflow_id: str = "",
        *,
        reason: str = "Discord operator authorized one Researcher evidence Revision cycle",
    ):
        if researcher_manager is None:
            await ctx.send("Researcher Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send(
                "使い方: !researcher_revision_execute <workflow_id> [reason]"
            )
            return
        try:
            state = await execute_researcher_revision(
                researcher_manager,
                workflow_id=workflow_id,
                actor_id=f"discord.user.{getattr(ctx.author, 'id', 'unknown')}",
                reason=reason,
            )
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Researcher Revision execution failed", exc)
            return
        await router.send_chunks(
            ctx.guild, "researcher", format_researcher_status(state)
        )
        await route_layer_status(
            ctx.guild, workflow_id, "Researcher", str(state.status)
        )

    @bot.command(name="researcher_recover")
    async def researcher_recover_command(ctx, workflow_id: str = ""):
        if researcher_manager is None:
            await ctx.send("Researcher Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !researcher_recover <workflow_id>")
            return
        try:
            state = recover_researcher_evidence(researcher_manager, workflow_id)
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Researcher Human Evidence recovery failed", exc)
            return
        await router.send_chunks(
            ctx.guild, "researcher", format_researcher_status(state)
        )

    async def execute_deliberation(ctx, workflow_id: str, *, resume: bool = False):
        if deliberation_manager is None:
            await ctx.send("Deliberation Managerが構成されていません")
            return
        if not await require_current_models(
            ctx,
            "deliberation",
            workflow_id=workflow_id,
            operation="deliberation_resume" if resume else "deliberation",
        ):
            return

        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "deliberation", message)

        try:
            await route_layer_status(ctx.guild, workflow_id, "Deliberation", "RUNNING")
            async with ctx.typing():
                state = await (
                    resume_deliberation(
                        deliberation_manager,
                        workflow_id=workflow_id,
                        progress_callback=progress,
                    )
                    if resume
                    else run_deliberation(
                        deliberation_manager,
                        workflow_id=workflow_id,
                        progress_callback=progress,
                    )
                )
        except Exception as exc:
            await _report_execution_error(ctx, "Deliberation", workflow_id, exc)
            return
        await router.send_chunks(
            ctx.guild,
            "deliberation",
            format_deliberation_result(state)
            if state.status == "COMPLETED"
            else format_deliberation_status(state),
        )
        await route_layer_status(ctx.guild, workflow_id, "Deliberation", str(state.status))

    @bot.command(name="deliberation")
    async def deliberation_command(ctx, workflow_id: str = ""):
        if not workflow_id:
            await ctx.send("使い方: !deliberation <workflow_id>")
            return
        await execute_deliberation(ctx, workflow_id)

    @bot.command(name="deliberation_status")
    async def deliberation_status_command(ctx, workflow_id: str = ""):
        if deliberation_manager is None:
            await ctx.send("Deliberation Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !deliberation_status <workflow_id>")
            return
        try:
            state = load_deliberation_status(deliberation_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Deliberation Workflowが見つかりません: {workflow_id}")
            return
        await router.send_chunks(ctx.guild, "deliberation", format_deliberation_status(state))

    @bot.command(name="deliberation_result")
    async def deliberation_result_command(ctx, workflow_id: str = ""):
        if deliberation_manager is None:
            await ctx.send("Deliberation Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !deliberation_result <workflow_id>")
            return
        try:
            result = load_deliberation_result(deliberation_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Deliberation Resultが見つかりません: {workflow_id}")
            return
        await router.send_chunks(
            ctx.guild,
            "deliberation",
            "```json\n" + result.model_dump_json(indent=2) + "\n```",
        )

    @bot.command(name="deliberation_resume")
    async def deliberation_resume_command(ctx, workflow_id: str = ""):
        if not workflow_id:
            await ctx.send("使い方: !deliberation_resume <workflow_id>")
            return
        await execute_deliberation(ctx, workflow_id, resume=True)

    @bot.command(name="deliberation_revise")
    async def deliberation_revise_command(
        ctx,
        workflow_id: str = "",
        *,
        reason: str = "Discord operator authorized one Deliberation Revision cycle",
    ):
        if deliberation_manager is None:
            await ctx.send("Deliberation Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !deliberation_revise <workflow_id> [reason]")
            return

        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "deliberation", message)

        try:
            state = await revise_deliberation(
                deliberation_manager,
                workflow_id=workflow_id,
                actor_id=f"discord.user.{getattr(ctx.author, 'id', 'unknown')}",
                reason=reason,
                progress_callback=progress,
            )
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Deliberation Revision failed", exc)
            return
        await router.send_chunks(
            ctx.guild,
            "deliberation",
            format_deliberation_result(state)
            if state.status == "COMPLETED"
            else format_deliberation_status(state),
        )
        await route_layer_status(
            ctx.guild, workflow_id, "Deliberation", str(state.status)
        )

    async def execute_conclusion(ctx, workflow_id: str, *, resume: bool = False):
        if conclusion_manager is None:
            await ctx.send("Conclusion Managerが構成されていません")
            return
        if not await require_current_models(
            ctx,
            "conclusion",
            workflow_id=workflow_id,
            operation="conclusion_resume" if resume else "conclusion",
        ):
            return

        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "conclusion", message)

        try:
            await route_layer_status(ctx.guild, workflow_id, "Conclusion", "RUNNING")
            async with ctx.typing():
                state = await (
                    resume_conclusion(
                        conclusion_manager,
                        workflow_id=workflow_id,
                        progress_callback=progress,
                    )
                    if resume
                    else run_conclusion(
                        conclusion_manager,
                        workflow_id=workflow_id,
                        progress_callback=progress,
                    )
                )
        except Exception as exc:
            await _report_execution_error(ctx, "Conclusion", workflow_id, exc)
            return
        await router.send_chunks(
            ctx.guild,
            "conclusion",
            format_conclusion_options(state)
            if state.status == "WAITING_HUMAN_SELECTION"
            else format_conclusion_status(state),
        )
        await route_layer_status(ctx.guild, workflow_id, "Conclusion", str(state.status))

    @bot.command(name="conclusion")
    async def conclusion_command(ctx, workflow_id: str = ""):
        if not workflow_id:
            await ctx.send("使い方: !conclusion <workflow_id>")
            return
        await execute_conclusion(ctx, workflow_id)

    @bot.command(name="conclusion_status")
    async def conclusion_status_command(ctx, workflow_id: str = ""):
        if conclusion_manager is None:
            await ctx.send("Conclusion Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !conclusion_status <workflow_id>")
            return
        try:
            state = load_conclusion_status(conclusion_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Conclusion Workflowが見つかりません: {workflow_id}")
            return
        await router.send_chunks(ctx.guild, "conclusion", format_conclusion_status(state))

    @bot.command(name="conclusion_options")
    async def conclusion_options_command(ctx, workflow_id: str = ""):
        if conclusion_manager is None:
            await ctx.send("Conclusion Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !conclusion_options <workflow_id>")
            return
        try:
            state = load_conclusion_status(conclusion_manager, workflow_id)
            load_conclusion_package(conclusion_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Conclusion Packageが見つかりません: {workflow_id}")
            return
        await router.send_chunks(ctx.guild, "conclusion", format_conclusion_options(state))

    @bot.command(name="conclusion_select")
    async def conclusion_select_command(ctx, workflow_id: str = "", candidate_id: str = ""):
        if conclusion_manager is None:
            await ctx.send("Conclusion Managerが構成されていません")
            return
        if not workflow_id or not candidate_id:
            await ctx.send("使い方: !conclusion_select <workflow_id> <candidate_id>")
            return
        try:
            state = select_conclusion(
                conclusion_manager,
                workflow_id=workflow_id,
                candidate_id=candidate_id,
            )
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Conclusion selection failed", exc)
            return
        await router.send_chunks(ctx.guild, "conclusion", format_conclusion_result(state))
        await route_layer_status(ctx.guild, workflow_id, "Conclusion", str(state.status))
        if state.status == "COMPLETED" and auto_start_playwright and playwright_manager is not None:
            await execute_playwright(ctx, workflow_id)

    @bot.command(name="conclusion_integrate")
    async def conclusion_integrate_command(ctx, workflow_id: str = "", *candidate_ids: str):
        if conclusion_manager is None:
            await ctx.send("Conclusion Managerが構成されていません")
            return
        if not workflow_id or len(candidate_ids) < 2:
            await ctx.send("使い方: !conclusion_integrate <workflow_id> <candidate_id_1> <candidate_id_2> [...]")
            return
        if not await require_current_models(
            ctx,
            "conclusion",
            workflow_id=workflow_id,
            operation="conclusion_integrate",
        ):
            return

        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "conclusion", message)

        try:
            async with ctx.typing():
                state = await integrate_conclusion_candidates(
                    conclusion_manager,
                    workflow_id=workflow_id,
                    candidate_ids=list(candidate_ids),
                    progress_callback=progress,
                )
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Conclusion integration failed", exc)
            return
        await router.send_chunks(ctx.guild, "conclusion", format_conclusion_options(state))
        await route_layer_status(ctx.guild, workflow_id, "Conclusion", str(state.status))

    @bot.command(name="conclusion_result")
    async def conclusion_result_command(ctx, workflow_id: str = ""):
        if conclusion_manager is None:
            await ctx.send("Conclusion Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !conclusion_result <workflow_id>")
            return
        try:
            result = load_final_conclusion(conclusion_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Final Conclusionが見つかりません: {workflow_id}")
            return
        await router.send_chunks(
            ctx.guild,
            "conclusion",
            "```json\n" + result.model_dump_json(indent=2) + "\n```",
        )

    @bot.command(name="conclusion_resume")
    async def conclusion_resume_command(ctx, workflow_id: str = ""):
        if not workflow_id:
            await ctx.send("使い方: !conclusion_resume <workflow_id>")
            return
        await execute_conclusion(ctx, workflow_id, resume=True)

    @bot.command(name="conclusion_revise")
    async def conclusion_revise_command(
        ctx,
        workflow_id: str = "",
        *,
        reason: str = "Discord operator authorized Conclusion Revision",
    ):
        if conclusion_manager is None:
            await ctx.send("Conclusion Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !conclusion_revise <workflow_id> [reason]")
            return

        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "conclusion", message)

        try:
            state = await revise_conclusion(
                conclusion_manager,
                workflow_id=workflow_id,
                actor_id=f"discord.user.{getattr(ctx.author, 'id', 'unknown')}",
                reason=reason,
                progress_callback=progress,
            )
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Conclusion Revision failed", exc)
            return
        await router.send_chunks(
            ctx.guild,
            "conclusion",
            format_conclusion_options(state)
            if state.status == "WAITING_HUMAN_SELECTION"
            else format_conclusion_status(state),
        )
        await route_layer_status(ctx.guild, workflow_id, "Conclusion", str(state.status))

    async def execute_playwright(ctx, workflow_id: str, *, resume: bool = False):
        if playwright_manager is None:
            await ctx.send("Playwright Managerが構成されていません")
            return
        if not await require_current_models(
            ctx,
            "playwright",
            workflow_id=workflow_id,
            operation="playwright_resume" if resume else "playwright",
        ):
            return

        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "playwright", message)

        try:
            await route_layer_status(ctx.guild, workflow_id, "Playwright", "RUNNING")
            async with ctx.typing():
                state = await (
                    resume_playwright(
                        playwright_manager,
                        workflow_id=workflow_id,
                        actor_id=f"discord.user.{getattr(ctx.author, 'id', 'unknown')}",
                        progress_callback=progress,
                    )
                    if resume
                    else run_playwright(
                        playwright_manager,
                        workflow_id=workflow_id,
                        progress_callback=progress,
                    )
                )
        except Exception as exc:
            await _report_execution_error(ctx, "Playwright", workflow_id, exc)
            return
        await router.send_chunks(
            ctx.guild,
            "playwright",
            format_playwright_status(state),
        )
        if state.status == "COMPLETED":
            await router.send_chunks(
                ctx.guild,
                "deliveries",
                format_playwright_result(state),
            )
        await route_layer_status(ctx.guild, workflow_id, "Playwright", str(state.status))

    @bot.command(name="playwright")
    async def playwright_command(ctx, workflow_id: str = ""):
        if not workflow_id:
            await ctx.send("使い方: !playwright <workflow_id>")
            return
        await execute_playwright(ctx, workflow_id)

    @bot.command(name="playwright_status")
    async def playwright_status_command(ctx, workflow_id: str = ""):
        if playwright_manager is None:
            await ctx.send("Playwright Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !playwright_status <workflow_id>")
            return
        try:
            state = load_playwright_status(playwright_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Playwright Workflowが見つかりません: {workflow_id}")
            return
        await router.send_chunks(ctx.guild, "playwright", format_playwright_status(state))

    @bot.command(name="playwright_script")
    async def playwright_script_command(ctx, workflow_id: str = ""):
        if playwright_manager is None:
            await ctx.send("Playwright Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !playwright_script <workflow_id>")
            return
        try:
            package = load_playwright_result(playwright_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Final Script Packageが見つかりません: {workflow_id}")
            return
        lines = [f"# {package.title_candidates[0]}", ""]
        for section in package.script.sections:
            lines.extend([f"## {section.heading}", ""])
            for paragraph in section.paragraphs:
                lines.extend([paragraph.speaker_text, ""])
        await router.send_chunks(ctx.guild, "playwright", "\n".join(lines).rstrip())

    @bot.command(name="playwright_citations")
    async def playwright_citations_command(ctx, workflow_id: str = ""):
        if playwright_manager is None:
            await ctx.send("Playwright Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !playwright_citations <workflow_id>")
            return
        try:
            package = load_playwright_result(playwright_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Citation Manifestが見つかりません: {workflow_id}")
            return
        await router.send_chunks(
            ctx.guild,
            "playwright",
            "```json\n" + package.citation_manifest.model_dump_json(indent=2) + "\n```",
        )

    @bot.command(name="playwright_visuals")
    async def playwright_visuals_command(ctx, workflow_id: str = ""):
        if playwright_manager is None:
            await ctx.send("Playwright Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !playwright_visuals <workflow_id>")
            return
        try:
            package = load_playwright_result(playwright_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Visual Planが見つかりません: {workflow_id}")
            return
        await router.send_chunks(
            ctx.guild,
            "playwright",
            "```json\n" + package.visual_plan.model_dump_json(indent=2) + "\n```",
        )

    @bot.command(name="playwright_result")
    async def playwright_result_command(ctx, workflow_id: str = ""):
        if playwright_manager is None:
            await ctx.send("Playwright Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !playwright_result <workflow_id>")
            return
        try:
            state = load_playwright_status(playwright_manager, workflow_id)
            load_playwright_result(playwright_manager, workflow_id)
        except FileNotFoundError:
            await ctx.send(f"Final Script Packageが見つかりません: {workflow_id}")
            return
        await router.send_chunks(ctx.guild, "playwright", format_playwright_result(state))

    @bot.command(name="playwright_resume")
    async def playwright_resume_command(ctx, workflow_id: str = ""):
        if not workflow_id:
            await ctx.send("使い方: !playwright_resume <workflow_id>")
            return
        await execute_playwright(ctx, workflow_id, resume=True)

    @bot.command(name="playwright_revise")
    async def playwright_revise_command(
        ctx,
        workflow_id: str = "",
        *,
        reason: str = "Discord operator authorized Playwright Revision",
    ):
        if playwright_manager is None:
            await ctx.send("Playwright Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !playwright_revise <workflow_id> [reason]")
            return

        async def progress(message: str) -> None:
            await router.send_chunks(ctx.guild, "playwright", message)

        try:
            state = await revise_playwright(
                playwright_manager,
                workflow_id=workflow_id,
                actor_id=f"discord.user.{getattr(ctx.author, 'id', 'unknown')}",
                reason=reason,
                progress_callback=progress,
            )
        except (FileNotFoundError, ValueError) as exc:
            await _send_operational_error(ctx, "Playwright Revision failed", exc)
            return
        await router.send_chunks(
            ctx.guild,
            "playwright",
            format_playwright_status(state),
        )
        if state.status == "COMPLETED":
            await router.send_chunks(
                ctx.guild,
                "deliveries",
                format_playwright_result(state),
            )
        await route_layer_status(ctx.guild, workflow_id, "Playwright", str(state.status))

    return bot
