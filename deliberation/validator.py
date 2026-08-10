from __future__ import annotations

from typing import Any

from common.ids import new_id
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult
from deliberation.schemas.causal_structural_analysis import CausalStructuralAnalysisResult
from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.integrated_analysis import FinalIntegratedAnalysis, InitialIntegratedAnalysis
from deliberation.schemas.review import DeterministicValidationResult, ValidationFinding
from deliberation.schemas.stakeholder_response_analysis import StakeholderResponseAnalysisResult
from researcher.schemas.research_report import ResearchReport


class DeliberationValidator:
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
        evidence_ids = {item.evidence_id for item in report.evidence_items}
        source_ids = {item.source_id for item in report.evidence_items}

        if len(primary_analyses) < 2:
            findings.append(self._finding("CRITICAL", "workflow", "Fewer than two primary analyses completed"))

        parsed: list[Any] = []
        schema_by_agent = {
            "deliberation.argument_analyst": ArgumentAnalysisResult,
            "deliberation.causal_structural_analyst": CausalStructuralAnalysisResult,
            "deliberation.stakeholder_response_analyst": StakeholderResponseAnalysisResult,
        }
        for agent_id, payload in primary_analyses.items():
            schema = schema_by_agent.get(agent_id)
            if schema is None:
                findings.append(self._finding("ERROR", "schema", f"Unknown primary analysis owner: {agent_id}"))
                continue
            parsed.append(schema.model_validate(payload))

        analysis_ids = [item.analysis_id for item in parsed] + [counterargument.analysis_id]
        if len(set(analysis_ids)) != len(analysis_ids):
            findings.append(self._finding("ERROR", "identifier", "analysis_id values must be unique"))

        argument = next((item for item in parsed if isinstance(item, ArgumentAnalysisResult)), None)
        claim_ids: set[str] = set()
        if argument:
            ids = [claim.claim_id for claim in argument.central_claims]
            claim_ids = set(ids)
            if len(claim_ids) != len(ids):
                findings.append(self._finding("ERROR", "identifier", "claim_id values must be unique"))

        referenced_evidence: set[str] = set()
        for item in parsed:
            referenced_evidence.update(self._collect_evidence_ids(item.model_dump(mode="json")))
        referenced_evidence.update(self._collect_evidence_ids(counterargument.model_dump(mode="json")))
        referenced_evidence.update(
            evidence_id
            for viewpoint in final_integration.major_viewpoints
            for evidence_id in viewpoint.supporting_evidence_ids
        )
        unknown_evidence = referenced_evidence - evidence_ids
        if unknown_evidence:
            findings.append(
                self._finding(
                    "ERROR",
                    "traceability",
                    "Analysis references unknown evidence IDs",
                    sorted(unknown_evidence),
                )
            )

        if final_integration.previous_integration_id != initial_integration.integration_id:
            findings.append(self._finding("ERROR", "lineage", "Final integration does not reference initial integration"))
        if len(final_integration.major_viewpoints) > 3:
            findings.append(self._finding("ERROR", "viewpoint", "More than three major viewpoints were produced"))
        if not counterargument.required_revisions:
            findings.append(self._finding("ERROR", "counterargument", "Counterargument analysis produced no integration revisions"))

        unreferenced = evidence_ids - referenced_evidence
        if unreferenced:
            findings.append(
                self._finding(
                    "WARNING",
                    "traceability",
                    "Some evidence is not referenced by a final viewpoint",
                    sorted(unreferenced),
                )
            )
        critical = {"ERROR", "CRITICAL"}
        return DeterministicValidationResult(
            passed=not any(item.severity in critical for item in findings),
            findings=findings,
            metrics={
                "primary_analysis_count": len(primary_analyses),
                "analysis_id_count": len(analysis_ids),
                "claim_id_count": len(claim_ids),
                "evidence_id_count": len(evidence_ids),
                "source_id_count": len(source_ids),
                "referenced_evidence_count": len(referenced_evidence & evidence_ids),
                "unreferenced_evidence_count": len(unreferenced),
                "viewpoint_count": len(final_integration.major_viewpoints),
                "revision_count": revision_count,
            },
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
