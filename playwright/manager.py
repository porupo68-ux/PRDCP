from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from common.ids import new_id
from common.models.pmp import (
    MessageStatus,
    MessageType,
    PMPContext,
    PMPMessage,
    PMPMetadata,
    PMPRouting,
)
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from common.validation import PMPValidator
from playwright.registry import PlaywrightRegistry
from playwright.schemas import (
    CitationEditingResult,
    CitationEditingTask,
    CitationManifest,
    CitationValidatedScript,
    DeterministicValidationResult,
    FinalScriptPackage,
    NarrativeBlueprint,
    NarrativeDesignTask,
    PlaywrightFinalGateResult,
    PlaywrightGateStatus,
    ProductionContext,
    ScriptDraft,
    ScriptWritingTask,
    UpstreamConclusionRevisionRequest,
    ValidationSeverity,
    VisualDirectionTask,
    VisualPlan,
)
from playwright.state import (
    PlaywrightRevisionRecord,
    PlaywrightStatus,
    PlaywrightUpstreamRevisionRecord,
    PlaywrightWorkflowState,
    utc_now,
)
from playwright.validator import PlaywrightValidator, canonical_hash
from playwright.workflow import (
    AGENT_ORDER,
    EVIDENCE_CITATION_EDITOR_ID,
    NARRATIVE_ARCHITECT_ID,
    REVISION_DEPENDENCIES,
    SCRIPTWRITER_ID,
    VISUAL_DIRECTOR_ID,
)
from storage.playwright_workflow_repository import PlaywrightWorkflowRepository


ProgressCallback = Callable[[str], Awaitable[None]]


