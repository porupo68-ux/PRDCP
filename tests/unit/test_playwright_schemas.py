import unittest

from pydantic import ValidationError

from common.structured_outputs import strict_output_schema, strict_schema_violations
from playwright.schemas import (
    CitationEditingResult,
    CitationManifest,
    NarrativeBlueprint,
    PlaywrightFinalGateResult,
    ProductionContext,
    ScriptDraft,
    UpstreamConclusionRevisionRequest,
    VisualPlan,
)
from playwright.validator import PlaywrightValidator
from providers.mock import playwright_fixtures


def production_context_data() -> dict:
    return {
        "production_context_id": "production_context_1",
        "workflow_id": "workflow_1",
        "final_conclusion_id": "final_1",
        "conclusion_package_id": "package_1",
        "human_selection_id": "selection_1",
        "topic": "検証対象",
        "central_question": "どの判断が妥当か",
        "selected_position": {"candidate_id": "position_1"},
        "final_recommendation": "段階的に進める",
        "target_audience": "一般視聴者",
        "video_objective": "証拠と限界を説明する",
        "desired_duration_seconds": 600,
        "must_include_claim_ids": ["claim_1"],
        "must_include_evidence_ids": ["evidence_1"],
        "source_manifest": [{"evidence_id": "evidence_1", "source_id": "source_1"}],
    }


def narrative_data() -> dict:
    section = {
        "section_id": "section_1",
        "sequence": 1,
        "section_type": "QUESTION",
        "purpose": "問いを示す",
        "key_message": "中心的な問い",
        "target_duration_seconds": 600,
        "claim_ids": ["claim_1"],
        "evidence_ids": ["evidence_1"],
    }
    return {
        "narrative_blueprint_id": "narrative_1",
        "production_context_id": "production_context_1",
        "narrative_strategy": "問いから結論へ進む",
        "central_question": "どの判断が妥当か",
        "central_message": "段階的に進める",
        "estimated_duration_seconds": 600,
        "sections": [section],
        "must_include_claim_ids": ["claim_1"],
        "must_include_evidence_ids": ["evidence_1"],
    }


def script_data() -> dict:
    return {
        "script_draft_id": "script_1",
        "narrative_blueprint_id": "narrative_1",
        "title_candidates": ["題名"],
        "thumbnail_text_candidates": ["要点"],
        "estimated_duration_seconds": 600,
        "estimated_character_count": 12,
        "sections": [
            {
                "section_id": "section_1",
                "sequence": 1,
                "section_type": "QUESTION",
                "heading": "問い",
                "target_duration_seconds": 600,
                "paragraphs": [
                    {
                        "paragraph_id": "paragraph_1",
                        "sequence": 1,
                        "speaker_text": "中心的な問いを確認します。",
                        "claim_ids": ["claim_1"],
                        "evidence_ids": ["evidence_1"],
                        "citation_required": True,
                        "rhetorical_function": "question",
                    }
                ],
            }
        ],
    }


