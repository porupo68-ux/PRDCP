from __future__ import annotations

import unittest
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from common.structured_outputs import strict_output_schema, strict_schema_violations
from common.structured_outputs import (
    StrictStructuredOutputSchemaError,
    normalize_strict_output_schema,
    validate_strict_output_schema,
)
from conclusion.schemas.decision_evaluation import DecisionEvaluationResult
from conclusion.schemas.decision_integration import DecisionIntegrationResult
from conclusion.schemas.position_candidate import PositionGenerationResult
from conclusion.schemas.review import ConclusionQualityReviewOutput
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult
from deliberation.schemas.causal_structural_analysis import CausalStructuralAnalysisResult
from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.integrated_analysis import (
    FinalIntegratedAnalysis,
    InitialIntegratedAnalysis,
)
from deliberation.schemas.review import DeliberationQualityReviewOutput, RevisionScope
from deliberation.schemas.stakeholder_response_analysis import (
    StakeholderResponseAnalysisResult,
)
from playwright.schemas.citation_manifest import CitationEditingResult
from playwright.schemas.narrative_blueprint import NarrativeBlueprint
from playwright.schemas.script_draft import ScriptDraft
from playwright.schemas.visual_plan import VisualPlan
from producer.schemas.general_opinion import GeneralOpinionOutput
from producer.schemas.research_plan import ResearchPlanOutput
from producer.schemas.review import QualityReviewOutput
from producer.schemas.topic_scout import TopicScoutOutput
from producer.schemas.topic_selector import TopicSelectorOutput
from researcher.schemas.research_result import ResearchResult
from researcher.schemas.review import ResearchQualityReviewOutput


OPENROUTER_OUTPUT_SCHEMAS = (
    # Producer
    TopicScoutOutput,
    TopicSelectorOutput,
    GeneralOpinionOutput,
    ResearchPlanOutput,
    QualityReviewOutput,
    # Researcher
    ResearchResult,
    ResearchQualityReviewOutput,
    # Deliberation
    ArgumentAnalysisResult,
    CausalStructuralAnalysisResult,
    StakeholderResponseAnalysisResult,
    InitialIntegratedAnalysis,
    CounterargumentAnalysisResult,
    FinalIntegratedAnalysis,
    DeliberationQualityReviewOutput,
    # Conclusion
    PositionGenerationResult,
    DecisionEvaluationResult,
    DecisionIntegrationResult,
    ConclusionQualityReviewOutput,
    # Playwright
    NarrativeBlueprint,
    ScriptDraft,
    CitationEditingResult,
    VisualPlan,
)