class PlaywrightManager:
    agent_id = "playwright.manager"

    def __init__(
        self,
        registry: PlaywrightRegistry,
        repository: PlaywrightWorkflowRepository,
        *,
        max_revisions: int = 2,
        target_duration_seconds: int = 720,
        target_audience: str = "一般の成人視聴者",
        video_format: str = "YouTube解説動画",
        language: str = "ja",
        rd_loader: RoleDefinitionLoader | None = None,
        demo_safe_mode: bool = True,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.demo_safe_mode = demo_safe_mode
        self.max_revisions = 0 if demo_safe_mode else max_revisions
        self.target_duration_seconds = target_duration_seconds
        self.target_audience = target_audience
        self.video_format = video_format
        self.language = language
        self.rd_loader = rd_loader or registry.rd_loader
        self.pmp_validator = PMPValidator()
        self.validator = PlaywrightValidator()

    async def start(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        try:
            return self.repository.load(workflow_id)
        except FileNotFoundError:
            pass
        return await self.start_from_message(
            self.repository.load_conclusion_handoff(workflow_id),
            progress_callback=progress_callback,
        )

    async def start_from_message(
        self,
        handoff: PMPMessage,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if not self.demo_safe_mode and runtime.revision_limit is not None:
            self.max_revisions = runtime.revision_limit
        self._validate_envelope(handoff)
        payload = handoff.payload
        if not payload.get("final_conclusion"):
            raise ValueError("HANDOFF_REJECTED: Final Conclusion is required")
        final = dict(payload["final_conclusion"])
        package = dict(payload.get("conclusion_package") or {})
        selection = dict(payload.get("human_selection") or {})
        trace = dict(payload.get("traceability_manifest") or {})
        state = PlaywrightWorkflowState(
            workflow_id=handoff.workflow_id,
            status=PlaywrightStatus.VALIDATING_HANDOFF,
            conclusion_handoff=handoff.model_dump(mode="json"),
            final_conclusion=final,
            conclusion_package=package,
            human_selection=selection,
            traceability_manifest=trace,
            final_conclusion_hash=canonical_hash(final),
            message_history=[handoff],
            role_definition_usage=[manager_snapshot.trace()],
            limitations=list(payload.get("limitations_to_disclose") or final.get("limitations") or []),
        )
        if not selection:
            state.status = PlaywrightStatus.BLOCKED
            state.error = {"stage": "handoff", "code": "HUMAN_SELECTION_MISSING", "message": "Human Selection is required before script production"}
            self.repository.save(state)
            return state
        problems = self._handoff_problems(payload)
        self.repository.save(state)
        if problems:
            return await self._request_upstream_revision(state, problems, progress_callback)
        context = self._build_production_context(state, payload)
        state.production_context = context.model_dump(mode="json")
        self.repository.save(state)
        await self._emit(progress_callback, f"Playwright Workflow開始: {state.workflow_id}")
        return await self._run(state, rerun_from=0, progress_callback=progress_callback)

    async def resume(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        state = self.repository.load(workflow_id)
        if state.status != PlaywrightStatus.WAITING_UPSTREAM_REVISION.value:
            raise ValueError("Playwright workflow is not waiting for an upstream revision")
        handoff = self.repository.load_conclusion_handoff(workflow_id)
        if handoff.message_id == state.conclusion_handoff.get("message_id"):
            raise ValueError("Conclusionから新しいrevision resultがまだ届いていません")
        self._validate_envelope(handoff)
        payload = handoff.payload
        if not payload.get("final_conclusion") or not payload.get("human_selection"):
            raise ValueError("Revised Conclusion handoff is incomplete")
        state.conclusion_handoff = handoff.model_dump(mode="json")
        state.final_conclusion = dict(payload["final_conclusion"])
        state.conclusion_package = dict(payload["conclusion_package"])
        state.human_selection = dict(payload["human_selection"])
        state.traceability_manifest = dict(payload["traceability_manifest"])
        state.final_conclusion_hash = canonical_hash(state.final_conclusion)
        state.production_context = None
        state.narrative_blueprint = None
        state.script_draft = None
        state.citation_validated_script = None
        state.citation_manifest = None
        state.visual_plan = None
        state.final_script_package = None
        state.deterministic_validation = None
        state.final_gate_result = None
        state.completed_agents = []
        state.failed_agents = []
        state.current_agent_ids = []
        state.revision_count = 0
        state.revision_history = []
        state.message_history.append(handoff)
        state.limitations = list(payload.get("limitations_to_disclose") or state.final_conclusion.get("limitations") or [])
        state.error = None
        problems = self._handoff_problems(payload)
        if problems:
            self.repository.save(state)
            return await self._request_upstream_revision(state, problems, progress_callback)
        state.production_context = self._build_production_context(state, payload).model_dump(mode="json")
        self.repository.save(state)
        await self._emit(progress_callback, "Conclusion修正結果を受領し、Playwrightを再開します")
        return await self._run(state, rerun_from=0, progress_callback=progress_callback)

    async def _run(
        self,
        state: PlaywrightWorkflowState,
        *,
        rerun_from: int,
        progress_callback: ProgressCallback | None,
    ) -> PlaywrightWorkflowState:
        while True:
            try:
                context = ProductionContext.model_validate(state.production_context)
                revision_context = self._latest_revision_context(state)
                if rerun_from <= 0 or not state.narrative_blueprint:
                    state.status = PlaywrightStatus.DESIGNING_NARRATIVE
                    narrative = await self._execute_agent(
                        state,
                        NARRATIVE_ARCHITECT_ID,
                        NarrativeDesignTask(production_context=context, revision_context=revision_context),
                        NarrativeBlueprint,
                    )
                    state.narrative_blueprint = narrative.model_dump(mode="json")
                    self.repository.save_narrative(narrative, state.workflow_id)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Narrative Architect完了")
                else:
                    narrative = NarrativeBlueprint.model_validate(state.narrative_blueprint)

                if rerun_from <= 1 or not state.script_draft:
                    state.status = PlaywrightStatus.WRITING_SCRIPT
                    script = await self._execute_agent(
                        state,
                        SCRIPTWRITER_ID,
                        ScriptWritingTask(
                            production_context=context,
                            narrative_blueprint=narrative,
                            revision_context=revision_context,
                        ),
                        ScriptDraft,
                    )
                    state.script_draft = script.model_dump(mode="json")
                    self.repository.save_script(script, state.workflow_id)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Scriptwriter完了")
                else:
                    script = ScriptDraft.model_validate(state.script_draft)

                if rerun_from <= 2 or not state.citation_manifest or not state.citation_validated_script:
                    state.status = PlaywrightStatus.EDITING_CITATIONS
                    citation_result = await self._execute_agent(
                        state,
                        EVIDENCE_CITATION_EDITOR_ID,
                        CitationEditingTask(
                            production_context=context,
                            script_draft=script,
                            revision_context=revision_context,
                        ),
                        CitationEditingResult,
                    )
                    validated_script = citation_result.citation_validated_script
                    citation_manifest = citation_result.citation_manifest
                    state.citation_validated_script = validated_script.model_dump(mode="json")
                    state.citation_manifest = citation_manifest.model_dump(mode="json")
                    self.repository.save_citation_manifest(citation_manifest, state.workflow_id)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Evidence & Citation Editor完了")
                else:
                    validated_script = CitationValidatedScript.model_validate(state.citation_validated_script)
                    citation_manifest = CitationManifest.model_validate(state.citation_manifest)

                if rerun_from <= 3 or not state.visual_plan:
                    state.status = PlaywrightStatus.DESIGNING_VISUALS
                    visual_plan = await self._execute_agent(
                        state,
                        VISUAL_DIRECTOR_ID,
                        VisualDirectionTask(
                            production_context=context,
                            citation_validated_script=validated_script,
                            citation_manifest=citation_manifest,
                            revision_context=revision_context,
                        ),
                        VisualPlan,
                    )
                    state.visual_plan = visual_plan.model_dump(mode="json")
                    self.repository.save_visual_plan(visual_plan, state.workflow_id)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Visual Director完了")
                else:
                    visual_plan = VisualPlan.model_validate(state.visual_plan)

                state.status = PlaywrightStatus.VALIDATING_PACKAGE
                validation = self.validator.validate(
                    production_context=context,
                    narrative=narrative,
                    script_draft=script,
                    validated_script=validated_script,
                    citation_manifest=citation_manifest,
                    visual_plan=visual_plan,
                    final_conclusion=state.final_conclusion,
                    expected_final_conclusion_hash=state.final_conclusion_hash,
                )
                state.deterministic_validation = validation.model_dump(mode="json")
                gate = self._final_gate(state, validation)
                state.final_gate_result = gate.model_dump(mode="json")
                self.repository.save(state)
            except Exception as exc:
                return await self._fail(state, f"Playwright生成に失敗しました: {exc}", progress_callback)

            if gate.status == PlaywrightGateStatus.UPSTREAM_REVISION_REQUIRED.value:
                return await self._request_upstream_revision(
                    state,
                    gate.upstream_revision_requests,
                    progress_callback,
                )
            if gate.status == PlaywrightGateStatus.BLOCKED.value:
                state.status = PlaywrightStatus.BLOCKED
                state.current_agent_ids = []
                state.error = {"stage": "final_gate", "message": gate.delivery_readiness}
                self.repository.save(state)
                return state
            if gate.status == PlaywrightGateStatus.REVISION_REQUIRED.value:
                state.revision_count += 1
                rerun_from = min(AGENT_ORDER.index(item) for item in gate.revision_targets)
                stages = REVISION_DEPENDENCIES[AGENT_ORDER[rerun_from]]
                state.revision_history.append(
                    PlaywrightRevisionRecord(
                        iteration=state.revision_count,
                        target_agent_ids=gate.revision_targets,
                        findings=gate.findings,
                        rerun_stages=stages,
                    )
                )
                state.status = PlaywrightStatus.REVISING
                self._clear_from(state, rerun_from)
                self.repository.save(state)
                await self._emit(progress_callback, f"Playwright Revision {state.revision_count}: {', '.join(gate.revision_targets)}")
                continue

            package = self._build_final_package(
                state,
                context,
                script,
                validated_script,
                citation_manifest,
                visual_plan,
                gate,
            )
            state.final_script_package = package.model_dump(mode="json")
            self.repository.save_final_package(package)
            state.delivery_paths = self.repository.save_deliveries(package)
            delivery = PMPMessage.create(
                workflow_id=state.workflow_id,
                parent_message_id=state.message_history[-1].message_id,
                sender_agent_id=self.agent_id,
                receiver_agent_id="system.final_output",
                message_type=MessageType.FINAL_SCRIPT_DELIVERY,
                objective="Deliver the completed Final Script Package",
                payload={
                    "final_script_package_id": package.final_script_package_id,
                    "production_summary": package.production_summary,
                    "delivery_paths": state.delivery_paths,
                },
                constraints={"final_conclusion_changes_allowed": False},
                context=PMPContext(current_stage="playwright.completed", previous_stage="playwright.final_gate", next_stage="delivery"),
                metadata=PMPMetadata(
                    status=MessageStatus.COMPLETED,
                    extensions={"role_definition": state.role_definition_usage[0]},
                ),
            )
            self.pmp_validator.validate(delivery)
            state.message_history.append(delivery)
            state.delivered = True
            state.status = PlaywrightStatus.COMPLETED
            state.current_agent_ids = []
            state.error = None
            state.completed_at = utc_now()
            self.repository.save(state)
            await self._emit(progress_callback, "Final Script PackageをJSON・Markdownで納品しました")
            return state

    async def _execute_agent(self, state, agent_id: str, task, output_schema):
        message_type = (
            MessageType.REVISION_REQUEST
            if getattr(task, "revision_context", None)
            else MessageType.TASK
        )
        request = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=state.message_history[-1].message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=agent_id,
            message_type=message_type,
            objective=f"Execute {agent_id} for the approved Final Conclusion",
            payload=task.model_dump(mode="json"),
            constraints={
                "preserve_final_conclusion": True,
                "new_evidence_allowed": False,
                "requested_action": self._task_action(agent_id),
            },
            context=PMPContext(current_stage="playwright.manager", previous_stage=state.status, next_stage=agent_id),
            metadata=PMPMetadata(status=MessageStatus.QUEUED),
        )
        self.pmp_validator.validate(request)
        state.message_history.append(request)
        state.current_agent_ids = [agent_id]
        self.repository.save(state)
        response = await self.registry.get(agent_id).execute(request)
        state.message_history.append(response)
        state.current_agent_ids = []
        if response.workflow_id != request.workflow_id or response.parent_message_id != request.message_id:
            raise ValueError(f"Invalid PMP response correlation from {agent_id}")
        if response.sender_agent_id != agent_id or response.receiver_agent_id != self.agent_id:
            raise ValueError(f"Invalid PMP response routing from {agent_id}")
        if response.message_type == MessageType.ERROR.value:
            if agent_id not in state.failed_agents:
                state.failed_agents.append(agent_id)
            raise RuntimeError(str(response.payload.get("message") or "Agent returned an error"))
        if response.message_type != MessageType.RESULT.value:
            raise ValueError(f"Unexpected PMP message type from {agent_id}: {response.message_type}")
        trace = response.metadata.extensions.get("role_definition")
        if trace and trace not in state.role_definition_usage:
            state.role_definition_usage.append(trace)
        if agent_id not in state.completed_agents:
            state.completed_agents.append(agent_id)
        if agent_id in state.failed_agents:
            state.failed_agents.remove(agent_id)
        self.repository.save(state)
        return output_schema.model_validate(response.payload)

    def _build_production_context(self, state: PlaywrightWorkflowState, payload: dict[str, Any]) -> ProductionContext:
        final = state.final_conclusion
        package = state.conclusion_package
        selection = state.human_selection
        sources = state.traceability_manifest.get("sources") or payload.get("evidence_links") or []
        return ProductionContext(
            production_context_id=new_id("production_context"),
            workflow_id=state.workflow_id,
            final_conclusion_id=final["final_conclusion_id"],
            conclusion_package_id=package["conclusion_package_id"],
            human_selection_id=selection["selection_id"],
            topic=package.get("topic") or payload["topic"],
            central_question=package.get("decision_question") or payload["central_question"],
            selected_position=final["selected_position"],
            final_recommendation=final["final_recommendation"],
            target_audience=payload.get("target_audience") or self.target_audience,
            video_objective=payload.get("video_objective") or "一般的な意見を証拠と反論を含めて検証し、選択済み結論を説明する",
            desired_duration_seconds=int(payload.get("desired_duration_seconds") or self.target_duration_seconds),
            language=payload.get("language") or self.language,
            format=payload.get("format") or self.video_format,
            must_include_claim_ids=final["supporting_claim_ids"],
            must_include_evidence_ids=final["supporting_evidence_ids"],
            accepted_tradeoffs=final.get("accepted_tradeoffs", []),
            accepted_risks=final.get("accepted_risks", []),
            uncertainties=final.get("uncertainties", []),
            limitations_to_disclose=state.limitations,
            tone_constraints=payload.get("tone_constraints") or ["証拠強度に応じて断定の強さを調整する"],
            format_constraints=payload.get("format_constraints") or ["段落単位で引用とVisual Cueを追跡する"],
            source_manifest=sources,
        )

    def _final_gate(self, state: PlaywrightWorkflowState, validation: DeterministicValidationResult) -> PlaywrightFinalGateResult:
        findings = [item.model_dump(mode="json") for item in validation.findings]
        errors = [item for item in validation.findings if item.severity == ValidationSeverity.ERROR.value]
        upstream = [item for item in errors if item.upstream_required]
        if upstream:
            requests = [self._finding_to_upstream(state, item).model_dump(mode="json") for item in upstream]
            return PlaywrightFinalGateResult(
                final_gate_result_id=new_id("pw_gate"),
                status=PlaywrightGateStatus.UPSTREAM_REVISION_REQUIRED,
                findings=findings,
                blocking_finding_ids=[item.finding_id for item in upstream],
                upstream_revision_requests=requests,
                limitations_to_disclose=state.limitations,
                delivery_readiness="Conclusion revision required",
            )
        if errors:
            targets = list(dict.fromkeys(item.target_agent_id for item in errors if item.target_agent_id in AGENT_ORDER))
            if self.max_revisions <= state.revision_count or not targets:
                return PlaywrightFinalGateResult(
                    final_gate_result_id=new_id("pw_gate"),
                    status=PlaywrightGateStatus.BLOCKED,
                    findings=findings,
                    blocking_finding_ids=[item.finding_id for item in errors],
                    limitations_to_disclose=state.limitations,
                    delivery_readiness="Revision limit reached or no valid revision route remains",
                )
            return PlaywrightFinalGateResult(
                final_gate_result_id=new_id("pw_gate"),
                status=PlaywrightGateStatus.REVISION_REQUIRED,
                findings=findings,
                blocking_finding_ids=[item.finding_id for item in errors],
                revision_targets=targets,
                limitations_to_disclose=state.limitations,
                delivery_readiness="Targeted Playwright revision required",
            )
        status = PlaywrightGateStatus.APPROVED_WITH_LIMITATIONS if state.limitations else PlaywrightGateStatus.APPROVED
        return PlaywrightFinalGateResult(
            final_gate_result_id=new_id("pw_gate"),
            status=status,
            findings=findings,
            limitations_to_disclose=state.limitations,
            delivery_readiness="READY_WITH_LIMITATIONS" if state.limitations else "READY",
        )

    def _build_final_package(self, state, context, script, validated_script, manifest, visual, gate):
        paragraph_count = sum(len(section.paragraphs) for section in validated_script.sections)
        traceability = {
            **state.traceability_manifest,
            "final_conclusion_id": state.final_conclusion["final_conclusion_id"],
            "human_selection_id": state.human_selection["selection_id"],
            "paragraph_ids": [p.paragraph_id for s in validated_script.sections for p in s.paragraphs],
            "citation_mapping_ids": [item.citation_mapping_id for item in manifest.mappings],
            "visual_cue_ids": [item.visual_cue_id for item in visual.visual_cues],
        }
        return FinalScriptPackage(
            final_script_package_id=new_id("final_script_package"),
            workflow_id=state.workflow_id,
            final_conclusion_id=state.final_conclusion["final_conclusion_id"],
            human_selection_id=state.human_selection["selection_id"],
            title_candidates=script.title_candidates,
            thumbnail_text_candidates=script.thumbnail_text_candidates,
            script=validated_script,
            citation_manifest=manifest,
            visual_plan=visual,
            production_summary={
                "estimated_duration_seconds": script.estimated_duration_seconds,
                "estimated_character_count": script.estimated_character_count,
                "section_count": len(validated_script.sections),
                "paragraph_count": paragraph_count,
                "citation_count": len(manifest.mappings),
                "visual_cue_count": len(visual.visual_cues),
                "chart_request_count": len(visual.chart_requests),
            },
            limitations_to_disclose=state.limitations,
            unresolved_production_items=[
                item.model_dump(mode="json")
                for item in (
                    validated_script.unresolved_citation_issues
                    + visual.visual_integrity_warnings
                )
            ],
            traceability_manifest=traceability,
            final_gate_result=gate.model_dump(mode="json"),
        )

    async def _request_upstream_revision(self, state, problems, progress_callback):
        if self.demo_safe_mode:
            return await self._fail(
                state,
                "Demo Safe Mode stopped automatic upstream revision routing",
                progress_callback,
            )
        requests = []
        if problems and isinstance(problems[0], dict) and "revision_request_id" in problems[0]:
            requests = problems
        else:
            for item in problems:
                finding_id = new_id("pw_handoff_finding")
                requests.append(
                    UpstreamConclusionRevisionRequest(
                        revision_request_id=new_id("pw_upstream"),
                        final_conclusion_id=state.final_conclusion.get("final_conclusion_id") or state.conclusion_handoff.get("payload", {}).get("conclusion_id") or "unknown",
                        affected_claim_ids=list(state.final_conclusion.get("supporting_claim_ids") or []),
                        affected_evidence_ids=list(state.final_conclusion.get("supporting_evidence_ids") or []),
                        issue_type=item.get("code", "CONCLUSION_HANDOFF_INVALID") if isinstance(item, dict) else "CONCLUSION_HANDOFF_INVALID",
                        issue_description=item.get("message", str(item)) if isinstance(item, dict) else str(item),
                        required_resolution="Conclusionの正本とTraceabilityを修正し、新しいconclusion_handoffを発行する",
                        acceptance_conditions=["Human SelectionとFinal ConclusionのIDが一致する", "全Supporting IDがTraceability Manifestに存在する"],
                        source_finding_ids=[finding_id],
                    ).model_dump(mode="json")
                )
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=state.message_history[-1].message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="conclusion.manager",
            message_type=MessageType.REVISION_REQUEST,
            objective="Revise Conclusion artifacts required for script production",
            payload={
                "final_conclusion_id": state.final_conclusion.get("final_conclusion_id", "unknown"),
                "revision_requests": requests,
            },
            constraints={"preserve_human_selection": True, "new_evidence_collection_allowed": False},
            context=PMPContext(current_stage="playwright.upstream_revision", previous_stage=state.status, next_stage="conclusion"),
            routing=PMPRouting(revision_target="conclusion.manager", reply_required=True),
            metadata=PMPMetadata(status=MessageStatus.REVISION_REQUIRED, extensions={"role_definition": state.role_definition_usage[0]}),
        )
        self.pmp_validator.validate(message)
        self.repository.save_conclusion_revision_outbox(message)
        state.message_history.append(message)
        state.upstream_revision_count += 1
        state.upstream_revision_history.append(
            PlaywrightUpstreamRevisionRecord(
                iteration=state.upstream_revision_count,
                request_message_id=message.message_id,
                requests=requests,
            )
        )
        state.status = PlaywrightStatus.WAITING_UPSTREAM_REVISION
        state.current_agent_ids = []
        state.error = None
        self.repository.save(state)
        await self._emit(progress_callback, "Conclusionへ修正を要求し、Playwrightを待機状態にしました")
        return state

    def _handoff_problems(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        final = payload.get("final_conclusion") or {}
        package = payload.get("conclusion_package") or {}
        selection = payload.get("human_selection") or {}
        trace = payload.get("traceability_manifest") or {}
        problems: list[dict[str, str]] = []

        def problem(code: str, message: str) -> None:
            problems.append({"code": code, "message": message})

        if final.get("workflow_id") != payload.get("workflow_metadata", {}).get("workflow_id", final.get("workflow_id")):
            problem("WORKFLOW_ID_MISMATCH", "Final Conclusion workflow_id does not match handoff metadata")
        if final.get("human_selection_id") != selection.get("selection_id"):
            problem("HUMAN_SELECTION_ID_MISMATCH", "Final Conclusion and Human Selection IDs do not match")
        if final.get("conclusion_package_id") != package.get("conclusion_package_id"):
            problem("CONCLUSION_PACKAGE_ID_MISMATCH", "Final Conclusion and Conclusion Package IDs do not match")
        selected_ids = set(selection.get("selected_candidate_ids") or [])
        selected_position_id = final.get("selected_position", {}).get("position_candidate_id") or final.get("selected_position", {}).get("candidate_id")
        if selected_ids and selected_position_id not in selected_ids:
            problem("HUMAN_SELECTION_TARGET_MISMATCH", "Selected position is not included in Human Selection")
        review = package.get("quality_review") or payload.get("quality_review") or {}
        if review.get("status") not in {"approved", "approved_with_conditions"}:
            problem("CONCLUSION_QUALITY_NOT_APPROVED", "Conclusion Package has not passed its Quality Gate")
        if review.get("playwright_readiness") not in {"READY", "READY_WITH_CONDITIONS"}:
            problem("PLAYWRIGHT_NOT_READY", "Conclusion Package is not marked Playwright-ready")
        for trace_key, final_key in (
            ("claim_ids", "supporting_claim_ids"),
            ("analysis_ids", "supporting_analysis_ids"),
            ("evidence_ids", "supporting_evidence_ids"),
            ("source_ids", "supporting_source_ids"),
        ):
            missing = set(final.get(final_key) or []) - set(trace.get(trace_key) or [])
            if missing:
                problem(f"TRACE_{trace_key.upper()}_MISSING", f"Traceability Manifest is missing {sorted(missing)}")
        sources = trace.get("sources") or payload.get("evidence_links") or []
        if not sources:
            problem("SOURCE_MANIFEST_MISSING", "Traceability Manifest contains no sources")
        elif any(not item.get("evidence_id") or not item.get("source_id") for item in sources):
            problem("SOURCE_MANIFEST_INVALID", "Every source manifest item requires evidence_id and source_id")
        return problems

    def _validate_envelope(self, handoff: PMPMessage) -> None:
        validated = self.pmp_validator.validate(handoff)
        checks = [
            (validated.sender_agent_id == "conclusion.manager", "sender_agent_id must be conclusion.manager"),
            (validated.receiver_agent_id == self.agent_id, "receiver_agent_id must be playwright.manager"),
            (validated.message_type == MessageType.CONCLUSION_HANDOFF.value, "message_type must be conclusion_handoff"),
        ]
        for passed, message in checks:
            if not passed:
                raise ValueError(f"HANDOFF_REJECTED: {message}")

    @staticmethod
    def _finding_to_upstream(state, finding) -> UpstreamConclusionRevisionRequest:
        return UpstreamConclusionRevisionRequest(
            revision_request_id=new_id("pw_upstream"),
            final_conclusion_id=state.final_conclusion["final_conclusion_id"],
            affected_claim_ids=list(state.final_conclusion.get("supporting_claim_ids") or []),
            affected_evidence_ids=list(state.final_conclusion.get("supporting_evidence_ids") or []),
            issue_type=finding.code,
            issue_description=finding.message,
            required_resolution="Conclusion正本を修正して新しいHandoffを発行する",
            acceptance_conditions=["Final Conclusionの不変性とTraceabilityが検証できる"],
            source_finding_ids=[finding.finding_id],
        )

    @staticmethod
    def _task_action(agent_id: str) -> str:
        return {
            NARRATIVE_ARCHITECT_ID: "narrative_design",
            SCRIPTWRITER_ID: "script_drafting",
            EVIDENCE_CITATION_EDITOR_ID: "citation_validation",
            VISUAL_DIRECTOR_ID: "visual_direction",
        }[agent_id]

    @staticmethod
    def _clear_from(state: PlaywrightWorkflowState, index: int) -> None:
        fields = [
            "narrative_blueprint",
            "script_draft",
            "citation_validated_script",
            "citation_manifest",
            "visual_plan",
        ]
        first_field_by_agent_index = {0: 0, 1: 1, 2: 2, 3: 4}
        for field in fields[first_field_by_agent_index[index]:]:
            setattr(state, field, None)
        for agent_id in AGENT_ORDER[index:]:
            if agent_id in state.completed_agents:
                state.completed_agents.remove(agent_id)
        state.deterministic_validation = None
        state.final_gate_result = None

    @staticmethod
    def _latest_revision_context(state: PlaywrightWorkflowState) -> dict[str, Any] | None:
        if not state.revision_history:
            return None
        return state.revision_history[-1].model_dump(mode="json")

    async def _fail(self, state, message: str, progress_callback):
        state.status = PlaywrightStatus.FAILED
        state.current_agent_ids = []
        state.error = {"stage": "playwright", "message": message}
        self.repository.save(state)
        await self._emit(progress_callback, f"Playwright失敗: {message}")
        return state

    @staticmethod
    async def _emit(callback: ProgressCallback | None, message: str) -> None:
        if callback is not None:
            await callback(message)
