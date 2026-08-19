from __future__ import annotations

from common.ids import new_id
from conclusion.schemas.evaluation_framework import EvaluationCriterion


def position_generation(input_data: dict, *, duplicate: bool = False) -> dict:
    context = input_data["decision_context"]
    claim_ids = context["key_claim_ids"][:2] or context["key_claim_ids"]
    evidence_ids = context["evidence_ids"][:2] or context["evidence_ids"]
    analysis_ids = context["analysis_ids"][:2] or context["analysis_ids"]
    stakeholder_ids = [item["stakeholder_id"] for item in context["affected_stakeholders"]]
    problem_id = str(context["target_problem"].get("problem_id") or context["deliberation_result_id"])

    def candidate(
        suffix: str,
        title: str,
        summary: str,
        position_type: str,
        action: str,
        actor: str,
        mechanism: str,
        time_horizon: str,
    ) -> dict:
        return {
            "position_candidate_id": f"position_{suffix}",
            "title": title,
            "summary": summary,
            "position_type": position_type,
            "normative_direction": "条件付き実施",
            "target_problem_ids": [problem_id],
            "target_stakeholder_ids": stakeholder_ids,
            "proposed_actions": [{"action_id": f"action_{suffix}", "action": action}],
            "responsible_actors": [actor],
            "mechanism_of_action": mechanism,
            "implementation_steps": ["対象範囲を定義する", action, "結果を検証して調整する"],
            "time_horizon": time_horizon,
            "required_resources": ["実施担当者", "評価データ"],
            "institutional_requirements": ["責任主体と評価基準の明文化"],
            "expected_benefits": ["問題への対応を段階的に進められる"],
            "expected_costs": ["実施・評価の費用"],
            "risks": ["対象外の主体へ負担が偏る可能性"],
            "tradeoffs": ["実施速度と慎重な検証の両立"],
            "unintended_consequences": ["形式的な対応に終わる可能性"],
            "supporting_claim_ids": claim_ids,
            "supporting_evidence_ids": evidence_ids,
            "supporting_analysis_ids": analysis_ids,
            "assumptions": ["Deliberationで示された因果関係が対象範囲内で成立する"],
            "success_conditions": ["評価指標と責任主体が事前に定義される"],
            "failure_conditions": ["検証結果を反映する仕組みがない"],
            "uncertainties": context["uncertainties"][:2],
            "limitations": context["limitations"][:2],
        }

    first = candidate(
        "a",
        "段階導入と移行支援",
        "段階的な導入と再訓練を組み合わせる",
        "phased_intervention",
        "限定導入と再訓練を実施する",
        "導入主体と公共部門",
        "限定導入で影響を検証し、技能移行で負担を緩和する",
        "短中期",
    )
    second = candidate(
        "b",
        "制度的保護と分配是正",
        "最低基準と移行支援制度を整える",
        "institutional_reform",
        "影響評価と保護基準を制度化する",
        "政府・規制主体",
        "共通ルールと再分配措置により影響の偏りを抑える",
        "中長期",
    )
    third = candidate(
        "c",
        "可逆的な試行と監視",
        "小規模な試行を行い、結果に応じて停止・拡張する",
        "reversible_pilot",
        "監視付きパイロットを実施する",
        "現場実施主体",
        "可逆性を確保した実験で不確実性を減らす",
        "短期",
    )
    if duplicate:
        second["title"] = first["title"]
        second["mechanism_of_action"] = first["mechanism_of_action"]
        second["proposed_actions"] = first["proposed_actions"]
    return {
        "position_generation_result_id": new_id("position_generation"),
        "task_id": input_data["task_id"],
        "decision_context_id": context["decision_context_id"],
        "position_candidates": [first, second, third],
        "diversity_dimensions": ["介入主体", "制度手段", "時間軸", "可逆性"],
        "generation_notes": ["Deliberationの追跡可能な範囲だけを使用"],
        "missing_information": [],
    }