class StrictTraversalLeaf(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    labels: list[str] = Field(default_factory=list)


class StrictTraversalAlternative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: int


class StrictTraversalRoot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nested: StrictTraversalLeaf
    array: list[StrictTraversalLeaf]
    union: StrictTraversalLeaf | StrictTraversalAlternative


class FreeAnyPayload(BaseModel):
    payload: dict[str, Any]


class FreeTypedMapPayload(BaseModel):
    payload: dict[str, int]


class ArbitraryObjectPayload(BaseModel):
    payload: object


class AnyArrayPayload(BaseModel):
    payload: list[Any]


class DefaultNamedPropertyPayload(BaseModel):
    default: str


class StructuredOutputSchemaTests(unittest.TestCase):
    def _assert_no_defaults_or_ref_siblings(self, node: Any, path: str = "$") -> None:
        if isinstance(node, dict):
            self.assertNotIn("default", node, path)
            if "$ref" in node:
                self.assertEqual(set(node), {"$ref"}, path)
            for key, value in node.items():
                self._assert_no_defaults_or_ref_siblings(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                self._assert_no_defaults_or_ref_siblings(value, f"{path}/{index}")

    @staticmethod
    def _schema_paths(node: Any, path: str = "$") -> list[str]:
        paths = [path]
        if isinstance(node, dict):
            for key, value in node.items():
                paths.extend(StructuredOutputSchemaTests._schema_paths(value, f"{path}/{key}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                paths.extend(
                    StructuredOutputSchemaTests._schema_paths(value, f"{path}/{index}")
                )
        return paths

    @staticmethod
    def _object_nodes(node: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
        found: list[tuple[str, dict[str, Any]]] = []
        if isinstance(node, dict):
            object_type = node.get("type")
            if (
                object_type == "object"
                or (isinstance(object_type, list) and "object" in object_type)
                or "properties" in node
                or "additionalProperties" in node
            ):
                found.append((path, node))
            for key, value in node.items():
                found.extend(StructuredOutputSchemaTests._object_nodes(value, f"{path}/{key}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found.extend(
                    StructuredOutputSchemaTests._object_nodes(value, f"{path}/{index}")
                )
        return found

    def test_all_openrouter_output_schemas_require_every_declared_property(self) -> None:
        self.assertEqual(len(OPENROUTER_OUTPUT_SCHEMAS), 22)
        for output_model in OPENROUTER_OUTPUT_SCHEMAS:
            with self.subTest(output_model=output_model.__name__):
                schema = strict_output_schema(output_model)
                self.assertEqual(strict_schema_violations(schema), [])
                self._assert_no_defaults_or_ref_siblings(schema)
                nodes = self._object_nodes(schema)
                self.assertTrue(nodes)
                for path, node in nodes:
                    self.assertIs(
                        node.get("additionalProperties"),
                        False,
                        f"{output_model.__name__} {path}",
                    )
                    properties = node.get("properties")
                    if isinstance(properties, dict):
                        self.assertEqual(
                            node.get("required"),
                            list(properties),
                            f"{output_model.__name__} {path}",
                        )

    def test_recursive_strictification_covers_defs_arrays_and_unions(self) -> None:
        schema = strict_output_schema(StrictTraversalRoot)
        paths = self._schema_paths(schema)

        self.assertTrue(any("/$defs/" in path for path in paths))
        self.assertTrue(any("/items" in path for path in paths))
        self.assertTrue(any("/anyOf/" in path for path in paths))
        self.assertEqual(strict_schema_violations(schema), [])

    def test_decision_evaluation_matrix_is_bounded_and_uses_plural_candidate_refs(self) -> None:
        schema = strict_output_schema(DecisionEvaluationResult)

        evaluations = schema["properties"]["candidate_evaluations"]
        self.assertEqual(evaluations["maxItems"], 70)
        self.assertEqual(schema["properties"]["comparison_matrix"]["maxItems"], 5)
        advantage = schema["$defs"]["ConditionalAdvantage"]
        sensitivity = schema["$defs"]["SensitivityResult"]
        self.assertIn("advantaged_candidate_ids", advantage["properties"])
        self.assertNotIn("advantaged_candidate_id", advantage["properties"])
        self.assertIn("preferred_candidate_ids", sensitivity["properties"])
        self.assertNotIn("preferred_candidate_id", sensitivity["properties"])

    def test_free_form_objects_are_rejected_instead_of_silently_closed(self) -> None:
        cases = (
            (FreeAnyPayload, {"payload": {"nested": {"unknown": [1, "two"]}}}),
            (FreeTypedMapPayload, {"payload": {"alpha": 1, "beta": 2}}),
            (ArbitraryObjectPayload, {"payload": {"unconstrained": True}}),
            (AnyArrayPayload, {"payload": [1, "two", {"three": True}]}),
        )
        for output_model, payload in cases:
            with self.subTest(output_model=output_model.__name__):
                raw_schema = output_model.model_json_schema()
                validated = output_model.model_validate(payload)
                self.assertEqual(validated.model_dump(), payload)
                with self.assertRaises(StrictStructuredOutputSchemaError) as raised:
                    strict_output_schema(output_model)
                self.assertEqual(raised.exception.schema_name, output_model.__name__)
                self.assertIn("path: $/properties/payload", str(raised.exception))
                self.assertEqual(output_model.model_json_schema(), raw_schema)

    def test_pydantic_defaults_remain_internal_but_are_removed_from_api_schema(self) -> None:
        raw_schema = DeliberationQualityReviewOutput.model_json_schema()
        raw_revision_scope = raw_schema["properties"]["revision_scope"]
        self.assertEqual(raw_revision_scope["default"], "none")
        self.assertEqual(
            DeliberationQualityReviewOutput.model_fields["revision_scope"].default,
            RevisionScope.NONE,
        )

        api_schema = strict_output_schema(DeliberationQualityReviewOutput)
        api_revision_scope = api_schema["properties"]["revision_scope"]
        self.assertEqual(api_revision_scope, {"$ref": "#/$defs/RevisionScope"})
        self._assert_no_defaults_or_ref_siblings(api_schema)

    def test_property_named_default_is_not_removed(self) -> None:
        api_schema = strict_output_schema(DefaultNamedPropertyPayload)
        self.assertIn("default", api_schema["properties"])
        self.assertEqual(api_schema["required"], ["default"])

    def test_validator_rejects_ref_siblings_with_a_schema_path(self) -> None:
        invalid = {
            "$defs": {"Value": {"type": "string"}},
            "type": "object",
            "properties": {
                "value": {
                    "$ref": "#/$defs/Value",
                    "minLength": 1,
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        }

        with self.assertRaises(StrictStructuredOutputSchemaError) as raised:
            validate_strict_output_schema(invalid, schema_name="InvalidRefSibling")

        self.assertEqual(raised.exception.schema_name, "InvalidRefSibling")
        self.assertIn("path: $/properties/value", str(raised.exception))
        self.assertIn("'$ref' cannot coexist", str(raised.exception))

    def test_validator_rejects_unresolved_local_refs(self) -> None:
        invalid = {
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/Missing"}},
            "required": ["value"],
            "additionalProperties": False,
        }

        with self.assertRaisesRegex(
            StrictStructuredOutputSchemaError,
            "unresolved local reference",
        ):
            validate_strict_output_schema(invalid, schema_name="UnresolvedRef")

    def test_normalizer_and_validator_are_separate_steps(self) -> None:
        raw = DeliberationQualityReviewOutput.model_json_schema()
        normalized = normalize_strict_output_schema(raw)
        validate_strict_output_schema(
            normalized,
            schema_name=DeliberationQualityReviewOutput.__name__,
        )

        self.assertIn("default", raw["properties"]["revision_scope"])
        self.assertNotIn("default", normalized["properties"]["revision_scope"])

    def test_dynamic_traceability_payload_is_preserved_as_explicit_entries(self) -> None:
        analysis = InitialIntegratedAnalysis.model_validate(
            {
                "integration_id": "integration_initial_1",
                "problem_definition": {
                    "topic": "topic",
                    "general_opinion": "general opinion",
                    "definition": "problem definition",
                },
                "key_claims": [
                    {
                        "claim_id": "claim_dynamic_17",
                        "statement": "claim",
                        "evidence_ids": ["evidence_4", "evidence_9"],
                    }
                ],
                "causal_structure": {"summary": "causal", "structural_factors": []},
                "stakeholder_structure": {"primary": [], "distribution": "mixed"},
                "candidate_viewpoints": [
                    {
                        "viewpoint_id": "viewpoint_1",
                        "title": "viewpoint",
                        "position": "position",
                        "supporting_claim_ids": ["claim_dynamic_17"],
                        "supporting_evidence_ids": ["evidence_4"],
                    }
                ],
                "traceability_index": {
                    "claim_dynamic_17": ["evidence_4", "evidence_9"]
                },
            }
        )

        self.assertEqual(
            analysis.model_dump(mode="json")["traceability_index"],
            [
                {
                    "schema_version": "1.0",
                    "claim_ids": ["claim_dynamic_17"],
                    "viewpoint_ids": [],
                    "causal_item_ids": [],
                    "integration_change_ids": [],
                    "evidence_ids": ["evidence_4", "evidence_9"],
                    "source_ids": [],
                    "analysis_ids": [],
                    "counterargument_ids": [],
                    "challenge_ids": [],
                    "integration_ids": [],
                    "task_ids": [],
                }
            ],
        )

    def test_deliberation_review_defaults_are_required_at_every_level(self) -> None:
        schema = strict_output_schema(DeliberationQualityReviewOutput)

        self.assertEqual(schema["required"], list(schema["properties"]))
        finding_schema = schema["$defs"]["QualityFinding"]
        self.assertEqual(finding_schema["required"], list(finding_schema["properties"]))
        self.assertIn("affected_agent_ids", finding_schema["required"])
        self.assertIn("evidence_ids", finding_schema["required"])

        for field_name in (
            "findings",
            "blocking_finding_ids",
            "revision_scope",
            "revision_targets",
            "upstream_revision_requests",
            "limitations_to_disclose",
        ):
            self.assertIn(field_name, schema["required"])

        readiness_ref = schema["properties"]["conclusion_readiness"]["$ref"]
        readiness_name = readiness_ref.rsplit("/", 1)[-1]
        self.assertEqual(
            schema["$defs"][readiness_name]["enum"],
            ["ready", "ready_with_conditions", "not_ready", "undetermined"],
        )

    def test_researcher_finding_type_is_required_and_bounded_for_human_gate(self) -> None:
        schema = strict_output_schema(ResearchQualityReviewOutput)
        finding_schema = schema["$defs"]["ResearchReviewFinding"]
        self.assertIn("finding_type", finding_schema["required"])
        finding_type = finding_schema["properties"]["finding_type"]
        enum_name = finding_type["$ref"].rsplit("/", 1)[-1]
        self.assertEqual(
            schema["$defs"][enum_name]["enum"],
            [
                "EVIDENCE_SUFFICIENCY_FINDING",
                "HARD_INTEGRITY_FAILURE",
                "UNCLASSIFIED",
            ],
        )

    def test_deliberation_required_scope_is_an_explicit_closed_model(self) -> None:
        schema = strict_output_schema(DeliberationQualityReviewOutput)
        request_schema = schema["$defs"]["UpstreamResearchRequest"]
        required_scope = request_schema["properties"]["required_scope"]
        self.assertEqual(required_scope["$ref"], "#/$defs/RequiredResearchScope")

        scope_schema = schema["$defs"]["RequiredResearchScope"]
        self.assertIs(scope_schema["additionalProperties"], False)
        self.assertEqual(scope_schema["required"], ["research_scope"])
        self.assertEqual(list(scope_schema["properties"]), ["research_scope"])

    def test_conclusion_and_playwright_nested_defaults_are_also_required(self) -> None:
        conclusion_schema = strict_output_schema(ConclusionQualityReviewOutput)
        conclusion_finding = conclusion_schema["$defs"]["ConclusionQualityFinding"]
        self.assertIn("affected_agent_ids", conclusion_finding["required"])
        self.assertIn("affected_candidate_ids", conclusion_finding["required"])
        readiness_ref = conclusion_schema["properties"]["playwright_readiness"]["$ref"]
        readiness_name = readiness_ref.rsplit("/", 1)[-1]
        self.assertEqual(
            conclusion_schema["$defs"][readiness_name]["enum"],
            ["ready", "ready_with_conditions", "not_ready", "not_applicable"],
        )

        playwright_schema = strict_output_schema(VisualPlan)
        visual_cue = playwright_schema["$defs"]["VisualCue"]
        self.assertIn("evidence_ids", visual_cue["required"])
        self.assertIn("source_ids", visual_cue["required"])


if __name__ == "__main__":
    unittest.main()
