from __future__ import annotations

import hashlib
import json
from typing import Any

from common.ids import new_id
from playwright.schemas import (
    CitationManifest,
    CitationValidatedScript,
    DeterministicValidationResult,
    NarrativeBlueprint,
    ProductionContext,
    ScriptClaimType,
    ScriptDraft,
    ValidationFinding,
    ValidationSeverity,
    VisualPlan,
)


def canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_script_claim_ids(script_draft: ScriptDraft) -> list[str]:
    """Return the Script-owned claim set in stable first-use order."""

    return list(
        dict.fromkeys(
            claim_id
            for section in script_draft.sections
            for paragraph in section.paragraphs
            for claim_id in paragraph.claim_ids
        )
    )


class PlaywrightValidator:
    @staticmethod
    def assert_manifest_claim_contract(
        *,
        script_draft: ScriptDraft,
        citation_manifest: CitationManifest,
    ) -> None:
        script_claims = set(canonical_script_claim_ids(script_draft))
        manifest_claims = set(citation_manifest.supported_claim_ids)
        if script_claims != manifest_claims:
            raise ValueError(
                "Citation Manifest claim contract mismatch: "
                f"missing={sorted(script_claims - manifest_claims)}, "
                f"unexpected={sorted(manifest_claims - script_claims)}"
            )

    def validate(
        self,
        *,
        production_context: ProductionContext,
        narrative: NarrativeBlueprint,
        script_draft: ScriptDraft,
        validated_script: CitationValidatedScript,
        citation_manifest: CitationManifest,
        visual_plan: VisualPlan,
        final_conclusion: dict[str, Any],
        expected_final_conclusion_hash: str,
    ) -> DeterministicValidationResult:
        findings: list[ValidationFinding] = []

        def add(
            code: str,
            message: str,
            *,
            target: str | None = None,
            severity: ValidationSeverity = ValidationSeverity.ERROR,
            upstream: bool = False,
            details: dict[str, Any] | None = None,
        ) -> None:
            findings.append(
                ValidationFinding(
                    finding_id=new_id("pw_finding"),
                    code=code,
                    severity=severity,
                    message=message,
                    target_agent_id=target,
                    upstream_required=upstream,
                    details=details or {},
                )
            )

        claim_ids = set(production_context.must_include_claim_ids)
        evidence_ids = set(production_context.must_include_evidence_ids)
        source_ids = {
            str(item["source_id"])
            for item in production_context.source_manifest
            if isinstance(item, dict) and item.get("source_id")
        }
        manifest_evidence_ids = {
            str(item["evidence_id"])
            for item in production_context.source_manifest
            if isinstance(item, dict) and item.get("evidence_id")
        }

        if canonical_hash(final_conclusion) != expected_final_conclusion_hash:
            add(
                "FINAL_CONCLUSION_CHANGED",
                "Final Conclusion changed after Playwright handoff",
                target="playwright.manager",
                upstream=True,
            )
        if narrative.production_context_id != production_context.production_context_id:
            add("NARRATIVE_CONTEXT_MISMATCH", "Narrative references another Production Context", target="playwright.narrative_architect")
        if script_draft.narrative_blueprint_id != narrative.narrative_blueprint_id:
            add("SCRIPT_NARRATIVE_MISMATCH", "Script references another Narrative Blueprint", target="playwright.scriptwriter")
        if validated_script.source_script_draft_id != script_draft.script_draft_id:
            add("CITATION_SCRIPT_MISMATCH", "Citation artifact references another Script Draft", target="playwright.evidence_citation_editor")
        if citation_manifest.script_draft_id != script_draft.script_draft_id:
            add("MANIFEST_SCRIPT_MISMATCH", "Citation Manifest references another Script Draft", target="playwright.evidence_citation_editor")
        if validated_script.citation_manifest_id != citation_manifest.citation_manifest_id:
            add("CITATION_MANIFEST_MISMATCH", "Validated Script references another Citation Manifest", target="playwright.evidence_citation_editor")
        if visual_plan.citation_validated_script_id != validated_script.citation_validated_script_id:
            add("VISUAL_SCRIPT_MISMATCH", "Visual Plan references another validated script", target="playwright.visual_director")

        narrative_types = {item.section_type for item in narrative.sections}
        required_types = {"QUESTION", "EVIDENCE", "DECISION", "CONCLUSION"}
        if production_context.limitations_to_disclose:
            required_types.add("LIMITATION")
        missing_types = sorted(required_types - narrative_types)
        if missing_types:
            add(
                "NARRATIVE_REQUIRED_SECTION_MISSING",
                f"Required narrative sections are missing: {missing_types}",
                target="playwright.narrative_architect",
                details={"missing_section_types": missing_types},
            )
        narrative_claims = {value for section in narrative.sections for value in section.claim_ids}
        narrative_evidence = {value for section in narrative.sections for value in section.evidence_ids}
        if claim_ids - narrative_claims:
            add(
                "NARRATIVE_CLAIM_COVERAGE",
                "Narrative does not place all mandatory claims",
                target="playwright.narrative_architect",
                details={"missing_claim_ids": sorted(claim_ids - narrative_claims)},
            )
        if evidence_ids - narrative_evidence:
            add(
                "NARRATIVE_EVIDENCE_COVERAGE",
                "Narrative does not place all mandatory evidence",
                target="playwright.narrative_architect",
                details={"missing_evidence_ids": sorted(evidence_ids - narrative_evidence)},
            )
        duration_delta = abs(sum(item.target_duration_seconds for item in narrative.sections) - narrative.estimated_duration_seconds)
        if duration_delta > max(30, int(narrative.estimated_duration_seconds * 0.2)):
            add(
                "NARRATIVE_DURATION_MISMATCH",
                "Narrative section durations do not match the estimated total",
                target="playwright.narrative_architect",
                severity=ValidationSeverity.WARNING,
            )

        narrative_section_ids = {item.section_id for item in narrative.sections}
        script_section_ids = {item.section_id for item in script_draft.sections}
        if script_section_ids != narrative_section_ids:
            add(
                "SCRIPT_SECTION_MISMATCH",
                "Script sections do not match Narrative Blueprint sections",
                target="playwright.scriptwriter",
                details={
                    "missing": sorted(narrative_section_ids - script_section_ids),
                    "unexpected": sorted(script_section_ids - narrative_section_ids),
                },
            )
        draft_paragraphs = {p.paragraph_id: p for section in script_draft.sections for p in section.paragraphs}
        validated_paragraphs = {p.paragraph_id: p for section in validated_script.sections for p in section.paragraphs}
        if set(validated_paragraphs) - set(draft_paragraphs):
            add("VALIDATED_SCRIPT_NEW_PARAGRAPH", "Citation editing introduced a new paragraph", target="playwright.evidence_citation_editor")
        if set(draft_paragraphs) - set(validated_paragraphs):
            add(
                "VALIDATED_SCRIPT_PARAGRAPH_MISSING",
                "Citation editing removed a canonical Script Draft paragraph",
                target="playwright.evidence_citation_editor",
                details={
                    "paragraph_ids": sorted(
                        set(draft_paragraphs) - set(validated_paragraphs)
                    )
                },
            )
        for paragraph_id in sorted(set(draft_paragraphs) & set(validated_paragraphs)):
            draft_paragraph = draft_paragraphs[paragraph_id]
            validated_paragraph = validated_paragraphs[paragraph_id]
            if (
                draft_paragraph.claim_ids != validated_paragraph.claim_ids
                or draft_paragraph.evidence_ids != validated_paragraph.evidence_ids
                or draft_paragraph.citation_required
                != validated_paragraph.citation_required
            ):
                add(
                    "VALIDATED_SCRIPT_TRACEABILITY_CHANGED",
                    "Citation editing changed Script-owned paragraph traceability",
                    target="playwright.evidence_citation_editor",
                    details={"paragraph_id": paragraph_id},
                )
        script_claim_ids = set(canonical_script_claim_ids(script_draft))
        manifest_claim_ids = set(citation_manifest.supported_claim_ids)
        if script_claim_ids != manifest_claim_ids:
            add(
                "CITATION_MANIFEST_CLAIM_SET_MISMATCH",
                "Citation Manifest supported claims do not match the canonical Script Draft",
                target="playwright.evidence_citation_editor",
                details={
                    "missing_claim_ids": sorted(script_claim_ids - manifest_claim_ids),
                    "unexpected_claim_ids": sorted(
                        manifest_claim_ids - script_claim_ids
                    ),
                },
            )
        actual_count = sum(len(p.speaker_text) for p in draft_paragraphs.values())
        if abs(actual_count - script_draft.estimated_character_count) > max(50, int(actual_count * 0.2)):
            add(
                "SCRIPT_CHARACTER_COUNT_MISMATCH",
                "estimated_character_count differs materially from the script",
                target="playwright.scriptwriter",
                severity=ValidationSeverity.WARNING,
            )

        mappings_by_paragraph: dict[str, list] = {}
        for mapping in citation_manifest.mappings:
            mappings_by_paragraph.setdefault(mapping.paragraph_id, []).append(mapping)
            if mapping.paragraph_id not in draft_paragraphs:
                add("CITATION_UNKNOWN_PARAGRAPH", f"Citation references unknown paragraph {mapping.paragraph_id}", target="playwright.evidence_citation_editor")
            unknown_claims = set(mapping.claim_ids) - claim_ids
            unknown_evidence = set(mapping.evidence_ids) - evidence_ids
            unknown_sources = set(mapping.source_ids) - source_ids
            if unknown_claims:
                add("CITATION_UNKNOWN_CLAIM", "Citation references unknown claim IDs", target="playwright.evidence_citation_editor", details={"ids": sorted(unknown_claims)})
            if unknown_evidence:
                add("CITATION_UNKNOWN_EVIDENCE", "Citation references unknown evidence IDs", target="playwright.evidence_citation_editor", details={"ids": sorted(unknown_evidence)})
            if unknown_sources:
                add("CITATION_UNKNOWN_SOURCE", "Citation references unknown source IDs", target="playwright.evidence_citation_editor", details={"ids": sorted(unknown_sources)})
            if mapping.claim_type == ScriptClaimType.UNSUPPORTED.value:
                add(
                    "UNSUPPORTED_CLAIM_REMAINS",
                    "Unsupported claim remains in the citation artifact",
                    target="playwright.scriptwriter",
                    details={
                        "citation_mapping_id": mapping.citation_mapping_id,
                        "paragraph_id": mapping.paragraph_id,
                        "claim_ids": mapping.claim_ids,
                        "evidence_ids": mapping.evidence_ids,
                    },
                )
        for paragraph in draft_paragraphs.values():
            if paragraph.citation_required and paragraph.paragraph_id not in mappings_by_paragraph:
                add(
                    "CITATION_MAPPING_MISSING",
                    f"Citation-required paragraph has no mapping: {paragraph.paragraph_id}",
                    target="playwright.evidence_citation_editor",
                    details={
                        "paragraph_id": paragraph.paragraph_id,
                        "claim_ids": paragraph.claim_ids,
                        "evidence_ids": paragraph.evidence_ids,
                    },
                )
            elif paragraph.citation_required:
                paragraph_mappings = mappings_by_paragraph[paragraph.paragraph_id]
                mapped_claim_ids = {
                    value
                    for mapping in paragraph_mappings
                    for value in mapping.claim_ids
                }
                mapped_evidence_ids = {
                    value
                    for mapping in paragraph_mappings
                    for value in mapping.evidence_ids
                }
                if (
                    mapped_claim_ids != set(paragraph.claim_ids)
                    or mapped_evidence_ids != set(paragraph.evidence_ids)
                ):
                    add(
                        "CITATION_MAPPING_TRACEABILITY_MISMATCH",
                        "Citation mapping does not reproduce Script-owned claim/evidence traceability",
                        target="playwright.evidence_citation_editor",
                        details={
                            "paragraph_id": paragraph.paragraph_id,
                            "script_claim_ids": paragraph.claim_ids,
                            "mapped_claim_ids": sorted(mapped_claim_ids),
                            "script_evidence_ids": paragraph.evidence_ids,
                            "mapped_evidence_ids": sorted(mapped_evidence_ids),
                        },
                    )
            if set(paragraph.claim_ids) - claim_ids:
                add("SCRIPT_UNKNOWN_CLAIM", "Script paragraph references an unknown claim", target="playwright.scriptwriter", details={"paragraph_id": paragraph.paragraph_id})
            if set(paragraph.evidence_ids) - evidence_ids:
                add("SCRIPT_UNKNOWN_EVIDENCE", "Script paragraph references unknown evidence", target="playwright.scriptwriter", details={"paragraph_id": paragraph.paragraph_id})
        if citation_manifest.unsupported_claims:
            add(
                "UNSUPPORTED_CLAIM_LIST_NOT_EMPTY",
                "Citation Manifest contains unsupported claims",
                target="playwright.scriptwriter",
                details={
                    "paragraph_ids": list(
                        dict.fromkeys(
                            item.paragraph_id
                            for item in citation_manifest.unsupported_claims
                        )
                    ),
                    "unsupported_claim_ids": list(
                        dict.fromkeys(
                            claim_id
                            for item in citation_manifest.unsupported_claims
                            for claim_id in item.claim_ids
                        )
                    ),
                },
            )
        if citation_manifest.missing_locators:
            add(
                "CITATION_LOCATOR_MISSING",
                "Citation Manifest contains missing locators",
                target="playwright.evidence_citation_editor",
                details={
                    "paragraph_ids": [
                        item.paragraph_id for item in citation_manifest.missing_locators
                    ]
                },
            )
        missing_limitations = set(production_context.limitations_to_disclose) - set(validated_script.limitations)
        if missing_limitations:
            add(
                "LIMITATION_DISCLOSURE_MISSING",
                "Required limitations are missing from the validated script",
                target="playwright.evidence_citation_editor",
                details={"limitations": sorted(missing_limitations)},
            )

        asset_ids = {item.asset_requirement_id for item in visual_plan.asset_requirements}
        for cue in visual_plan.visual_cues:
            paragraph = validated_paragraphs.get(cue.paragraph_id)
            if paragraph is None:
                add("VISUAL_UNKNOWN_PARAGRAPH", f"Visual Cue references unknown paragraph {cue.paragraph_id}", target="playwright.visual_director")
                continue
            if cue.section_id not in {item.section_id for item in validated_script.sections}:
                add("VISUAL_UNKNOWN_SECTION", f"Visual Cue references unknown section {cue.section_id}", target="playwright.visual_director")
            if set(cue.evidence_ids) - set(paragraph.evidence_ids):
                add(
                    "VISUAL_FACT_ADDITION",
                    "Visual Cue introduces evidence not present in its paragraph",
                    target="playwright.visual_director",
                    details={
                        "visual_cue_id": cue.visual_cue_id,
                        "paragraph_id": cue.paragraph_id,
                        "unknown_evidence_ids": sorted(
                            set(cue.evidence_ids) - set(paragraph.evidence_ids)
                        ),
                    },
                )
            if set(cue.source_ids) - source_ids:
                add("VISUAL_UNKNOWN_SOURCE", "Visual Cue references an unknown source", target="playwright.visual_director")
            if set(cue.asset_requirement_ids) - asset_ids:
                add(
                    "VISUAL_UNKNOWN_ASSET",
                    "Visual Cue references an unknown asset requirement",
                    target="playwright.visual_director",
                    details={
                        "visual_cue_id": cue.visual_cue_id,
                        "paragraph_id": cue.paragraph_id,
                        "unknown_asset_requirement_ids": sorted(
                            set(cue.asset_requirement_ids) - asset_ids
                        ),
                    },
                )
            if cue.factual_visual and not cue.citation_display_required:
                add("FACTUAL_VISUAL_WITHOUT_CITATION", "Factual visual must display a citation", target="playwright.visual_director")
        for chart in visual_plan.chart_requests:
            if chart.paragraph_id not in validated_paragraphs:
                add("CHART_UNKNOWN_PARAGRAPH", "Chart references an unknown paragraph", target="playwright.visual_director")
            if not chart.data_source_ids:
                add("CHART_SOURCE_MISSING", "Chart Request has no data source", target="playwright.visual_director")
            elif set(chart.data_source_ids) - source_ids:
                add("CHART_UNKNOWN_SOURCE", "Chart Request references an unknown source", target="playwright.visual_director")
            if set(chart.evidence_ids) - (evidence_ids | manifest_evidence_ids):
                add("CHART_UNKNOWN_EVIDENCE", "Chart Request references unknown evidence", target="playwright.visual_director")

        error_count = sum(item.severity == ValidationSeverity.ERROR.value for item in findings)
        return DeterministicValidationResult(
            validation_id=new_id("pw_validation"),
            is_valid=error_count == 0,
            findings=findings,
            checked_counts={
                "narrative_sections": len(narrative.sections),
                "script_sections": len(script_draft.sections),
                "paragraphs": len(validated_paragraphs),
                "citation_mappings": len(citation_manifest.mappings),
                "script_claim_count": len(script_claim_ids),
                "manifest_claim_count": len(manifest_claim_ids),
                "unsupported_claim_count": len(
                    citation_manifest.unsupported_claims
                ),
                "citation_mapping_count": len(citation_manifest.mappings),
                "visual_cues": len(visual_plan.visual_cues),
                "chart_requests": len(visual_plan.chart_requests),
                "errors": error_count,
            },
        )
