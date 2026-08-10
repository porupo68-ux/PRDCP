from __future__ import annotations

from common.ids import new_id


def argument_analysis(input_data: dict) -> dict:
    evidence_ids = input_data["target_evidence_ids"][:2]
    claim_id = new_id("claim")
    return {
        "analysis_id": new_id("analysis_argument"),
        "task_id": input_data["task_id"],
        "central_claims": [
            {
                "claim_id": claim_id,
                "statement": "生成AI導入は一部業務を代替する一方、職務全体への影響は条件に依存する",
                "claim_type": "DESCRIPTIVE",
                "importance": "HIGH",
                "evidence_ids": evidence_ids,
                "support_status": "PARTIALLY_SUPPORTED",
            }
        ],
        "premises": [
            {
                "premise_id": new_id("premise"),
                "statement": "業務単位の代替と雇用者数の変化は区別する必要がある",
                "claim_ids": [claim_id],
                "evidence_ids": evidence_ids,
            }
        ],
        "warrants": [
            {
                "warrant_id": new_id("warrant"),
                "statement": "技術導入の影響は導入範囲と補完的業務の有無で変わる",
                "claim_ids": [claim_id],
            }
        ],
        "logical_gaps": [],
        "descriptive_claim_ids": [claim_id],
        "normative_claim_ids": [],
        "evidence_mappings": [
            {
                "claim_id": claim_id,
                "evidence_ids": evidence_ids,
                "relationship": "PARTIAL_SUPPORT",
            }
        ],
        "exception_conditions": ["産業、職種、導入速度が異なる場合"],
        "scope_conditions": list(input_data["geographic_scope"]),
        "uncertainties": ["長期の雇用純増減は現時点の証拠だけでは確定できない"],
    }


def causal_analysis(input_data: dict) -> dict:
    evidence_ids = input_data["target_evidence_ids"][:2]
    return {
        "analysis_id": new_id("analysis_causal"),
        "task_id": input_data["task_id"],
        "causal_claims": [
            _causal_item("causal", "定型業務の自動化が業務構成を変える", evidence_ids, "PLAUSIBLE")
        ],
        "mechanisms": [
            _causal_item("mechanism", "導入コスト低下から反復作業の代替へ至る", evidence_ids, "SUPPORTED")
        ],
        "structural_factors": [
            _causal_item("structure", "再訓練機会と労働移動制度", evidence_ids, "MATERIAL")
        ],
        "feedback_loops": [],
        "alternative_explanations": [
            _causal_item("alternative", "景気変動や人口構成も雇用変化へ影響する", evidence_ids, "PLAUSIBLE")
        ],
        "correlation_causation_risks": [
            {"risk_id": new_id("risk"), "description": "導入時期と雇用変化の同時発生だけでは因果を確定できない"}
        ],
        "necessary_conditions": [{"condition": "業務へ実装されること", "evidence_ids": evidence_ids}],
        "sufficient_conditions": [],
        "evidence_mappings": [{"item_id": "causal_model", "evidence_ids": evidence_ids}],
        "uncertainties": ["中長期の補完効果の大きさ"],
    }


def _causal_item(prefix: str, description: str, evidence_ids: list[str], status: str) -> dict:
    return {
        "item_id": new_id(prefix),
        "description": description,
        "evidence_ids": evidence_ids,
        "status": status,
    }