def decision_evaluation(input_data: dict, *, blocking_candidate_id: str | None = None) -> dict:
    framework = input_data["evaluation_framework"]
    candidates = input_data["position_candidates"]
    evaluations = []
    matrix = []
    for candidate_index, candidate in enumerate(candidates):
        row = {"candidate_id": candidate["position_candidate_id"], "ratings": {}}
        for criterion in framework["criteria"]:
            rating = "HIGH" if candidate_index == 0 else "MEDIUM"
            if criterion == EvaluationCriterion.POLITICAL_FEASIBILITY.value and candidate_index == 2:
                rating = "NOT_EVALUABLE"
            blocking = candidate["position_candidate_id"] == blocking_candidate_id and criterion == EvaluationCriterion.LEGAL_FEASIBILITY.value
            evaluations.append(
                {
                    "candidate_id": candidate["position_candidate_id"],
                    "criterion": criterion,
                    "rating": "VERY_LOW" if blocking else rating,
                    "rationale": "共通の対象範囲とDeliberationのEvidenceに基づく定性的評価",
                    "supporting_claim_ids": candidate["supporting_claim_ids"],
                    "supporting_evidence_ids": candidate["supporting_evidence_ids"],
                    "supporting_analysis_ids": candidate["supporting_analysis_ids"],
                    "assumptions": candidate["assumptions"],
                    "uncertainties": candidate["uncertainties"],
                    "blocking_issue": blocking,
                    "blocking_reason": "法的必須条件と両立しない" if blocking else None,
                }
            )
            row["ratings"][criterion] = "VERY_LOW" if blocking else rating
        matrix.append(row)
    disqualified = []
    if blocking_candidate_id:
        disqualified.append(
            {"candidate_id": blocking_candidate_id, "reason": "非相殺の法的Blocking Issue"}
        )
    return {
        "decision_evaluation_result_id": new_id("decision_evaluation"),
        "task_id": input_data["task_id"],
        "decision_context_id": input_data["decision_context"]["decision_context_id"],
        "evaluation_framework": framework,
        "candidate_evaluations": evaluations,
        "comparison_matrix": matrix,
        "conditional_advantages": [
            {"profile_id": "effectiveness_priority", "advantaged_candidate_ids": [candidates[0]["position_candidate_id"]]},
            {"profile_id": "equity_priority", "advantaged_candidate_ids": [candidates[1]["position_candidate_id"]]},
        ],
        "disqualification_findings": disqualified,
        "sensitivity_analysis": [
            {"profile_id": "effectiveness_priority", "preferred_candidate_ids": [candidates[0]["position_candidate_id"]], "reason": "効果と直接性を優先"},
            {"profile_id": "equity_priority", "preferred_candidate_ids": [candidates[1]["position_candidate_id"]], "reason": "分配影響を優先"},
            {"profile_id": "risk_averse", "preferred_candidate_ids": [candidates[2]["position_candidate_id"]], "reason": "可逆性を優先"},
        ],
        "missing_information": [
            {"candidate_id": candidates[2]["position_candidate_id"], "criterion": "POLITICAL_FEASIBILITY", "status": "NOT_EVALUABLE"}
        ],
        "revision_recommendations": [],
        "status": "COMPLETED_WITH_UNCERTAINTY",
    }


def decision_integration(input_data: dict) -> dict:
    candidates = input_data["position_candidates"]
    evaluations = input_data["decision_evaluation"]
    blocked = {item["candidate_id"] for item in evaluations["disqualification_findings"]}
    viable = [item["position_candidate_id"] for item in candidates if item["position_candidate_id"] not in blocked]
    requested = input_data.get("requested_integration_candidate_ids") or viable[:2]
    requested = [item for item in requested if item in viable]
    integrated = None
    if len(requested) >= 2:
        integrated = {
            "integrated_option_id": new_id("integrated_option"),
            "candidate_ids": requested,
            "title": "段階導入と制度的保護の統合案",
            "summary": "可逆的な段階導入と分配保護を組み合わせる",
            "implementation_direction": "段階導入、影響評価、保護基準を一体で実施する",
            "non_combinable_elements": ["実施速度と事前規制の強度は別途選択が必要"],
        }
    return {
        "decision_integration_result_id": new_id("decision_integration"),
        "task_id": input_data["task_id"],
        "decision_evaluation_result_id": evaluations["decision_evaluation_result_id"],
        "viable_candidates": viable,
        "excluded_candidates": [
            {"candidate_id": item, "reason": "非相殺のBlocking Issue"} for item in sorted(blocked)
        ],
        "candidate_comparison_summary": [
            {"candidate_id": item["position_candidate_id"], "summary": item["summary"]}
            for item in candidates
            if item["position_candidate_id"] in viable
        ],
        "recommended_options": [
            {"candidate_id": item, "recommendation_type": "conditional", "reason": "優先価値により選択が変わる"}
            for item in viable
        ],
        "integrated_option": integrated,
        "unresolved_value_conflicts": [
            {"conflict_id": new_id("value_conflict"), "description": "即効性と分配公正の優先度"}
        ],
        "non_negotiable_constraints": input_data["decision_context"]["non_negotiable_constraints"],
        "major_tradeoffs": input_data["decision_context"]["tradeoffs"],
        "accepted_uncertainties": input_data["decision_context"]["uncertainties"],
        "human_decisions_required": [
            {"decision_id": new_id("human_decision"), "question": "効果、分配、公平、可逆性のどれを優先するか"}
        ],
        "limitations": input_data["decision_context"]["limitations"],
    }


