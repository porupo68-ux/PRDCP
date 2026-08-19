from __future__ import annotations

import asyncio
import sys
import traceback
from typing import Any

from cli_app.arguments import parse_args
from cli_app.diagnostics import print_doctor_report, run_doctor
from cli_app.events import ProgressReporter
from cli_app.output import (
    format_human_evidence_summary,
    load_workflow_states,
    print_state,
)
from common.logger import configure_logging
from common.runtime_models import RuntimeModelGuard
from researcher.schemas.human_evidence import (
    HumanActorSource,
    HumanEvidenceDecisionType,
)
from config.settings import Settings, apply_runtime_overrides
from runtime import (
    build_all_managers,
    build_managers,
    build_producer_manager,
    build_researcher_manager,
)


def configure_cli_text_streams() -> None:
    """Make Japanese CLI output deterministic across Windows code pages."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


async def run_demo(
    settings: Settings,
    topic: str | None,
    *,
    run_researcher: bool = False,
    run_deliberation: bool = False,
    run_conclusion: bool = False,
    run_playwright: bool = False,
    json_output: bool = False,
) -> int:
    managers = build_all_managers(settings)
    producer, researcher, deliberation, conclusion, playwright = managers

    state = await producer.start(
        user_topic=topic,
        progress_callback=ProgressReporter("producer", settings.data_dir),
    )
    print_state("producer", state, json_output=json_output)
    if state.status != "COMPLETED" or not run_researcher:
        return 0 if state.status == "COMPLETED" else 1

    workflow_id = state.workflow_id
    research_state = await researcher.start(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", research_state, json_output=json_output)
    if (
        research_state.status == "WAITING_HUMAN_EVIDENCE_REVIEW"
        and run_deliberation
        and settings.provider == "mock"
    ):
        summary = researcher.inspect_human_evidence_gate(workflow_id)
        mock_decision = (
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
            if summary.evidence_sufficiency_findings
            else HumanEvidenceDecisionType.ACCEPT
        )
        print(f"[researcher] Mock Human Evidence Decision: {mock_decision.value}")
        research_state = researcher.decide_human_evidence(
            workflow_id,
            mock_decision,
            reason="Explicit Mock E2E Human Evidence Gate fixture",
            actor_source=HumanActorSource.MOCK_FIXTURE,
        )
        print_state("researcher", research_state, json_output=json_output)
    if research_state.status != "COMPLETED" or not run_deliberation:
        return 0 if research_state.status == "COMPLETED" else 1

    deliberation_state = await deliberation.start(
        workflow_id,
        progress_callback=ProgressReporter("deliberation", settings.data_dir, workflow_id),
    )
    print_state("deliberation", deliberation_state, json_output=json_output)
    if deliberation_state.status != "COMPLETED" or not run_conclusion:
        return 0 if deliberation_state.status == "COMPLETED" else 1

    conclusion_state = await conclusion.start(
        workflow_id,
        progress_callback=ProgressReporter("conclusion", settings.data_dir, workflow_id),
    )
    print_state("conclusion", conclusion_state, json_output=json_output)
    if conclusion_state.status != "WAITING_HUMAN_SELECTION" or not run_playwright:
        return 0 if conclusion_state.status == "WAITING_HUMAN_SELECTION" else 1

    selected_id = conclusion_state.position_candidates[0]["position_candidate_id"]
    print(f"[conclusion] E2E auto-selection: {selected_id}")
    conclusion_state = conclusion.select(workflow_id, [selected_id])
    print_state("conclusion", conclusion_state, json_output=json_output)
    if conclusion_state.status != "COMPLETED":
        return 1

    playwright_state = await playwright.start(
        workflow_id,
        progress_callback=ProgressReporter("playwright", settings.data_dir, workflow_id),
    )
    print_state("playwright", playwright_state, json_output=json_output)
    return 0 if playwright_state.status == "COMPLETED" else 1


async def recover_saved_producer(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    producer = build_producer_manager(settings)
    state = await producer.recover(
        workflow_id,
        progress_callback=ProgressReporter("producer", settings.data_dir, workflow_id),
    )
    print_state("producer", state, json_output=json_output)
    return 0 if state.status in {"COMPLETED", "WAITING_HUMAN_EVIDENCE_REVIEW"} else 1


def inspect_saved_researcher_evidence(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    researcher = build_researcher_manager(settings)
    summary = researcher.inspect_human_evidence_gate(workflow_id)
    if json_output:
        print(summary.model_dump_json(indent=2))
    else:
        print(format_human_evidence_summary(summary))
    return 0 if summary.eligible else 1


def decide_saved_researcher_evidence(
    settings: Settings,
    workflow_id: str,
    *,
    decision: HumanEvidenceDecisionType,
    reason: str | None,
    json_output: bool,
) -> int:
    researcher = build_researcher_manager(settings)
    default_reasons = {
        HumanEvidenceDecisionType.ACCEPT: (
            "Human Operator accepted the current evidence stopping point"
        ),
        HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS: (
            "Human Operator accepted all disclosed Evidence Sufficiency Findings "
            "as unresolved limitations"
        ),
        HumanEvidenceDecisionType.REVISE: (
            "Human Operator requested additional evidence research"
        ),
    }
    state = researcher.decide_human_evidence(
        workflow_id,
        decision,
        reason=reason or default_reasons[decision],
        actor_source=HumanActorSource.CLI,
    )
    print_state("researcher", state, json_output=json_output)
    if decision == HumanEvidenceDecisionType.REVISE:
        summary = researcher.inspect_human_evidence_gate(workflow_id)
        if not json_output:
            print(format_human_evidence_summary(summary))
        return 0
    return 0 if state.status in {"COMPLETED", "COMPLETED_REVISION"} else 1


def recover_saved_researcher_human_gate(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    researcher = build_researcher_manager(settings)
    state = researcher.recover_human_evidence_gate(workflow_id)
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status in {
        "WAITING_HUMAN_EVIDENCE_REVIEW",
        "COMPLETED",
        "COMPLETED_REVISION",
        "BLOCKED",
    } else 1


async def retry_saved_producer_provider(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--producer-provider-retry requires Demo Safe Mode to remain enabled"
        )
    producer = build_producer_manager(settings)
    state = await producer.retry_provider_call(
        workflow_id,
        progress_callback=ProgressReporter("producer", settings.data_dir, workflow_id),
    )
    print_state("producer", state, json_output=json_output)
    return 0 if state.status == "RUNNING" else 1


async def repair_saved_producer_output(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--producer-output-repair requires Demo Safe Mode to remain enabled"
        )
    producer = build_producer_manager(settings)
    state = await producer.repair_provider_output(
        workflow_id,
        progress_callback=ProgressReporter(
            "producer", settings.data_dir, workflow_id
        ),
    )
    print_state("producer", state, json_output=json_output)
    return 0 if state.status == "RUNNING" else 1


async def run_saved_researcher(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    _producer, researcher, _deliberation = build_managers(settings)
    state = await researcher.start(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status in {"COMPLETED", "WAITING_HUMAN_EVIDENCE_REVIEW"} else 1


async def run_saved_researcher_task(
    settings: Settings,
    workflow_id: str,
    task_id: str,
    *,
    json_output: bool,
) -> int:
    researcher = build_researcher_manager(settings)
    state = await researcher.run_task(
        workflow_id,
        task_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "RUNNING" else 1


async def resume_saved_researcher(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    researcher = build_researcher_manager(settings)
    state = await researcher.resume(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "COMPLETED_REVISION" else 1


async def retry_saved_researcher_provider(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--researcher-provider-retry requires Demo Safe Mode to remain enabled"
        )
    researcher = build_researcher_manager(settings)
    state = await researcher.retry_provider_call(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "COMPLETED_REVISION" else 1


async def repair_saved_researcher_runtime_models(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--researcher-runtime-model-repair requires Demo Safe Mode to remain enabled"
        )
    researcher = build_researcher_manager(settings)
    RuntimeModelGuard(
        (researcher,),
        settings_loader=lambda: settings,
    ).require_current(
        layer="researcher",
        workflow_id=workflow_id,
        operation="researcher_runtime_model_repair",
    )
    state = await researcher.recover_runtime_model_drift(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def reconstruct_saved_researcher_retrieval(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--researcher-retrieval-reconstruct requires Demo Safe Mode to remain enabled"
        )
    researcher = build_researcher_manager(settings)
    RuntimeModelGuard(
        (researcher,),
        settings_loader=lambda: settings,
    ).require_current(
        layer="researcher",
        workflow_id=workflow_id,
        operation="researcher_retrieval_reconstruct",
    )
    state = await researcher.reconstruct_missing_retrieval_and_recover(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def repair_saved_researcher_runtime_output(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--researcher-runtime-output-repair requires Demo Safe Mode to remain enabled"
        )
    researcher = build_researcher_manager(settings)
    RuntimeModelGuard(
        (researcher,),
        settings_loader=lambda: settings,
    ).require_current(
        layer="researcher",
        workflow_id=workflow_id,
        operation="researcher_runtime_output_repair",
    )
    state = await researcher.recover_runtime_output_contract(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def repair_saved_researcher_runtime_adapter(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--researcher-runtime-adapter-repair requires Demo Safe Mode to remain enabled"
        )
    researcher = build_researcher_manager(settings)
    RuntimeModelGuard(
        (researcher,),
        settings_loader=lambda: settings,
    ).require_current(
        layer="researcher",
        workflow_id=workflow_id,
        operation="researcher_runtime_adapter_repair",
    )
    state = await researcher.recover_runtime_adapter_contract(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def repair_saved_researcher_runtime_identity(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--researcher-runtime-identity-repair requires Demo Safe Mode to remain enabled"
        )
    researcher = build_researcher_manager(settings)
    RuntimeModelGuard(
        (researcher,),
        settings_loader=lambda: settings,
    ).require_current(
        layer="researcher",
        workflow_id=workflow_id,
        operation="researcher_runtime_identity_repair",
    )
    state = await researcher.recover_runtime_identity_contract(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def repair_saved_researcher_runtime_provenance(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--researcher-runtime-provenance-repair requires Demo Safe Mode to remain enabled"
        )
    researcher = build_researcher_manager(settings)
    RuntimeModelGuard(
        (researcher,),
        settings_loader=lambda: settings,
    ).require_current(
        layer="researcher",
        workflow_id=workflow_id,
        operation="researcher_runtime_provenance_repair",
    )
    state = await researcher.recover_runtime_provenance_contract(
        workflow_id,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def revise_saved_researcher(
    settings: Settings,
    workflow_id: str,
    *,
    reason: str | None,
    json_output: bool,
) -> int:
    researcher = build_researcher_manager(settings)
    state = await researcher.revise(
        workflow_id,
        reason=reason or "Human Operator requested additional evidence research",
        actor_source=HumanActorSource.CLI,
        progress_callback=ProgressReporter("researcher", settings.data_dir, workflow_id),
    )
    print_state("researcher", state, json_output=json_output)
    return 0 if state.status == "BLOCKED" else 1


async def run_saved_deliberation(
    settings: Settings,
    workflow_id: str,
    *,
    resume: bool,
    json_output: bool,
    recover: bool = False,
) -> int:
    if resume and recover:
        raise ValueError("Deliberation resume and recovery cannot be requested together")
    _producer, _researcher, deliberation = build_managers(settings)
    callback = ProgressReporter("deliberation", settings.data_dir, workflow_id)
    if recover:
        state = await deliberation.recover(workflow_id, progress_callback=callback)
    elif resume:
        state = await deliberation.resume(workflow_id, progress_callback=callback)
    else:
        state = await deliberation.start(workflow_id, progress_callback=callback)
    print_state("deliberation", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def retry_saved_deliberation_provider(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--deliberation-provider-retry requires Demo Safe Mode to remain enabled"
        )
    _producer, _researcher, deliberation = build_managers(settings)
    state = await deliberation.retry_provider_call(
        workflow_id,
        progress_callback=ProgressReporter(
            "deliberation",
            settings.data_dir,
            workflow_id,
        ),
    )
    print_state("deliberation", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def run_saved_conclusion(
    settings: Settings,
    workflow_id: str,
    *,
    resume: bool,
    json_output: bool,
    recover: bool = False,
) -> int:
    if resume and recover:
        raise ValueError("Conclusion resume and recovery cannot be requested together")
    _producer, _researcher, _deliberation, conclusion, _playwright = build_all_managers(settings)
    callback = ProgressReporter("conclusion", settings.data_dir, workflow_id)
    if recover:
        state = await conclusion.recover(workflow_id, progress_callback=callback)
    elif resume:
        state = await conclusion.resume(workflow_id, progress_callback=callback)
    else:
        state = await conclusion.start(workflow_id, progress_callback=callback)
    print_state("conclusion", state, json_output=json_output)
    return 0 if state.status in {"WAITING_HUMAN_SELECTION", "COMPLETED"} else 1


async def retry_saved_conclusion_provider(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--conclusion-provider-retry requires Demo Safe Mode to remain enabled"
        )
    _producer, _researcher, _deliberation, conclusion, _playwright = build_all_managers(
        settings
    )
    state = await conclusion.retry_provider_call(
        workflow_id,
        progress_callback=ProgressReporter(
            "conclusion",
            settings.data_dir,
            workflow_id,
        ),
    )
    print_state("conclusion", state, json_output=json_output)
    return 0 if state.status in {"WAITING_HUMAN_SELECTION", "COMPLETED"} else 1


async def revise_saved_conclusion(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--conclusion-revise requires Demo Safe Mode to remain enabled"
        )
    _producer, _researcher, _deliberation, conclusion, _playwright = build_all_managers(
        settings
    )
    state = await conclusion.revise(
        workflow_id,
        progress_callback=ProgressReporter(
            "conclusion",
            settings.data_dir,
            workflow_id,
        ),
    )
    print_state("conclusion", state, json_output=json_output)
    return 0 if state.status in {"WAITING_HUMAN_SELECTION", "COMPLETED"} else 1


async def repair_saved_conclusion_provider_contract(
    settings: Settings,
    workflow_id: str,
    repair_model_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--conclusion-contract-repair requires Demo Safe Mode to remain enabled"
        )
    _producer, _researcher, _deliberation, conclusion, _playwright = build_all_managers(
        settings
    )
    state = await conclusion.repair_provider_contract(
        workflow_id,
        repair_model_id=repair_model_id,
        progress_callback=ProgressReporter(
            "conclusion",
            settings.data_dir,
            workflow_id,
        ),
    )
    print_state("conclusion", state, json_output=json_output)
    return 0 if state.status in {"WAITING_HUMAN_SELECTION", "COMPLETED"} else 1


async def run_conclusion_integration(
    settings: Settings,
    values: list[str],
    *,
    json_output: bool,
) -> int:
    if len(values) < 3:
        raise ValueError(
            "--conclusion-integrate requires WORKFLOW_ID and at least two candidate IDs"
        )
    workflow_id, *candidate_ids = values
    _producer, _researcher, _deliberation, conclusion, _playwright = build_all_managers(settings)
    state = await conclusion.integrate_candidates(workflow_id, candidate_ids)
    print_state("conclusion", state, json_output=json_output)
    return 0 if state.status == "WAITING_HUMAN_SELECTION" else 1


def run_conclusion_selection(
    settings: Settings,
    workflow_id: str,
    candidate_id: str,
    *,
    json_output: bool,
) -> int:
    _producer, _researcher, _deliberation, conclusion, _playwright = build_all_managers(settings)
    state = conclusion.select(workflow_id, [candidate_id])
    print_state("conclusion", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def run_saved_playwright(
    settings: Settings,
    workflow_id: str,
    *,
    resume: bool,
    json_output: bool,
    recover: bool = False,
) -> int:
    if resume and recover:
        raise ValueError("Playwright resume and recovery cannot be requested together")
    _producer, _researcher, _deliberation, _conclusion, playwright = build_all_managers(settings)
    callback = ProgressReporter("playwright", settings.data_dir, workflow_id)
    if recover:
        state = await playwright.recover(workflow_id, progress_callback=callback)
    elif resume:
        state = await playwright.resume(workflow_id, progress_callback=callback)
    else:
        state = await playwright.start(workflow_id, progress_callback=callback)
    print_state("playwright", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def retry_saved_playwright_provider(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--playwright-provider-retry requires Demo Safe Mode to remain enabled"
        )
    _producer, _researcher, _deliberation, _conclusion, playwright = build_all_managers(
        settings
    )
    state = await playwright.retry_provider_call(
        workflow_id,
        progress_callback=ProgressReporter(
            "playwright",
            settings.data_dir,
            workflow_id,
        ),
    )
    print_state("playwright", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def revise_saved_playwright(
    settings: Settings,
    workflow_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--playwright-revise requires Demo Safe Mode to remain enabled"
        )
    _producer, _researcher, _deliberation, _conclusion, playwright = (
        build_all_managers(settings)
    )
    state = await playwright.revise(
        workflow_id,
        progress_callback=ProgressReporter(
            "playwright",
            settings.data_dir,
            workflow_id,
        ),
    )
    print_state("playwright", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


async def repair_saved_playwright_provider_capability(
    settings: Settings,
    workflow_id: str,
    repair_model_id: str,
    *,
    json_output: bool,
) -> int:
    if not settings.demo_safe_mode:
        raise ValueError(
            "--playwright-capability-repair requires Demo Safe Mode to remain enabled"
        )
    _producer, _researcher, _deliberation, _conclusion, playwright = (
        build_all_managers(settings)
    )
    state = await playwright.repair_provider_capability(
        workflow_id,
        repair_model_id=repair_model_id,
        progress_callback=ProgressReporter(
            "playwright",
            settings.data_dir,
            workflow_id,
        ),
    )
    print_state("playwright", state, json_output=json_output)
    return 0 if state.status == "COMPLETED" else 1


def show_status(settings: Settings, workflow_id: str, *, json_output: bool) -> int:
    states = load_workflow_states(settings.data_dir, workflow_id)
    if not states:
        print(
            f"Workflow {workflow_id} は {settings.data_dir / 'workflows'} に見つかりません。",
            file=sys.stderr,
        )
        return 1
    if json_output:
        import json

        print(json.dumps({layer: state for layer, state in states}, ensure_ascii=False, indent=2))
        return 0
    for index, (layer, state) in enumerate(states):
        if index:
            print()
        print_state(layer, state, include_next_action=index == len(states) - 1)
    return 0


def run_discord_bot(settings: Settings) -> int:
    if not settings.discord_bot_token:
        raise ValueError(
            "DISCORD_BOT_TOKENが未設定です。まず `py main.py --doctor` を実行するか、"
            "APIなし確認には `py main.py --demo-e2e` を使用してください。"
        )
    from discord_app.bot import create_bot

    managers = build_all_managers(settings)
    bot = create_bot(
        *managers,
        auto_start_researcher=settings.auto_start_researcher,
        auto_start_deliberation=settings.auto_start_deliberation,
        auto_start_conclusion=settings.auto_start_conclusion,
        auto_start_playwright=settings.auto_start_playwright,
        settings=settings,
    )
    bot.run(settings.discord_bot_token, log_handler=None)
    return 0


def dispatch(args: Any, settings: Settings) -> int:
    if args.doctor:
        return print_doctor_report(run_doctor(settings), json_output=args.json_output)
    if args.status:
        return show_status(settings, args.status, json_output=args.json_output)
    print(
        f"[runtime] provider={settings.provider} "
        f"demo_safe_mode={str(settings.demo_safe_mode).lower()}",
        file=sys.stderr,
    )
    if settings.provider == "openrouter" and not settings.demo_safe_mode:
        print(
            "[WARNING] OpenRouter + Demo Safe Mode OFF: automatic revision and "
            "additional provider calls are enabled.",
            file=sys.stderr,
        )
    if args.producer_recover:
        return asyncio.run(
            recover_saved_producer(
                settings,
                args.producer_recover,
                json_output=args.json_output,
            )
        )
    if args.producer_provider_retry:
        return asyncio.run(
            retry_saved_producer_provider(
                settings,
                args.producer_provider_retry,
                json_output=args.json_output,
            )
        )
    if args.producer_output_repair:
        return asyncio.run(
            repair_saved_producer_output(
                settings,
                args.producer_output_repair,
                json_output=args.json_output,
            )
        )
    if args.researcher:
        return asyncio.run(
            run_saved_researcher(settings, args.researcher, json_output=args.json_output)
        )
    if args.researcher_task:
        return asyncio.run(
            run_saved_researcher_task(
                settings,
                *args.researcher_task,
                json_output=args.json_output,
            )
        )
    if args.researcher_resume:
        return asyncio.run(
            resume_saved_researcher(
                settings,
                args.researcher_resume,
                json_output=args.json_output,
            )
        )
    if args.researcher_recover:
        return recover_saved_researcher_human_gate(
            settings,
            args.researcher_recover,
            json_output=args.json_output,
        )
    if args.researcher_evidence:
        return inspect_saved_researcher_evidence(
            settings,
            args.researcher_evidence,
            json_output=args.json_output,
        )
    if args.researcher_accept:
        return decide_saved_researcher_evidence(
            settings,
            args.researcher_accept,
            decision=HumanEvidenceDecisionType.ACCEPT,
            reason=args.reason,
            json_output=args.json_output,
        )
    if args.researcher_accept_limitations:
        return decide_saved_researcher_evidence(
            settings,
            args.researcher_accept_limitations,
            decision=HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS,
            reason=args.reason,
            json_output=args.json_output,
        )
    if args.researcher_provider_retry:
        return asyncio.run(
            retry_saved_researcher_provider(
                settings,
                args.researcher_provider_retry,
                json_output=args.json_output,
            )
        )
    if args.researcher_runtime_model_repair:
        return asyncio.run(
            repair_saved_researcher_runtime_models(
                settings,
                args.researcher_runtime_model_repair,
                json_output=args.json_output,
            )
        )
    if args.researcher_retrieval_reconstruct:
        return asyncio.run(
            reconstruct_saved_researcher_retrieval(
                settings,
                args.researcher_retrieval_reconstruct,
                json_output=args.json_output,
            )
        )
    if args.researcher_runtime_output_repair:
        return asyncio.run(
            repair_saved_researcher_runtime_output(
                settings,
                args.researcher_runtime_output_repair,
                json_output=args.json_output,
            )
        )
    if args.researcher_runtime_adapter_repair:
        return asyncio.run(
            repair_saved_researcher_runtime_adapter(
                settings,
                args.researcher_runtime_adapter_repair,
                json_output=args.json_output,
            )
        )
    if args.researcher_runtime_identity_repair:
        return asyncio.run(
            repair_saved_researcher_runtime_identity(
                settings,
                args.researcher_runtime_identity_repair,
                json_output=args.json_output,
            )
        )
    if args.researcher_runtime_provenance_repair:
        return asyncio.run(
            repair_saved_researcher_runtime_provenance(
                settings,
                args.researcher_runtime_provenance_repair,
                json_output=args.json_output,
            )
        )
    if args.researcher_revise:
        return asyncio.run(
            revise_saved_researcher(
                settings,
                args.researcher_revise,
                reason=args.reason,
                json_output=args.json_output,
            )
        )
    if args.deliberation:
        return asyncio.run(
            run_saved_deliberation(
                settings,
                args.deliberation,
                resume=False,
                json_output=args.json_output,
            )
        )
    if args.deliberation_resume:
        return asyncio.run(
            run_saved_deliberation(
                settings,
                args.deliberation_resume,
                resume=True,
                json_output=args.json_output,
            )
        )
    if args.deliberation_recover:
        return asyncio.run(
            run_saved_deliberation(
                settings,
                args.deliberation_recover,
                resume=False,
                recover=True,
                json_output=args.json_output,
            )
        )
    if args.deliberation_provider_retry:
        return asyncio.run(
            retry_saved_deliberation_provider(
                settings,
                args.deliberation_provider_retry,
                json_output=args.json_output,
            )
        )
    if args.conclusion:
        return asyncio.run(
            run_saved_conclusion(
                settings,
                args.conclusion,
                resume=False,
                json_output=args.json_output,
            )
        )
    if args.conclusion_resume:
        return asyncio.run(
            run_saved_conclusion(
                settings,
                args.conclusion_resume,
                resume=True,
                json_output=args.json_output,
            )
        )
    if args.conclusion_recover:
        return asyncio.run(
            run_saved_conclusion(
                settings,
                args.conclusion_recover,
                resume=False,
                recover=True,
                json_output=args.json_output,
            )
        )
    if args.conclusion_provider_retry:
        return asyncio.run(
            retry_saved_conclusion_provider(
                settings,
                args.conclusion_provider_retry,
                json_output=args.json_output,
            )
        )
    if args.conclusion_revise:
        return asyncio.run(
            revise_saved_conclusion(
                settings,
                args.conclusion_revise,
                json_output=args.json_output,
            )
        )
    if args.conclusion_contract_repair:
        return asyncio.run(
            repair_saved_conclusion_provider_contract(
                settings,
                *args.conclusion_contract_repair,
                json_output=args.json_output,
            )
        )
    if args.conclusion_select:
        return run_conclusion_selection(
            settings,
            *args.conclusion_select,
            json_output=args.json_output,
        )
    if args.conclusion_integrate:
        return asyncio.run(
            run_conclusion_integration(
                settings,
                args.conclusion_integrate,
                json_output=args.json_output,
            )
        )
    if args.playwright:
        return asyncio.run(
            run_saved_playwright(
                settings,
                args.playwright,
                resume=False,
                json_output=args.json_output,
            )
        )
    if args.playwright_resume:
        return asyncio.run(
            run_saved_playwright(
                settings,
                args.playwright_resume,
                resume=True,
                json_output=args.json_output,
            )
        )
    if args.playwright_recover:
        return asyncio.run(
            run_saved_playwright(
                settings,
                args.playwright_recover,
                resume=False,
                recover=True,
                json_output=args.json_output,
            )
        )
    if args.playwright_provider_retry:
        return asyncio.run(
            retry_saved_playwright_provider(
                settings,
                args.playwright_provider_retry,
                json_output=args.json_output,
            )
        )
    if args.playwright_revise:
        return asyncio.run(
            revise_saved_playwright(
                settings,
                args.playwright_revise,
                json_output=args.json_output,
            )
        )
    if args.playwright_capability_repair:
        return asyncio.run(
            repair_saved_playwright_provider_capability(
                settings,
                *args.playwright_capability_repair,
                json_output=args.json_output,
            )
        )
    if args.demo or args.demo_full or args.demo_e2e:
        return asyncio.run(
            run_demo(
                settings,
                args.topic,
                run_researcher=args.demo_full or args.demo_e2e,
                run_deliberation=args.demo_full or args.demo_e2e,
                run_conclusion=args.demo_full or args.demo_e2e,
                run_playwright=args.demo_e2e,
                json_output=args.json_output,
            )
        )
    return run_discord_bot(settings)


def entrypoint(argv: list[str] | None = None) -> int:
    configure_cli_text_streams()
    args = parse_args(argv)
    try:
        settings = apply_runtime_overrides(
            Settings.from_env(),
            provider=args.provider,
            demo_safe_mode=args.demo_safe_mode,
        )
        configure_logging(settings.log_level, data_dir=settings.data_dir)
        return dispatch(args, settings)
    except KeyboardInterrupt:
        print("[CANCELLED] ユーザー操作で停止しました。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        print("次の確認: py main.py --doctor", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 1
