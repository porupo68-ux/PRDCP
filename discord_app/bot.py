"""Canonical router-aware Discord bot entry point for all PRDCP layers."""

from __future__ import annotations

from conclusion.manager import ConclusionManager
from deliberation.manager import DeliberationManager
from discord_app.channel_router import ChannelRouter, ChannelRoutingError
from discord_app.commands import (
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
    resume_deliberation,
    resume_playwright,
    run_conclusion,
    run_deliberation,
    run_playwright,
    run_producer,
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
    format_researcher_sources,
    format_researcher_status,
    format_result,
    format_status,
)
from playwright.manager import PlaywrightManager
from producer.manager import ProducerManager
from researcher.manager import ResearcherManager


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

    @bot.check
    async def require_routed_command_channel(ctx):
        command_name = getattr(getattr(ctx, "command", None), "name", "")
        try:
            return await router.require_command_channel(ctx, command_name)
        except ChannelRoutingError as exc:
            await ctx.send(f"ChannelRoutingError:\n{exc}")
            return False

    @bot.event
    async def on_command_error(ctx, error):
        original = getattr(error, "original", error)
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(original, ChannelRoutingError):
            await ctx.send(f"ChannelRoutingError:\n{original}")
            return
        raise error

    @bot.event
    async def on_ready():
        print(f"ログインしました: {bot.user}")

    async def route_layer_status(guild, workflow_id: str, layer: str, status: str) -> None:
        marker = {
            "RUNNING": "🟡",
            "COMPLETED": "✅",
            "WAITING_HUMAN_SELECTION": "🟠",
            "ERROR": "🔴",
            "FAILED": "🔴",
        }.get(status, "⚪")
        await router.send_chunks(
            guild,
            "status",
            f"PRDCP Workflow\nID: {workflow_id}\n\n{layer}\n{marker} {status}",
        )

    async def execute(ctx, topic: str | None = None):
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
            async def researcher_progress(message: str) -> None:
                await router.send_chunks(ctx.guild, "researcher", message)

            await route_layer_status(ctx.guild, state.workflow_id, "Researcher", "RUNNING")
            async with ctx.typing():
                research_state = await run_researcher(
                    researcher_manager,
                    workflow_id=state.workflow_id,
                    progress_callback=researcher_progress,
                )
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
                async def deliberation_progress(message: str) -> None:
                    await router.send_chunks(ctx.guild, "deliberation", message)

                await route_layer_status(ctx.guild, state.workflow_id, "Deliberation", "RUNNING")
                async with ctx.typing():
                    deliberation_state = await run_deliberation(
                        deliberation_manager,
                        workflow_id=state.workflow_id,
                        progress_callback=deliberation_progress,
                    )
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
                    async def conclusion_progress(message: str) -> None:
                        await router.send_chunks(ctx.guild, "conclusion", message)

                    await route_layer_status(ctx.guild, state.workflow_id, "Conclusion", "RUNNING")
                    async with ctx.typing():
                        conclusion_state = await run_conclusion(
                            conclusion_manager,
                            workflow_id=state.workflow_id,
                            progress_callback=conclusion_progress,
                        )
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

    @bot.command(name="researcher")
    async def researcher_command(ctx, workflow_id: str = ""):
        if researcher_manager is None:
            await ctx.send("Researcher Managerが構成されていません")
            return
        if not workflow_id:
            await ctx.send("使い方: !researcher <workflow_id>")
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
        except FileNotFoundError:
            await ctx.send(f"ProducerのResearch Planが見つかりません: {workflow_id}")
            await route_layer_status(ctx.guild, workflow_id, "Researcher", "ERROR")
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

    async def execute_deliberation(ctx, workflow_id: str, *, resume: bool = False):
        if deliberation_manager is None:
            await ctx.send("Deliberation Managerが構成されていません")
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
        except (FileNotFoundError, ValueError) as exc:
            await ctx.send(str(exc))
            await route_layer_status(ctx.guild, workflow_id, "Deliberation", "ERROR")
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

    async def execute_conclusion(ctx, workflow_id: str, *, resume: bool = False):
        if conclusion_manager is None:
            await ctx.send("Conclusion Managerが構成されていません")
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
        except (FileNotFoundError, ValueError) as exc:
            await ctx.send(str(exc))
            await route_layer_status(ctx.guild, workflow_id, "Conclusion", "ERROR")
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
            await ctx.send(str(exc))
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
            await ctx.send(str(exc))
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

    async def execute_playwright(ctx, workflow_id: str, *, resume: bool = False):
        if playwright_manager is None:
            await ctx.send("Playwright Managerが構成されていません")
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
                        progress_callback=progress,
                    )
                    if resume
                    else run_playwright(
                        playwright_manager,
                        workflow_id=workflow_id,
                        progress_callback=progress,
                    )
                )
        except (FileNotFoundError, ValueError) as exc:
            await ctx.send(str(exc))
            await route_layer_status(ctx.guild, workflow_id, "Playwright", "ERROR")
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

    return bot