def stakeholder_analysis(input_data: dict) -> dict:
    evidence_ids = input_data["target_evidence_ids"][:2]
    worker_id = new_id("stakeholder")
    employer_id = new_id("stakeholder")
    response_id = new_id("response")
    return {
        "analysis_id": new_id("analysis_stakeholder"),
        "task_id": input_data["task_id"],
        "stakeholders": [
            {"stakeholder_id": worker_id, "name": "労働者", "role": "影響を受ける主体", "evidence_ids": evidence_ids},
            {"stakeholder_id": employer_id, "name": "雇用主", "role": "導入を決定する主体", "evidence_ids": evidence_ids},
        ],
        "interests": [
            {"item_id": new_id("interest"), "stakeholder_id": worker_id, "description": "雇用安定と技能移行", "evidence_ids": evidence_ids}
        ],
        "authority_and_capacity": [
            {"item_id": new_id("capacity"), "stakeholder_id": employer_id, "description": "導入範囲と再配置を決定できる", "evidence_ids": evidence_ids}
        ],
        "existing_responses": [
            {
                "response_id": response_id,
                "actor_stakeholder_ids": [employer_id],
                "description": "段階導入と再訓練",
                "implementation_status": "PARTIALLY_IMPLEMENTED",
                "evidence_ids": evidence_ids,
            }
        ],
        "response_effectiveness": [
            {"response_id": response_id, "assessment": "効果は対象範囲に依存する", "evidence_ids": evidence_ids}
        ],
        "incentives": [{"stakeholder_id": employer_id, "description": "生産性向上"}],
        "implementation_barriers": [{"description": "再訓練時間と費用"}],
        "distributional_effects": [{"description": "職種により利益と負担が偏る"}],
        "evidence_mappings": [{"item_id": response_id, "evidence_ids": evidence_ids}],
        "uncertainties": ["小規模企業での実施能力"],
    }


def initial_integration(input_data: dict) -> dict:
    analyses = input_data["primary_analyses"]
    report = input_data["research_report"]
    argument = analyses.get("deliberation.argument_analyst", {})
    claims = argument.get("central_claims") or [
        {
            "claim_id": new_id("claim_integrated"),
            "statement": "技術導入の影響は条件依存である",
            "evidence_ids": [report["evidence_items"][0]["evidence_id"]],
        }
    ]
    evidence_ids = list(
        dict.fromkeys(
            evidence_id
            for claim in claims
            for evidence_id in claim.get("evidence_ids", [])
        )
    ) or [report["evidence_items"][0]["evidence_id"]]
    viewpoint_id = new_id("viewpoint")
    return {
        "integration_id": new_id("integration_initial"),
        "problem_definition": {
            "topic": report["topic"],
            "general_opinion": report["general_opinion"],
            "definition": "生成AIによる業務代替と雇用全体の変化を区別して分析する",
        },
        "key_claims": claims,
        "causal_structure": {"summary": "導入→業務代替・補完→職務再構成", "structural_factors": ["再訓練制度"]},
        "stakeholder_structure": {"primary": ["労働者", "雇用主"], "distribution": "影響は不均等"},
        "existing_response_assessment": [{"response": "再訓練", "assessment": "適用範囲が限定的"}],
        "agreements": [{"agreement_id": new_id("agreement"), "summary": "影響は職種・導入条件に依存する"}],
        "conflicts": [{"conflict_id": new_id("conflict"), "summary": "雇用純減の規模は一致していない"}],
        "unresolved_items": [{"item_id": new_id("unresolved"), "summary": "長期純効果"}],
        "candidate_viewpoints": [
            _viewpoint(viewpoint_id, claims, evidence_ids, "条件依存の変化", "単純な全面代替ではなく職務再構成として捉える")
        ],
        "traceability_index": {claim.get("claim_id", "claim"): claim.get("evidence_ids", evidence_ids) for claim in claims},
        "limitations": list(report.get("research_limitations", [])),
    }