class PlaywrightSchemaTests(unittest.TestCase):
    def test_production_context_rejects_duplicate_claim_ids(self):
        raw = production_context_data()
        raw["must_include_claim_ids"] = ["claim_1", "claim_1"]
        with self.assertRaises(ValidationError):
            ProductionContext.model_validate(raw)

    def test_production_context_enforces_duration_boundary(self):
        raw = production_context_data()
        raw["desired_duration_seconds"] = 59
        with self.assertRaises(ValidationError):
            ProductionContext.model_validate(raw)

    def test_narrative_rejects_duplicate_section_ids(self):
        raw = narrative_data()
        duplicate = dict(raw["sections"][0])
        duplicate["sequence"] = 2
        raw["sections"].append(duplicate)
        with self.assertRaises(ValidationError):
            NarrativeBlueprint.model_validate(raw)

    def test_narrative_requires_contiguous_sequence(self):
        raw = narrative_data()
        raw["sections"][0]["sequence"] = 2
        with self.assertRaises(ValidationError):
            NarrativeBlueprint.model_validate(raw)

    def test_script_rejects_duplicate_paragraph_ids(self):
        raw = script_data()
        second_section = dict(raw["sections"][0])
        second_section["section_id"] = "section_2"
        second_section["sequence"] = 2
        second_section["paragraphs"] = [dict(raw["sections"][0]["paragraphs"][0])]
        raw["sections"].append(second_section)
        with self.assertRaises(ValidationError):
            ScriptDraft.model_validate(raw)

    def test_citation_manifest_rejects_duplicate_mapping_ids(self):
        mapping = {
            "citation_mapping_id": "citation_1",
            "paragraph_id": "paragraph_1",
            "claim_text": "確認済みの事実",
            "claim_type": "SUPPORTED_FACT",
            "support_status": "SUPPORTED",
            "wording_risk": "LOW",
        }
        with self.assertRaises(ValidationError):
            CitationManifest.model_validate(
                {
                    "citation_manifest_id": "manifest_1",
                    "script_draft_id": "script_1",
                    "mappings": [mapping, dict(mapping)],
                }
            )

    def test_citation_manifest_rejects_duplicate_supported_claim_ids(self):
        raw = playwright_fixtures.citation_editing(
            {
                "production_context": production_context_data(),
                "script_draft": script_data(),
            }
        )["citation_manifest"]
        raw["supported_claim_ids"] = ["claim_1", "claim_1"]
        with self.assertRaisesRegex(ValidationError, "supported_claim_ids"):
            CitationManifest.model_validate(raw)

    def test_manifest_claim_contract_rejects_partial_known_id_set(self):
        script = ScriptDraft.model_validate(script_data())
        raw = playwright_fixtures.citation_editing(
            {
                "production_context": production_context_data(),
                "script_draft": script_data(),
            }
        )["citation_manifest"]
        raw["supported_claim_ids"] = []
        manifest = CitationManifest.model_validate(raw)
        with self.assertRaisesRegex(ValueError, "claim contract mismatch"):
            PlaywrightValidator.assert_manifest_claim_contract(
                script_draft=script,
                citation_manifest=manifest,
            )

    def test_visual_plan_rejects_duplicate_cue_ids(self):
        cue = {
            "visual_cue_id": "cue_1",
            "section_id": "section_1",
            "paragraph_id": "paragraph_1",
            "visual_type": "TEXT_OVERLAY",
            "description": "問いを表示する",
            "target_duration_seconds": 10,
        }
        with self.assertRaises(ValidationError):
            VisualPlan.model_validate(
                {
                    "visual_plan_id": "visual_1",
                    "citation_validated_script_id": "validated_1",
                    "visual_cues": [cue, dict(cue)],
                }
            )

    def test_visual_plan_rejects_unknown_asset_requirement_reference(self):
        with self.assertRaisesRegex(ValidationError, "unknown asset_requirement_ids"):
            VisualPlan.model_validate(
                {
                    "visual_plan_id": "visual_1",
                    "citation_validated_script_id": "validated_1",
                    "visual_cues": [
                        {
                            "visual_cue_id": "cue_1",
                            "section_id": "section_1",
                            "paragraph_id": "paragraph_1",
                            "visual_type": "TEXT_OVERLAY",
                            "description": "問いを表示する",
                            "target_duration_seconds": 10,
                            "asset_requirement_ids": ["asset_missing"],
                        }
                    ],
                }
            )

    def test_citation_result_requires_cross_artifact_identity(self):
        result = playwright_fixtures.citation_editing(
            {
                "production_context": production_context_data(),
                "script_draft": script_data(),
            }
        )
        result["citation_manifest"]["script_draft_id"] = "script_other"
        with self.assertRaisesRegex(ValidationError, "same Script Draft"):
            CitationEditingResult.model_validate(result)

    def test_citation_strict_schema_binds_each_mapping_to_its_paragraph(self):
        input_data = {
            "task_id": "playwright_citation_upstream_0_revision_1",
            "target_agent_id": "playwright.evidence_citation_editor",
            "production_context": production_context_data(),
            "script_draft": script_data(),
            "revision_context": None,
        }
        schema = strict_output_schema(CitationEditingResult, input_data=input_data)
        self.assertEqual([], strict_schema_violations(schema))
        mapping_array = self._property_node(schema, "mappings")
        branches = mapping_array["items"]["anyOf"]
        self.assertEqual(1, len(branches))
        properties = branches[0]["properties"]
        self.assertEqual(["paragraph_1"], properties["paragraph_id"]["enum"])
        self.assertEqual(
            ["evidence_1"],
            properties["evidence_ids"]["items"]["enum"],
        )
        self.assertEqual(
            ["source_1"],
            properties["source_ids"]["items"]["enum"],
        )
        supported = self._property_node(schema, "supported_claim_ids")
        self.assertEqual(1, supported["minItems"])
        self.assertEqual(1, supported["maxItems"])
        self.assertEqual(["claim_1"], supported["items"]["enum"])

    def test_long_citation_schema_keeps_global_ids_without_variant_explosion(self):
        context = production_context_data()
        draft = script_data()
        prototype = draft["sections"][0]["paragraphs"][0]
        draft["sections"][0]["paragraphs"] = [
            {
                **prototype,
                "paragraph_id": f"paragraph_{index}",
            }
            for index in range(16)
        ]
        input_data = {
            "task_id": "playwright_citation_long_script",
            "target_agent_id": "playwright.evidence_citation_editor",
            "production_context": context,
            "script_draft": draft,
            "revision_context": None,
        }

        schema = strict_output_schema(CitationEditingResult, input_data=input_data)
        self.assertEqual([], strict_schema_violations(schema))
        mapping_array = self._property_node(schema, "mappings")
        self.assertEqual(
            mapping_array["items"],
            {"$ref": "#/$defs/CitationMapping"},
        )
        mapping = schema["$defs"]["CitationMapping"]["properties"]
        self.assertEqual(
            [f"paragraph_{index}" for index in range(16)],
            mapping["paragraph_id"]["enum"],
        )
        self.assertEqual(
            ["evidence_1"],
            mapping["evidence_ids"]["items"]["enum"],
        )

    def test_visual_strict_schema_binds_cues_to_paragraph_local_references(self):
        citation = playwright_fixtures.citation_editing(
            {
                "production_context": production_context_data(),
                "script_draft": script_data(),
            }
        )
        input_data = {
            "task_id": "playwright_visual_upstream_0_revision_1",
            "target_agent_id": "playwright.visual_director",
            "production_context": production_context_data(),
            "citation_validated_script": citation["citation_validated_script"],
            "citation_manifest": citation["citation_manifest"],
            "revision_context": None,
        }
        schema = strict_output_schema(VisualPlan, input_data=input_data)
        self.assertEqual([], strict_schema_violations(schema))
        cue_array = self._property_node(schema, "visual_cues")
        branch = cue_array["items"]["anyOf"][0]
        properties = branch["properties"]
        self.assertEqual(["section_1"], properties["section_id"]["enum"])
        self.assertEqual(["paragraph_1"], properties["paragraph_id"]["enum"])
        self.assertEqual(
            ["evidence_1"],
            properties["evidence_ids"]["items"]["enum"],
        )
        self.assertEqual(
            ["source_1"],
            properties["source_ids"]["items"]["enum"],
        )

    def test_revision_gate_requires_target(self):
        with self.assertRaises(ValidationError):
            PlaywrightFinalGateResult.model_validate(
                {
                    "final_gate_result_id": "gate_1",
                    "status": "REVISION_REQUIRED",
                    "delivery_readiness": "needs revision",
                }
            )

    def test_upstream_request_requires_acceptance_conditions(self):
        with self.assertRaises(ValidationError):
            UpstreamConclusionRevisionRequest.model_validate(
                {
                    "revision_request_id": "request_1",
                    "final_conclusion_id": "final_1",
                    "issue_type": "TRACEABILITY_MISSING",
                    "issue_description": "claim IDが見つからない",
                    "required_resolution": "Traceabilityを修正する",
                    "acceptance_conditions": [],
                    "source_finding_ids": ["finding_1"],
                }
            )

    @staticmethod
    def _property_node(schema: dict, field: str) -> dict:
        found = []

        def walk(node):
            if isinstance(node, dict):
                properties = node.get("properties")
                if isinstance(properties, dict) and field in properties:
                    found.append(properties[field])
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(schema)
        if len(found) != 1:
            raise AssertionError(f"expected one {field} property, found {len(found)}")
        return found[0]


if __name__ == "__main__":
    unittest.main()
