from __future__ import annotations

from collections import Counter
import json
from typing import Any

from common.ids import new_id
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult
from deliberation.schemas.causal_structural_analysis import CausalStructuralAnalysisResult
from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.identifiers import TASK_PREFIXES
from deliberation.schemas.integrated_analysis import (
    FinalIntegratedAnalysis,
    InitialIntegratedAnalysis,
)
from deliberation.schemas.review import (
    DeterministicValidationResult,
    ValidationFinding,
    ValidationMetrics,
    ValidationTargetSet,
)
from deliberation.schemas.stakeholder_response_analysis import (
    StakeholderResponseAnalysisResult,
)
from researcher.schemas.research_report import ResearchReport


class DeliberationValidator:
    """Cross-check one explicitly defined set of saved Deliberation artifacts."""

    def validate(
        self,
        *,
        report: ResearchReport,
        primary_analyses: dict[str, dict[str, Any]],
        initial_integration: InitialIntegratedAnalysis,
        counterargument: CounterargumentAnalysisResult,
        final_integration: FinalIntegratedAnalysis,
        revision_count: int,
    ) -> DeterministicValidationResult:
        findings: list[ValidationFinding] = []
        report_evidence_ids = {item.evidence_id for item in report.evidence_items}
        report_source_ids = {item.source_id for item in report.evidence_items}
        source_by_evidence = {
            item.evidence_id: item.source_id for item in report.evidence_items
        }

        if len(primary_analyses) < 2:
            findings.append(
                self._finding(
                    "CRITICAL",
                    "workflow",
                    "Fewer than two primary analyses completed",
                )
            )

        parsed: list[Any] = []
        schema_by_agent = {
            "deliberation.argument_analyst": ArgumentAnalysisResult,
            "deliberation.causal_structural_analyst": CausalStructuralAnalysisResult,
            "deliberation.stakeholder_response_analyst": StakeholderResponseAnalysisResult,
        }
        for agent_id, payload in primary_analyses.items():
            schema = schema_by_agent.get(agent_id)
            if schema is None:
                findings.append(
                    self._finding(
                        "ERROR",
                        "schema",
                        f"Unknown primary analysis owner: {agent_id}",
                    )
                )
                continue
            parsed.append(schema.model_validate(payload))

        analysis_ids = [item.analysis_id for item in parsed] + [
            counterargument.analysis_id
        ]
        task_ids = [item.task_id for item in parsed] + [counterargument.task_id]
        integration_ids = [
            initial_integration.integration_id,
            final_integration.integration_id,
        ]
        all_workflow_ids = analysis_ids + task_ids + integration_ids
        collisions = sorted(
            identifier
            for identifier, count in Counter(all_workflow_ids).items()
            if count > 1
        )
        if collisions:
            findings.append(
                self._finding(
                    "ERROR",
                    "identifier",
                    "analysis_id, task_id, and integration_id namespaces collide",
                    collisions,
                )
            )
        invalid_task_ids = sorted(
            item for item in task_ids if not item.startswith(TASK_PREFIXES)
        )
        if invalid_task_ids:
            findings.append(
                self._finding(
                    "ERROR",
                    "identifier",
                    "task_id values must use delib_task_* or counter_task_*",
                    invalid_task_ids,
                )
            )

        claim_ids = {
            claim.claim_id for claim in final_integration.key_claims
        }
        claim_ids.update(
            item.item_id for item in final_integration.causal_structure.causal_claims
        )
        claim_ids.update(
            claim_id
            for viewpoint in final_integration.major_viewpoints
            for claim_id in viewpoint.supporting_claim_ids
        )
        claim_ids.update(
            claim_id
            for item in counterargument.counterarguments
            for claim_id in item.target_claim_ids
        )
        viewpoint_ids = {
            viewpoint.viewpoint_id for viewpoint in final_integration.major_viewpoints
        }
        counterargument_ids = {
            item.counterargument_id for item in counterargument.counterarguments
        }
        challenge_ids = {
            item.challenge_id for item in counterargument.steelman_arguments
        }
        required_revision_ids = {
            item.revision_id for item in counterargument.required_revisions
        }
        integration_change_ids = {
            item.change_id for item in final_integration.integration_changes
        }

        referenced_evidence: set[str] = set()
        for item in parsed:
            referenced_evidence.update(
                self._collect_evidence_ids(item.model_dump(mode="json"))
            )
        for artifact in (
            initial_integration,
            counterargument,
            final_integration,
        ):
            referenced_evidence.update(
                self._collect_evidence_ids(artifact.model_dump(mode="json"))
            )
        unknown_evidence = referenced_evidence - report_evidence_ids
        if unknown_evidence:
            findings.append(
                self._finding(
                    "ERROR",
                    "traceability",
                    "Analysis references unknown evidence IDs",
                    sorted(unknown_evidence),
                )
            )

        referenced_source_ids: set[str] = set()
        known_analysis_ids = set(analysis_ids)
        known_integration_ids = set(integration_ids)
        known_task_ids = set(task_ids)
        for index, entry in enumerate(final_integration.traceability_index):
            path = f"final_integration.traceability_index[{index}]"
            referenced_source_ids.update(entry.source_ids)
            self._append_unknown_reference_finding(
                findings,
                path,
                "evidence_ids",
                set(entry.evidence_ids) - report_evidence_ids,
            )
            self._append_unknown_reference_finding(
                findings,
                path,
                "source_ids",
                set(entry.source_ids) - report_source_ids,
            )
            self._append_unknown_reference_finding(
                findings,
                path,
                "analysis_ids",
                set(entry.analysis_ids) - known_analysis_ids,
            )
            self._append_unknown_reference_finding(
                findings,
                path,
                "claim_ids",
                set(entry.claim_ids) - claim_ids,
            )
            self._append_unknown_reference_finding(
                findings,
                path,
                "counterargument_ids",
                set(entry.counterargument_ids) - counterargument_ids,
            )
            self._append_unknown_reference_finding(
                findings,
                path,
                "challenge_ids",
                set(entry.challenge_ids) - challenge_ids,
            )
            self._append_unknown_reference_finding(
                findings,
                path,
                "integration_ids",
                set(entry.integration_ids) - known_integration_ids,
            )
            self._append_unknown_reference_finding(
                findings,
                path,
                "task_ids",
                set(entry.task_ids) - known_task_ids,
            )
            missing_sources = sorted(
                {
                    source_by_evidence[evidence_id]
                    for evidence_id in entry.evidence_ids
                    if evidence_id in source_by_evidence
                }
                - set(entry.source_ids)
            )
            if missing_sources:
                findings.append(
                    self._finding(
                        "ERROR",
                        "traceability",
                        f"{path} cannot complete evidence -> source traversal",
                        missing_sources,
                    )
                )

        traced_claim_ids = {
            claim_id
            for entry in final_integration.traceability_index
            for claim_id in entry.claim_ids
            if entry.analysis_ids and entry.evidence_ids and entry.source_ids
        }
        untraced_final_claims = {
            claim.claim_id for claim in final_integration.key_claims
        } - traced_claim_ids
        if untraced_final_claims:
            findings.append(
                self._finding(
                    "ERROR",
                    "traceability",
                    "Final claims require claim -> analysis -> evidence -> source traceability",
                    sorted(untraced_final_claims),
                )
            )

        stakeholder_analysis = next(
            (
                item
                for item in parsed
                if isinstance(item, StakeholderResponseAnalysisResult)
            ),
            None,
        )
        if stakeholder_analysis:
            serialized_final = json.dumps(
                final_integration.model_dump(mode="json"),
                ensure_ascii=False,
            )
            promoted_unverified = sorted(
                fact.fact_id
                for fact in stakeholder_analysis.specific_facts
                if fact.verification_status in {"unknown", "unverified"}
                and fact.statement in serialized_final
            )
            if promoted_unverified:
                findings.append(
                    self._finding(
                        "ERROR",
                        "stakeholder_evidence_boundary",
                        "Final integration promoted unverified stakeholder specifics",
                        promoted_unverified,
                    )
                )

        if final_integration.previous_integration_id != initial_integration.integration_id:
            findings.append(
                self._finding(
                    "ERROR",
                    "lineage",
                    "Final integration does not reference initial integration",
                )
            )
        if len(final_integration.major_viewpoints) > 3:
            findings.append(
                self._finding(
                    "ERROR",
                    "viewpoint",
                    "More than three major viewpoints were produced",
                )
            )
        if not counterargument.required_revisions:
            findings.append(
                self._finding(
                    "ERROR",
                    "counterargument",
                    "Counterargument analysis produced no integration revisions",
                )
            )

        unrouted = counterargument.unrouted_required_counterargument_ids()
        if unrouted:
            findings.append(
                self._finding(
                    "ERROR",
                    "counterargument_routing",
                    "Blocking counterarguments were omitted from required_revisions",
                    unrouted,
                )
            )

        dispositions = {
            item.counterargument_id: item
            for item in final_integration.counterargument_dispositions
        }
        unknown_dispositions = set(dispositions) - counterargument_ids
        if unknown_dispositions:
            findings.append(
                self._finding(
                    "ERROR",
                    "counterargument_routing",
                    "Final integration contains dispositions for unknown counterarguments",
                    sorted(unknown_dispositions),
                )
            )
        missing_dispositions = {
            item.counterargument_id
            for item in counterargument.counterarguments
            if item.required_revision and item.counterargument_id not in dispositions
        }
        if missing_dispositions:
            findings.append(
                self._finding(
                    "ERROR",
                    "counterargument_routing",
                    "Every blocking counterargument needs a revised, rejected, unresolved, or researcher_return disposition",
                    sorted(missing_dispositions),
                )
            )
        for counterargument_id, disposition in dispositions.items():
            unknown_changes = set(disposition.integration_change_ids) - integration_change_ids
            if unknown_changes:
                findings.append(
                    self._finding(
                        "ERROR",
                        "counterargument_routing",
                        f"Disposition {counterargument_id} references unknown integration changes",
                        sorted(unknown_changes),
                    )
                )

        uncertainty_values = set(final_integration.uncertainties)
        uncertainty_values.update(final_integration.causal_structure.uncertainties)
        uncertainty_values.update(final_integration.stakeholder_structure.uncertainties)
        uncertainty_values.update(
            uncertainty
            for viewpoint in final_integration.major_viewpoints
            for uncertainty in viewpoint.uncertainties
        )
        uncertainty_values.update(counterargument.remaining_uncertainties)
        uncertainty_values.update(
            item.remaining_uncertainty
            for item in counterargument.counterarguments
            if item.remaining_uncertainty
        )
        missing_parent_uncertainties = {
            item.remaining_uncertainty
            for item in counterargument.counterarguments
            if item.remaining_uncertainty
            and item.remaining_uncertainty not in counterargument.remaining_uncertainties
        }
        if missing_parent_uncertainties:
            findings.append(
                self._finding(
                    "ERROR",
                    "counterargument_completion",
                    "Counterargument item uncertainties are absent from remaining_uncertainties",
                    sorted(missing_parent_uncertainties),
                )
            )

        unresolved_ids = {
            item.item_id for item in final_integration.unresolved_questions
        }
        unresolved_ids.update(
            item.counterargument_id
            for item in final_integration.counterargument_dispositions
            if item.resolution in {"unresolved", "researcher_return"}
        )

        unreferenced = report_evidence_ids - referenced_evidence
        if unreferenced:
            findings.append(
                self._finding(
                    "WARNING",
                    "traceability",
                    "Some Research Report evidence is not referenced by Deliberation artifacts",
                    sorted(unreferenced),
                )
            )

        critical = {"ERROR", "CRITICAL"}
        targets = ValidationTargetSet(
            analysis_ids=sorted(set(analysis_ids)),
            task_ids=sorted(set(task_ids)),
            integration_ids=sorted(set(integration_ids)),
            claim_ids=sorted(claim_ids),
            viewpoint_ids=sorted(viewpoint_ids),
            evidence_ids=sorted(referenced_evidence),
            source_ids=sorted(referenced_source_ids),
            counterargument_ids=sorted(counterargument_ids),
            required_revision_ids=sorted(required_revision_ids),
            integration_change_ids=sorted(integration_change_ids),
            unresolved_ids=sorted(unresolved_ids),
            uncertainties=sorted(uncertainty_values),
        )
        metrics = ValidationMetrics(
            primary_analysis_count=len(parsed),
            analysis_id_count=len(targets.analysis_ids),
            task_id_count=len(targets.task_ids),
            integration_id_count=len(targets.integration_ids),
            claim_count=len(targets.claim_ids),
            viewpoint_count=len(targets.viewpoint_ids),
            evidence_reference_count=len(targets.evidence_ids),
            report_evidence_count=len(report_evidence_ids),
            source_count=len(targets.source_ids),
            report_source_count=len(report_source_ids),
            counterargument_count=len(targets.counterargument_ids),
            required_revision_count=len(targets.required_revision_ids),
            integration_change_count=len(targets.integration_change_ids),
            unresolved_count=len(targets.unresolved_ids),
            uncertainty_count=len(targets.uncertainties),
            workflow_revision_count=revision_count,
        )
        return DeterministicValidationResult(
            schema_version="2.0",
            passed=not any(item.severity.upper() in critical for item in findings),
            findings=findings,
            metrics=metrics,
            validation_targets=targets,
        )

    @staticmethod
    def _append_unknown_reference_finding(
        findings: list[ValidationFinding],
        path: str,
        field_name: str,
        unknown: set[str],
    ) -> None:
        if unknown:
            findings.append(
                DeliberationValidator._finding(
                    "ERROR",
                    "traceability",
                    f"{path}.{field_name} contains unknown IDs",
                    sorted(unknown),
                )
            )

    @staticmethod
    def _collect_evidence_ids(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_id" and isinstance(item, str):
                    found.add(item)
                elif key.endswith("evidence_ids") and isinstance(item, list):
                    found.update(part for part in item if isinstance(part, str))
                else:
                    found.update(DeliberationValidator._collect_evidence_ids(item))
        elif isinstance(value, list):
            for item in value:
                found.update(DeliberationValidator._collect_evidence_ids(item))
        return found

    @staticmethod
    def _finding(
        severity: str,
        category: str,
        message: str,
        affected_ids: list[str] | None = None,
    ) -> ValidationFinding:
        return ValidationFinding(
            finding_id=new_id("validation"),
            severity=severity,
            category=category,
            message=message,
            affected_ids=affected_ids or [],
        )
