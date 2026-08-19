from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

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
        comparison_ids = [
            item.candidate_id
            for item in decision_integration.candidate_comparison_summary
        ]
        if len(comparison_ids) != len(set(comparison_ids)) or set(comparison_ids) != viable:
            add(
                "integration_coverage",
                "Integrator comparison summaries must cover every viable candidate exactly once",
                comparison_ids,
            )
        recommended_ids = [
            item.candidate_id for item in decision_integration.recommended_options
        ]
        if len(recommended_ids) != len(set(recommended_ids)) or not set(
            recommended_ids
        ).issubset(viable):
            add(
                "integration_coverage",
                "Integrator recommendations must be unique viable candidates",
                recommended_ids,
            )

        raw_evaluation = decision_evaluation.model_dump(mode="json")
        if self._contains_key(raw_evaluation, {"final_selection", "selected_candidate_id", "final_candidate_id"}):
            add("role_boundary", "Decision Evaluator attempted a final selection")
        reference_artifacts = (
            ("Position Generation", position_generation),
            ("Decision Evaluation", decision_evaluation),
            ("Decision Integration", decision_integration),
            ("Conclusion Package", conclusion_package),
        )
        for label, artifact in reference_artifacts:
            violations = self.unknown_reference_ids(
                decision_context=decision_context,
                value=artifact,
                candidate_ids=set(candidate_ids),
            )
            if violations:
                add(
                    "traceability",
                    f"{label} contains references outside canonical input sets",
                    sorted({item["id"] for item in violations}),
                )

        package_ids = {
            str(item.get("position_candidate_id") or item.get("candidate_id"))
            for item in conclusion_package.options
        }
        if package_ids != set(candidate_ids):
            add("package_coverage", "Conclusion Package options do not match generated candidates")
        primary_candidate_id = None
        if conclusion_package.primary_recommendation:
            primary_candidate_id = conclusion_package.primary_recommendation.get(
                "candidate_id"
            )
        expected_alternative_ids = viable - (
            {str(primary_candidate_id)} if primary_candidate_id else set()
        )
        alternative_ids = [
            str(item.get("candidate_id"))
            for item in conclusion_package.alternatives
            if isinstance(item, dict) and item.get("candidate_id")
        ]
        actual_alternative_ids = set(alternative_ids)
        if (
            len(alternative_ids) != len(actual_alternative_ids)
            or actual_alternative_ids != expected_alternative_ids
        ):
            add(
                "alternative_coverage",
                "Conclusion Package alternatives must contain every viable non-primary candidate exactly once",
                sorted(expected_alternative_ids ^ actual_alternative_ids),
            )
        incomplete_alternatives = [
            str(item.get("candidate_id") or "<missing_candidate_id>")
            for item in conclusion_package.alternatives
            if not isinstance(item, dict)
            or not str(item.get("reason") or "").strip()
            or not isinstance(item.get("applicable_conditions"), list)
            or not item.get("applicable_conditions")
        ]
        if incomplete_alternatives:
            add(
                "alternative_detail",
                "Every alternative must preserve its upstream reason and applicable conditions",
                incomplete_alternatives,
            )
        package_evidence = self._collect_id_values(conclusion_package.evidence_traceability, "evidence_id")
        package_sources = self._collect_id_values(conclusion_package.evidence_traceability, "source_id")
        package_analyses = self._collect_id_values(conclusion_package.analysis_traceability, "analysis_id")
        valid_evidence = set(decision_context.evidence_ids)
        valid_sources = set(decision_context.source_ids)
        valid_analysis = set(decision_context.analysis_ids)
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
                "viable_candidate_count": len(viable),
                "expected_alternative_count": len(expected_alternative_ids),
                "alternative_count": len(actual_alternative_ids),
                "missing_alternative_count": len(
                    expected_alternative_ids - actual_alternative_ids
                ),
                "finding_count": len(findings),
            },
        )

    @classmethod
    def unknown_reference_ids(
        cls,
        *,
        decision_context: DecisionContext,
        value: Any,
        candidate_ids: set[str] | None = None,
    ) -> list[dict[str, str]]:
        """Return structured references that are absent from canonical inputs.

        Only explicit ID fields are inspected. Narrative strings remain free
        text and are never rewritten or interpreted as identifiers.
        """

        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        allowlists = cls._reference_allowlists(
            decision_context,
            candidate_ids=candidate_ids,
        )
        violations: list[dict[str, str]] = []

        def walk(current: Any, path: str) -> None:
            if isinstance(current, dict):
                for field, child in current.items():
                    child_path = f"{path}.{field}" if path else field
                    kind = cls._reference_kind(field)
                    if kind is not None:
                        values = child if isinstance(child, list) else [child]
                        for index, item in enumerate(values):
                            if not isinstance(item, str) or not item:
                                continue
                            if item not in allowlists[kind]:
                                item_path = (
                                    f"{child_path}[{index}]"
                                    if isinstance(child, list)
                                    else child_path
                                )
                                violations.append(
                                    {"kind": kind, "id": item, "path": item_path}
                                )
                    walk(child, child_path)
            elif isinstance(current, list):
                for index, child in enumerate(current):
                    walk(child, f"{path}[{index}]")

        walk(value, "")
        return sorted(
            violations,
            key=lambda item: (item["path"], item["kind"], item["id"]),
        )

    @classmethod
    def canonical_decision_context_view(
        cls,
        decision_context: DecisionContext,
    ) -> tuple[DecisionContext, list[dict[str, str]]]:
        """Build an Agent-facing view without unknown structured references.

        The persisted Deliberation Result remains authoritative and unchanged.
        Only explicit ID fields are filtered; narrative descriptions and other
        free-form content are preserved byte-for-byte through model dumping.
        """

        allowlists = cls._reference_allowlists(decision_context)
        raw = decision_context.model_dump(mode="json")
        removed: list[dict[str, str]] = []

        def walk(current: Any, path: str) -> None:
            if isinstance(current, dict):
                for field in list(current):
                    child = current[field]
                    child_path = f"{path}.{field}" if path else field
                    kind = cls._reference_kind(field)
                    if kind is not None and kind != "candidate":
                        if isinstance(child, list):
                            kept: list[Any] = []
                            for index, item in enumerate(child):
                                if (
                                    isinstance(item, str)
                                    and item
                                    and item not in allowlists[kind]
                                ):
                                    removed.append(
                                        {
                                            "kind": kind,
                                            "id": item,
                                            "path": f"{child_path}[{index}]",
                                        }
                                    )
                                else:
                                    kept.append(item)
                            current[field] = kept
                            child = kept
                        elif (
                            isinstance(child, str)
                            and child
                            and child not in allowlists[kind]
                        ):
                            removed.append(
                                {"kind": kind, "id": child, "path": child_path}
                            )
                            current.pop(field)
                            continue
                    walk(child, child_path)
            elif isinstance(current, list):
                for index, child in enumerate(current):
                    walk(child, f"{path}[{index}]")

        walk(raw, "")
        return DecisionContext.model_validate(raw), sorted(
            removed,
            key=lambda item: (item["path"], item["kind"], item["id"]),
        )

    @staticmethod
    def _reference_allowlists(
        decision_context: DecisionContext,
        *,
        candidate_ids: set[str] | None = None,
    ) -> dict[str, set[str]]:
        stakeholder_ids = {
            str(item.get("stakeholder_id"))
            for item in decision_context.affected_stakeholders
            if item.get("stakeholder_id")
        }
        problem_ids = {
            str(decision_context.deliberation_result_id),
            str(
                decision_context.target_problem.get("problem_id")
                or decision_context.deliberation_result_id
            ),
        }
        return {
            "claim": set(decision_context.key_claim_ids),
            "evidence": set(decision_context.evidence_ids),
            "analysis": set(decision_context.analysis_ids),
            "source": set(decision_context.source_ids),
            "candidate": set(candidate_ids or set()),
            "stakeholder": stakeholder_ids,
            "problem": problem_ids,
        }

    @staticmethod
    def _reference_kind(field: str) -> str | None:
        for kind in (
            "claim",
            "evidence",
            "analysis",
            "source",
            "candidate",
            "stakeholder",
            "problem",
        ):
            if field in {f"{kind}_id", f"{kind}_ids"} or field.endswith(
                (f"_{kind}_id", f"_{kind}_ids")
            ):
                return kind
        if field == "viable_candidates":
            return "candidate"
        return None

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