def counterargument_analysis(input_data: dict) -> dict:
    evidence_ids = input_data["evidence_ids"][:2]
    claim_ids = input_data["key_claim_ids"]
    counter_id = new_id("counterargument")
    return {
        "analysis_id": new_id("analysis_counterargument"),
        "task_id": input_data["task_id"],
        "steelman_arguments": [
            {
                "challenge_id": new_id("steelman"),
                "target_claim_ids": claim_ids,
                "argument": "代替速度が補完業務の創出を上回る可能性がある",
                "evidence_ids": evidence_ids,
                "strength": "PLAUSIBLE",
            }
        ],
        "counterarguments": [
            {
                "challenge_id": counter_id,
                "target_claim_ids": claim_ids,
                "argument": "集計された雇用者数だけでは業務内容の劣化や分配影響を捉えられない",
                "evidence_ids": evidence_ids,
                "strength": "STRONG",
            }
        ],
        "contrary_evidence": [{"evidence_ids": evidence_ids, "summary": "反対方向の観測も含まれる"}],
        "exception_conditions": [{"condition": "急速かつ広範な導入"}],
        "falsification_conditions": [{"condition": "長期にわたり職務・雇用の両方へ影響がないこと"}],
        "alternative_interpretations": [{"interpretation_id": new_id("interpretation"), "summary": "雇用消失より職務再編が中心"}],
        "overlooked_stakeholders": [{"stakeholder": "再訓練を提供できない小規模企業"}],
        "false_balance_risks": [{"risk": "証拠量が非対称な見解を同等扱いしない"}],
        "required_revisions": [
            {
                "revision_id": new_id("integration_revision"),
                "target_item_id": claim_ids[0],
                "required_change": "条件依存性と分配影響を明示する",
                "reason": "強い反論を最終Viewpointに残す必要がある",
                "source_counterargument_ids": [counter_id],
            }
        ],
        "remaining_uncertainties": ["導入速度と制度対応速度の比較"],
    }


def final_integration(input_data: dict) -> dict:
    initial = input_data["initial_integration"]
    counter = input_data["counterargument_analysis"]
    claims = initial["key_claims"]
    evidence_ids = list(
        dict.fromkeys(
            evidence_id
            for claim in claims
            for evidence_id in claim.get("evidence_ids", [])
        )
    ) or counter["counterarguments"][0]["evidence_ids"]
    viewpoint = _viewpoint(
        initial["candidate_viewpoints"][0]["viewpoint_id"],
        claims,
        evidence_ids,
        "条件依存の職務再編",
        "生成AIの影響を代替・補完・分配の三点から評価する",
    )
    viewpoint["counterarguments"] = [item["argument"] for item in counter["counterarguments"]]
    viewpoint["strongest_objections"] = [counter["counterarguments"][0]["argument"]]
    source_counter_ids = [counter["counterarguments"][0]["challenge_id"]]
    return {
        "integration_id": new_id("integration_final"),
        "previous_integration_id": initial["integration_id"],
        "problem_definition": initial["problem_definition"],
        "key_claims": claims,
        "causal_structure": initial["causal_structure"],
        "stakeholder_structure": initial["stakeholder_structure"],
        "existing_response_assessment": initial["existing_response_assessment"],
        "major_viewpoints": [viewpoint],
        "agreements": initial["agreements"],
        "disagreements": initial["conflicts"],
        "tradeoffs": [{"tradeoff_id": new_id("tradeoff"), "summary": "生産性向上と移行負担"}],
        "unresolved_questions": initial["unresolved_items"],
        "uncertainties": ["長期雇用純効果"],
        "limitations": initial["limitations"],
        "integration_changes": [
            {
                "change_id": new_id("change"),
                "target_item_id": counter["required_revisions"][0]["target_item_id"],
                "change_type": "QUALIFY",
                "before_summary": "条件依存性の記述が限定的",
                "after_summary": "導入速度、補完業務、分配影響を明示",
                "reason": counter["required_revisions"][0]["reason"],
                "source_counterargument_ids": source_counter_ids,
            }
        ],
        "traceability_index": initial["traceability_index"],
    }


def _viewpoint(viewpoint_id: str, claims: list[dict], evidence_ids: list[str], title: str, position: str) -> dict:
    return {
        "viewpoint_id": viewpoint_id,
        "title": title,
        "position": position,
        "supporting_claim_ids": [claim.get("claim_id", "claim") for claim in claims],
        "supporting_evidence_ids": evidence_ids,
        "counterarguments": [],
        "strongest_objections": [],
        "assumptions": ["導入条件は産業・職種で異なる"],
        "uncertainties": ["長期純効果"],
        "scope_conditions": ["Research Reportの対象範囲内"],
    }


