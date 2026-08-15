from __future__ import annotations

import json
from typing import Any

from common.ids import new_id
from conclusion.schemas.conclusion_package import ConclusionPackage
from conclusion.schemas.decision_context import DecisionContext
from conclusion.schemas.decision_evaluation import DecisionEvaluationResult
from conclusion.schemas.decision_integration import DecisionIntegrationResult
from conclusion.schemas.position_candidate import PositionGenerationResult
from conclusion.schemas.review import DeterministicValidationResult, ValidationFinding


class ConclusionValidator:
    def validate(
        self,
        *,
        decision_context: DecisionContext,
        position_generation: PositionGenerationResult,
        decision_evaluation: DecisionEvaluationResult,
        decision_integration: DecisionIntegrationResult,
        conclusion_package: ConclusionPackage,
        human_selection_present: bool = False,
    ) -> DeterministicValidationResult:
        findings: list[ValidationFinding] = []

        def add(category: str, message: str, affected: list[str] | None = None) -> None:
            findings.append(
                ValidationFinding(
                    finding_id=new_id("conclusion_validation"),
                    severity="CRITICAL" if category in {"traceability", "blocking_issue"} else "MAJOR",
                    category=category,
                    message=message,
                    affected_ids=affected or [],
                )
            )

        candidates = position_generation.position_candidates
        candidate_ids = [item.position_candidate_id for item in candidates]
        if len(candidates) < 2 or len(candidates) > 5:
            add("candidate_count", "Position candidates must contain between two and five items")
        if len(candidate_ids) != len(set(candidate_ids)):
            add("candidate_id", "Position candidate IDs are not unique", candidate_ids)
        duplicates = self._duplicate_pairs(candidates)
        if duplicates:
            add("candidate_diversity", f"Substantively duplicate candidates: {duplicates}")

        valid_claims = set(decision_context.key_claim_ids)
        valid_evidence = set(decision_context.evidence_ids)
        valid_analysis = set(decision_context.analysis_ids)
        valid_sources = set(decision_context.source_ids)
        for candidate in candidates:
            unknown_claims = set(candidate.supporting_claim_ids) - valid_claims
            unknown_evidence = set(candidate.supporting_evidence_ids) - valid_evidence
            unknown_analysis = set(candidate.supporting_analysis_ids) - valid_analysis
            if unknown_claims or unknown_evidence or unknown_analysis:
                add(
                    "traceability",
                    f"{candidate.position_candidate_id} references unknown traceability IDs",
                    sorted(unknown_claims | unknown_evidence | unknown_analysis),
                )

        framework = decision_evaluation.evaluation_framework
        expected_criteria = set(framework.criteria)
        evaluated_candidates = {item.candidate_id for item in decision_evaluation.candidate_evaluations}
        if evaluated_candidates != set(candidate_ids):
            add("evaluation_coverage", "Decision Evaluator did not evaluate exactly every candidate")
        for candidate_id in candidate_ids:
            criteria = {
                item.criterion
                for item in decision_evaluation.candidate_evaluations
                if item.candidate_id == candidate_id
            }
            if criteria != expected_criteria:
                add("evaluation_framework", f"Criteria are incomplete or inconsistent for {candidate_id}")
        if not framework.not_evaluable_is_not_zero:
            add("not_evaluable", "NOT_EVALUABLE must not be converted to zero")

        blocking_ids = {
            item.candidate_id
            for item in decision_evaluation.candidate_evaluations
            if item.blocking_issue
        }
        viable = set(decision_integration.viable_candidates)
        if blocking_ids & viable:
            add(
                "blocking_issue",
                "Candidates with a blocking issue were treated as viable",
                sorted(blocking_ids & viable),
            )
        excluded = {item.candidate_id for item in decision_integration.excluded_candidates}
        if set(candidate_ids) != viable | excluded:
            add("integration_coverage", "Integrator did not account for every candidate")

        raw_evaluation = decision_evaluation.model_dump(mode="json")
        if self._contains_key(raw_evaluation, {"final_selection", "selected_candidate_id", "final_candidate_id"}):
            add("role_boundary", "Decision Evaluator attempted a final selection")
        unknown_integration_evidence = self._collect_id_values(
            decision_integration.model_dump(mode="json"), "evidence_id"
        ) - valid_evidence
        if unknown_integration_evidence:
            add("traceability", "Decision Integrator introduced unknown evidence IDs", sorted(unknown_integration_evidence))

        package_ids = {
            str(item.get("position_candidate_id") or item.get("candidate_id"))
            for item in conclusion_package.options
        }
        if package_ids != set(candidate_ids):
            add("package_coverage", "Conclusion Package options do not match generated candidates")
        package_evidence = self._collect_id_values(conclusion_package.evidence_traceability, "evidence_id")
        package_sources = self._collect_id_values(conclusion_package.evidence_traceability, "source_id")
        package_analyses = self._collect_id_values(conclusion_package.analysis_traceability, "analysis_id")
        if not package_evidence or package_evidence - valid_evidence:
            add("traceability", "Conclusion Package evidence traceability is incomplete or unknown")
        if not package_sources or package_sources - valid_sources:
            add("traceability", "Conclusion Package source traceability is incomplete or unknown")
        if not package_analyses or package_analyses - valid_analysis:
            add("traceability", "Conclusion Package analysis traceability is incomplete or unknown")
        if human_selection_present or not conclusion_package.selection_required:
            add("human_gate", "Package was marked final before the human selection gate")

        return DeterministicValidationResult(
            passed=not findings,
            findings=findings,
            metrics={
                "candidate_count": len(candidates),
                "evaluation_count": len(decision_evaluation.candidate_evaluations),
                "blocking_candidate_count": len(blocking_ids),
                "finding_count": len(findings),
            },
        )

    @staticmethod
    def _duplicate_pairs(candidates) -> list[list[str]]:
        signatures: dict[str, str] = {}
        duplicates: list[list[str]] = []
        for candidate in candidates:
            tokens = [candidate.title, candidate.mechanism_of_action]
            tokens += [item.action for item in candidate.proposed_actions]
            signature = " ".join("".join(tokens).lower().split())
            prior = signatures.get(signature)
            if prior:
                duplicates.append([prior, candidate.position_candidate_id])
            else:
                signatures[signature] = candidate.position_candidate_id
        return duplicates

    @classmethod
    def _contains_key(cls, value: Any, keys: set[str]) -> bool:
        if isinstance(value, dict):
            return bool(keys & value.keys()) or any(cls._contains_key(child, keys) for child in value.values())
        if isinstance(value, list):
            return any(cls._contains_key(child, keys) for child in value)
        return False

    @classmethod
    def _collect_id_values(cls, value: Any, key_fragment: str) -> set[str]:
        result: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                if key == key_fragment and isinstance(child, str):
                    result.add(child)
                elif key == key_fragment + "s" and isinstance(child, list):
                    result.update(str(item) for item in child)
                result.update(cls._collect_id_values(child, key_fragment))
        elif isinstance(value, list):
            for child in value:
                result.update(cls._collect_id_values(child, key_fragment))
        return result

    @staticmethod
    def stable_snapshot(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