def quality_review(input_data: dict, decision: str | None) -> dict:
    validation = input_data["deterministic_validation"]
    candidates = input_data["position_generation"]["position_candidates"]
    candidate_ids = [item["position_candidate_id"] for item in candidates]
    evaluation_id = input_data["decision_evaluation"]["decision_evaluation_result_id"]
    integration_id = input_data["decision_integration"]["decision_integration_result_id"]
    if decision is None:
        decision = "approved" if validation["passed"] else "blocked"
    base = {
        "review_id": new_id("conclusion_review"),
        "reason": "ConclusionのSchema、Traceability、責務境界、Human Gate準備状態を確認した",
        "playwright_readiness": "ready",
        "findings": [],
        "blocking_finding_ids": [],
        "revision_scope": "none",
        "revision_targets": [],
        "upstream_revision_requests": [],
        "limitations_to_disclose": [],
        "reviewed_candidate_ids": candidate_ids,
        "reviewed_evaluation_result_id": evaluation_id,
        "reviewed_integration_result_id": integration_id,
    }
    if decision == "revision_required":
        finding_id = new_id("finding")
        return {
            **base,
            "status": "revision_required",
            "reason": "候補間の実質的多様性を修正する必要がある",
            "playwright_readiness": "not_ready",
            "findings": [{
                "finding_id": finding_id,
                "severity": "MAJOR",
                "category": "candidate_diversity",
                "issue": "候補の介入手段が実質的に重複している",
                "required_action": "Position Candidateを実質的に区別する",
                "affected_agent_ids": ["conclusion.position_generator"],
                "affected_candidate_ids": candidate_ids[:2],
            }],
            "revision_scope": "targeted",
            "revision_targets": ["conclusion.position_generator"],
        }
    if decision == "evaluator_revision_required":
        finding_id = new_id("finding")
        return {
            **base,
            "status": "revision_required",
            "reason": "共通評価基準の適用を修正する必要がある",
            "playwright_readiness": "not_ready",
            "findings": [{
                "finding_id": finding_id,
                "severity": "MAJOR",
                "category": "evaluation_consistency",
                "issue": "候補間の評価条件が不統一",
                "required_action": "全候補を共通条件で再評価する",
                "affected_agent_ids": ["conclusion.decision_evaluator"],
                "affected_candidate_ids": candidate_ids,
            }],
            "revision_scope": "targeted",
            "revision_targets": ["conclusion.decision_evaluator"],
        }
    if decision == "upstream_revision_required":
        finding_id = new_id("finding")
        return {
            **base,
            "status": "revision_required",
            "reason": "Stakeholder分析が不足しておりConclusion内では修正できない",
            "playwright_readiness": "not_ready",
            "findings": [{
                "finding_id": finding_id,
                "severity": "MAJOR",
                "category": "upstream_analysis_gap",
                "issue": "影響主体別の実施負担が不足",
                "required_action": "DeliberationでStakeholder分析を補完する",
                "affected_agent_ids": ["deliberation.stakeholder_response_analyst"],
                "affected_candidate_ids": candidate_ids,
            }],
            "revision_scope": "deliberation_return",
            "upstream_revision_requests": [{
                "revision_request_id": new_id("upstream_revision"),
                "affected_candidate_ids": candidate_ids,
                "affected_claim_ids": [],
                "missing_analysis_description": "影響主体別の実施負担と権限の分析",
                "required_analysis_types": ["stakeholder_response_analysis"],
                "acceptance_conditions": ["stakeholder_idを持つ", "analysis_idとEvidenceへ追跡可能"],
                "source_finding_ids": [finding_id],
            }],
        }
    if decision == "blocked":
        finding_id = new_id("finding")
        return {
            **base,
            "status": "blocked",
            "reason": "決定論的検証またはWorkflow完全性を満たしていない",
            "playwright_readiness": "not_ready",
            "findings": [{
                "finding_id": finding_id,
                "severity": "CRITICAL",
                "category": "workflow_integrity",
                "issue": "修正不能なBlocking issueがある",
                "required_action": "入力またはWorkflowを修復する",
                "affected_agent_ids": [],
                "affected_candidate_ids": candidate_ids,
            }],
            "blocking_finding_ids": [finding_id],
        }
    if decision == "approved_with_conditions":
        return {
            **base,
            "status": "approved_with_conditions",
            "reason": "開示済みの制約を保持すればHuman Selectionへ進める",
            "playwright_readiness": "ready_with_conditions",
            "limitations_to_disclose": ["一部の実現可能性はNOT_EVALUABLE"],
        }
    return {**base, "status": "approved"}