def quality_review(input_data: dict, decision: str | None) -> dict:
    report = input_data["research_report"]
    validation = input_data["deterministic_validation"]
    failed_agents = input_data.get("failed_agent_ids", [])
    if decision is None:
        if not validation["passed"]:
            decision = "blocked"
        elif failed_agents or input_data.get("limitations"):
            decision = "approved_with_conditions"
        else:
            decision = "approved"
    analysis_ids = [
        payload["analysis_id"] for payload in input_data["primary_analyses"].values()
    ] + [
        input_data["counterargument_analysis"]["analysis_id"],
        input_data["initial_integration"]["integration_id"],
        input_data["final_integration"]["integration_id"],
    ]
    evidence_ids = [item["evidence_id"] for item in report["evidence_items"]]
    base = {
        "review_id": new_id("deliberation_review"),
        "conclusion_readiness": "READY",
        "findings": [],
        "blocking_finding_ids": [],
        "revision_scope": "none",
        "revision_targets": [],
        "upstream_revision_requests": [],
        "limitations_to_disclose": [],
        "reviewed_analysis_ids": analysis_ids,
        "reviewed_evidence_ids": evidence_ids,
    }
    if decision == "revision_required":
        finding_id = new_id("finding")
        return {
            **base,
            "status": "revision_required",
            "conclusion_readiness": "NOT_READY",
            "reason": "Argument AnalysisのEvidence mappingを再確認する必要がある",
            "findings": [
                {
                    "finding_id": finding_id,
                    "severity": "MAJOR",
                    "category": "traceability",
                    "issue": "主要ClaimのEvidence mappingが不十分",
                    "required_action": "ClaimとEvidenceの対応を再検証する",
                    "affected_agent_ids": ["deliberation.argument_analyst"],
                    "evidence_ids": evidence_ids[:1],
                }
            ],
            "revision_scope": "targeted",
            "revision_targets": ["deliberation.argument_analyst"],
        }
    if decision == "upstream_evidence_required":
        finding_id = new_id("finding")
        return {
            **base,
            "status": "revision_required",
            "conclusion_readiness": "NOT_READY",
            "reason": "因果主張を検証する一次Evidenceが不足している",
            "findings": [
                {
                    "finding_id": finding_id,
                    "severity": "MAJOR",
                    "category": "evidence_gap",
                    "issue": "因果メカニズムを検証する直接Evidenceが不足",
                    "required_action": "Researcherで追加の一次資料を収集する",
                    "affected_agent_ids": ["deliberation.causal_structural_analyst"],
                    "evidence_ids": [],
                }
            ],
            "revision_scope": "researcher_return",
            "upstream_revision_requests": [
                {
                    "revision_request_id": new_id("upstream_revision"),
                    "research_question_id": report["research_questions"][0]["research_question_id"],
                    "affected_claim_ids": [],
                    "missing_evidence_description": "因果メカニズムを直接検証する一次Evidence",
                    "preferred_source_categories": ["ACADEMIC", "GOVERNMENT"],
                    "required_scope": {"research_scope": report["research_scope"]},
                    "acceptance_conditions": ["source_idとevidence_idを持つ", "Research Questionへ追跡可能"],
                    "requesting_agent_id": "deliberation.quality_reviewer",
                    "source_finding_ids": [finding_id],
                }
            ],
        }
    if decision == "blocked":
        finding_id = new_id("finding")
        return {
            **base,
            "status": "blocked",
            "conclusion_readiness": "NOT_READY",
            "reason": "決定論的検証または必須Workflow条件を満たしていない",
            "findings": [
                {
                    "finding_id": finding_id,
                    "severity": "CRITICAL",
                    "category": "workflow_integrity",
                    "issue": "Conclusionへ渡せないBlocking issueがある",
                    "required_action": "入力またはWorkflowを修復する",
                    "affected_agent_ids": [],
                    "evidence_ids": [],
                }
            ],
            "blocking_finding_ids": [finding_id],
        }
    if decision == "approved_with_conditions":
        return {
            **base,
            "status": "approved_with_conditions",
            "conclusion_readiness": "READY_WITH_CONDITIONS",
            "reason": "開示済みの制約を保持すればConclusionへ進める",
            "limitations_to_disclose": input_data.get("limitations") or ["一部Evidenceの代表性に制約がある"],
        }
    return {
        **base,
        "status": "approved",
        "reason": "Schema、Traceability、Counterargument、責務境界を満たしている",
    }
