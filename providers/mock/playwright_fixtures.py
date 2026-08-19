from __future__ import annotations

from copy import deepcopy

from common.ids import new_id


def narrative_blueprint(input_data: dict) -> dict:
    context = input_data["production_context"]
    claim_ids = list(context["must_include_claim_ids"])
    evidence_ids = list(context["must_include_evidence_ids"])
    limitations = list(context.get("limitations_to_disclose", []))
    types = ["HOOK", "QUESTION", "GENERAL_OPINION", "EVIDENCE", "ANALYSIS", "COUNTERPOINT", "DECISION"]
    if limitations:
        types.append("LIMITATION")
    types.append("CONCLUSION")
    base_duration = max(1, context["desired_duration_seconds"] // len(types))
    sections = []
    for index, section_type in enumerate(types, start=1):
        sections.append(
            {
                "section_id": f"section_{index:02d}",
                "sequence": index,
                "section_type": section_type,
                "purpose": f"{section_type}の役割を果たす",
                "key_message": _key_message(section_type, context),
                "target_duration_seconds": base_duration,
                "claim_ids": claim_ids if section_type in {"EVIDENCE", "ANALYSIS", "DECISION"} else [],
                "evidence_ids": evidence_ids if section_type in {"EVIDENCE", "ANALYSIS"} else [],
                "required_counterpoints": ["主要な反論を示す"] if section_type == "COUNTERPOINT" else [],
                "required_limitations": limitations if section_type == "LIMITATION" else [],
                "transition_goal": "次の論点へ接続する" if index < len(types) else None,
            }
        )
    total = sum(item["target_duration_seconds"] for item in sections)
    sections[-1]["target_duration_seconds"] += context["desired_duration_seconds"] - total
    return {
        "narrative_blueprint_id": new_id("narrative"),
        "production_context_id": context["production_context_id"],
        "narrative_strategy": "問いから証拠、反論、判断へ段階的に進む解説構成",
        "central_question": context["central_question"],
        "central_message": context["final_recommendation"],
        "estimated_duration_seconds": context["desired_duration_seconds"],
        "sections": sections,
        "must_include_claim_ids": claim_ids,
        "must_include_evidence_ids": evidence_ids,
        "uncertainty_placement": [{"section_id": sections[-2]["section_id"], "items": context.get("uncertainties", [])}],
        "limitation_placement": [{"section_id": item["section_id"], "items": limitations} for item in sections if item["section_type"] == "LIMITATION"],
        "emotional_arc": ["関心", "理解", "比較", "判断"],
        "pacing_notes": ["証拠部分を急がず説明する"],
        "prohibited_reframings": ["Final Conclusionを断定的に強化しない"],
    }


def script_draft(input_data: dict, *, unsupported: bool = False) -> dict:
    context = input_data["production_context"]
    blueprint = input_data["narrative_blueprint"]
    sections = []
    character_count = 0
    for section in blueprint["sections"]:
        claim_ids = list(section.get("claim_ids", []))
        evidence_ids = list(section.get("evidence_ids", []))
        text = _speaker_text(section["section_type"], context)
        paragraphs = [
            {
                "paragraph_id": f"paragraph_{section['sequence']:02d}_01",
                "sequence": 1,
                "speaker_text": text,
                "claim_ids": claim_ids,
                "evidence_ids": evidence_ids,
                "citation_required": bool(claim_ids or evidence_ids),
                "uncertainty_disclosed": section["section_type"] in {"COUNTERPOINT", "LIMITATION"},
                "limitation_disclosed": section["section_type"] == "LIMITATION",
                "rhetorical_function": section["section_type"].lower(),
            }
        ]
        if unsupported and section["section_type"] == "EVIDENCE":
            paragraphs.append(
                {
                    "paragraph_id": f"paragraph_{section['sequence']:02d}_02",
                    "sequence": 2,
                    "speaker_text": "ここでは上流証拠に存在しない具体的な数値を断定します。",
                    "claim_ids": [],
                    "evidence_ids": [],
                    "citation_required": True,
                    "uncertainty_disclosed": False,
                    "limitation_disclosed": False,
                    "rhetorical_function": "unsupported_test_fixture",
                }
            )
        character_count += sum(len(item["speaker_text"]) for item in paragraphs)
        sections.append(
            {
                "section_id": section["section_id"],
                "sequence": section["sequence"],
                "section_type": section["section_type"],
                "heading": section["key_message"],
                "target_duration_seconds": section["target_duration_seconds"],
                "paragraphs": paragraphs,
                "transition_text": "続いて、次の論点を確認します。" if section["sequence"] < len(blueprint["sections"]) else None,
            }
        )
    return {
        "script_draft_id": new_id("script_draft"),
        "narrative_blueprint_id": blueprint["narrative_blueprint_id"],
        "title_candidates": [f"{context['topic']}を証拠から検証する", context["central_question"]],
        "thumbnail_text_candidates": ["一般論を検証", "証拠で考える"],
        "estimated_duration_seconds": blueprint["estimated_duration_seconds"],
        "estimated_character_count": character_count,
        "sections": sections,
        "disclosure_summary": list(context.get("limitations_to_disclose", [])),
        "unresolved_items": [],
    }


def citation_editing(input_data: dict, *, missing_mapping: bool = False) -> dict:
    context = input_data["production_context"]
    draft = deepcopy(input_data["script_draft"])
    evidence_to_source = {
        item.get("evidence_id"): item.get("source_id")
        for item in context["source_manifest"]
        if item.get("evidence_id") and item.get("source_id")
    }
    mappings = []
    unsupported = []
    revision_map = []
    citation_paragraphs = [p for section in draft["sections"] for p in section["paragraphs"] if p["citation_required"]]
    for index, paragraph in enumerate(citation_paragraphs):
        if paragraph["rhetorical_function"] == "unsupported_test_fixture":
            mappings.append(
                {
                    "citation_mapping_id": new_id("citation"),
                    "paragraph_id": paragraph["paragraph_id"],
                    "claim_text": paragraph["speaker_text"],
                    "claim_type": "UNSUPPORTED",
                    "claim_ids": [],
                    "evidence_ids": [],
                    "source_ids": [],
                    "citation_locator": None,
                    "support_status": "UNSUPPORTED",
                    "wording_risk": "HIGH",
                    "required_revision": "上流証拠にない数値表現を削除する",
                }
            )
            unsupported.append({"paragraph_id": paragraph["paragraph_id"], "reason": "No supporting evidence"})
            continue
        if missing_mapping and index == 0:
            continue
        source_ids = list(dict.fromkeys(evidence_to_source[value] for value in paragraph["evidence_ids"] if value in evidence_to_source))
        mappings.append(
            {
                "citation_mapping_id": new_id("citation"),
                "paragraph_id": paragraph["paragraph_id"],
                "claim_text": paragraph["speaker_text"],
                "claim_type": "SUPPORTED_FACT" if paragraph["evidence_ids"] else "INTERPRETATION",
                "claim_ids": paragraph["claim_ids"],
                "evidence_ids": paragraph["evidence_ids"],
                "source_ids": source_ids,
                "citation_locator": {"source_ids": source_ids} if source_ids else None,
                "support_status": "SUPPORTED" if source_ids else "NOT_VERIFIABLE",
                "wording_risk": "LOW",
                "required_revision": None,
            }
        )
    manifest_id = new_id("citation_manifest")
    return {
        "citation_validated_script": {
            "citation_validated_script_id": new_id("validated_script"),
            "source_script_draft_id": draft["script_draft_id"],
            "sections": draft["sections"],
            "paragraph_revision_map": revision_map,
            "citation_manifest_id": manifest_id,
            "unresolved_citation_issues": unsupported,
            "limitations": list(context.get("limitations_to_disclose", [])),
        },
        "citation_manifest": {
            "citation_manifest_id": manifest_id,
            "script_draft_id": draft["script_draft_id"],
            "mappings": mappings,
            "unsupported_claims": unsupported,
            "partially_supported_claims": [],
            "missing_locators": [],
            "source_list": context["source_manifest"],
            "disclosure_checks": [{"limitation": item, "preserved": True} for item in context.get("limitations_to_disclose", [])],
            "revision_summary": revision_map,
        },
    }


def visual_plan(input_data: dict, *, mismatch: bool = False, missing_chart_source: bool = False) -> dict:
    script = input_data["citation_validated_script"]
    manifest = input_data["citation_manifest"]
    source_by_paragraph = {item["paragraph_id"]: item.get("source_ids", []) for item in manifest["mappings"]}
    visual_cues = []
    for section in script["sections"]:
        for paragraph in section["paragraphs"]:
            factual = bool(paragraph["evidence_ids"])
            visual_cues.append(
                {
                    "visual_cue_id": new_id("visual_cue"),
                    "section_id": section["section_id"],
                    "paragraph_id": "missing_paragraph" if mismatch and not visual_cues else paragraph["paragraph_id"],
                    "visual_type": "TEXT_OVERLAY" if not factual else "QUOTE_CARD",
                    "description": "話者の要点を簡潔に画面表示する",
                    "target_duration_seconds": min(12, section["target_duration_seconds"]),
                    "on_screen_text": paragraph["speaker_text"][:36],
                    "evidence_ids": paragraph["evidence_ids"],
                    "source_ids": source_by_paragraph.get(paragraph["paragraph_id"], []),
                    "asset_requirement_ids": [],
                    "factual_visual": factual,
                    "citation_display_required": factual,
                }
            )
    evidence_paragraph = next((p for s in script["sections"] for p in s["paragraphs"] if p["evidence_ids"]), None)
    chart_requests = []
    if evidence_paragraph:
        source_ids = source_by_paragraph.get(evidence_paragraph["paragraph_id"], [])
        chart_requests.append(
            {
                "chart_request_id": new_id("chart"),
                "paragraph_id": evidence_paragraph["paragraph_id"],
                "chart_type": "bar",
                "title": "証拠の要点",
                "data_source_ids": [] if missing_chart_source else source_ids[:1],
                "evidence_ids": evidence_paragraph["evidence_ids"][:1],
                "x_axis": "項目",
                "y_axis": "値",
                "required_annotations": ["出典を表示する"],
                "prohibited_transformations": ["ゼロ起点を崩して差を誇張しない"],
            }
        )
    return {
        "visual_plan_id": new_id("visual_plan"),
        "citation_validated_script_id": script["citation_validated_script_id"],
        "visual_cues": visual_cues,
        "chart_requests": chart_requests,
        "asset_requirements": [],
        "source_display_rules": [{"rule": "事実表現にはsource_idを表示する"}],
        "visual_integrity_warnings": [],
    }


def _key_message(section_type: str, context: dict) -> str:
    return {
        "HOOK": "身近な問いとして提示する",
        "QUESTION": context["central_question"],
        "CONTEXT": "判断に必要な背景を整理する",
        "GENERAL_OPINION": "広く共有される見方を確認する",
        "EVIDENCE": "根拠を順番に確認する",
        "ANALYSIS": "根拠から言える範囲を整理する",
        "COUNTERPOINT": "反論と別の見方を確認する",
        "DECISION": context["final_recommendation"],
        "LIMITATION": "残る限界と不確実性を開示する",
        "CONCLUSION": "判断と提言をまとめる",
        "CTA": "視聴者が次に取れる行動を示す",
    }[section_type]


def _speaker_text(section_type: str, context: dict) -> str:
    return {
        "HOOK": f"今回扱うテーマは、{context['topic']}です。",
        "QUESTION": f"中心となる問いは、{context['central_question']}です。",
        "CONTEXT": "まず、この問いを判断するために必要な背景と前提を整理します。",
        "GENERAL_OPINION": "まず、広く共有されている見方が何を前提にしているのか確認します。",
        "EVIDENCE": "ここからは、上流で確認された証拠を使って事実関係を見ていきます。",
        "ANALYSIS": "証拠から直接言えることと、解釈として述べることを分けて考えます。",
        "COUNTERPOINT": "一方で、反対の証拠や別の説明も無視できません。",
        "DECISION": f"人間が選択した最終方向は、{context['final_recommendation']}です。",
        "LIMITATION": "この判断には、明示しておくべき不確実性と制限があります。",
        "CONCLUSION": "以上を踏まえ、判断の条件と今後確認すべき点をまとめます。",
        "CTA": "皆さんの経験や考えも確認し、次に検証すべき点を共有してください。",
    }[section_type]
