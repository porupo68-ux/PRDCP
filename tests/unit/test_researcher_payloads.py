import unittest

from pydantic import ValidationError

from common.structured_outputs import strict_output_schema
from researcher.schemas.research_result import ResearchResult
from researcher.schemas.research_task import ResearchTask
from researcher.schemas.review import (
    ResearchQualityReviewInput,
    ResearchQualityReviewOutput,
    ResearchReviewFinding,
)
from researcher.schemas.source import ResearchSource
from researcher.schemas.trace_ids import canonicalize_legacy_trace_ids
from tests.researcher_helpers import valid_source


class ResearcherPayloadTests(unittest.TestCase):
    required_metadata = {
        "EXPERT": {"expert_name", "field", "affiliation", "statement_context"},
        "ACADEMIC": {"doi", "peer_reviewed", "journal_name", "study_type"},
        "GOVERNMENT": {"organization", "country", "document_type"},
        "NEWS": {"media_name", "article_type"},
        "PUBLIC_OPINION": {
            "platform",
            "engagement_count",
            "sample_size",
            "representativeness_warning",
        },
        "POLITICIAN": {"politician_name", "party", "position", "statement_type"},
        "INDUSTRY": {"organization_name", "organization_type", "industry"},
    }

    def test_valid_source_is_accepted(self):
        source = ResearchSource.model_validate(valid_source())
        self.assertEqual(source.source_type, "ACADEMIC")

    def test_empty_url_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(url=""))

    def test_missing_source_id_is_rejected(self):
        data = valid_source()
        data.pop("source_id")
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(data)

    def test_missing_evidence_id_is_rejected(self):
        data = valid_source()
        data.pop("evidence_id")
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(data)

    def test_source_and_evidence_ids_require_canonical_namespaces(self):
        for field, legacy_value in (
            ("source_id", "src_legacy"),
            ("source_id", "source-legacy"),
            ("evidence_id", "ev_legacy"),
            ("evidence_id", "evidence-legacy"),
        ):
            with self.subTest(field=field, legacy_value=legacy_value):
                data = valid_source()
                data[field] = legacy_value
                with self.assertRaises(ValidationError):
                    ResearchSource.model_validate(data)

    def test_legacy_trace_ids_are_canonicalized_only_by_explicit_read_adapter(self):
        legacy = valid_source(source_id="src_legacy", evidence_id="ev_legacy")
        converted = canonicalize_legacy_trace_ids(legacy)

        source = ResearchSource.model_validate(converted)
        self.assertTrue(source.source_id.startswith("source_legacy_"))
        self.assertTrue(source.evidence_id.startswith("evidence_legacy_"))
        self.assertEqual(canonicalize_legacy_trace_ids(converted), converted)

    def test_invalid_source_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(source_type="BLOG"))

    def test_invalid_stance_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(stance="TRUE"))

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(retrieved_at="2026-08-01T12:00:00"))

    def test_question_ids_cannot_be_empty(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(research_question_ids=[]))

    def test_category_metadata_is_required(self):
        for category, required_fields in self.required_metadata.items():
            with self.subTest(category=category):
                data = valid_source(category)
                data["source_specific_metadata"].pop(next(iter(required_fields)))
                with self.assertRaises(ValidationError):
                    ResearchSource.model_validate(data)

    def test_all_category_metadata_models_accept_valid_payloads(self):
        for category in self.required_metadata:
            with self.subTest(category=category):
                source = ResearchSource.model_validate(valid_source(category))
                self.assertEqual(source.source_type, category)
                self.assertIsInstance(source.source_specific_metadata, dict)

    def test_structured_output_schema_contains_category_requirements(self):
        schema = ResearchResult.model_json_schema()
        for category, required_fields in self.required_metadata.items():
            definition_name = "".join(part.title() for part in category.lower().split("_"))
            definition_name += "Metadata"
            with self.subTest(category=category):
                definition = schema["$defs"][definition_name]
                self.assertTrue(required_fields <= set(definition["required"]))
                self.assertFalse(definition["additionalProperties"])

    def test_structured_output_schema_correlates_source_type_and_metadata(self):
        schema = ResearchResult.model_json_schema()
        source_schema = schema["$defs"]["ResearchSource"]
        branches = {
            branch["properties"]["source_type"]["const"]: branch
            for branch in source_schema["anyOf"]
        }

        self.assertEqual(set(branches), set(self.required_metadata))
        for category in self.required_metadata:
            definition_name = "".join(part.title() for part in category.lower().split("_"))
            definition_name += "Metadata"
            with self.subTest(category=category):
                metadata_schema = branches[category]["properties"][
                    "source_specific_metadata"
                ]
                self.assertEqual(metadata_schema, {"$ref": f"#/$defs/{definition_name}"})
                self.assertFalse(branches[category]["additionalProperties"])

    def test_complete_result_requires_source(self):
        with self.assertRaises(ValidationError):
            ResearchResult.model_validate(
                {
                    "task_id": "task_1",
                    "research_question_id": "rq_employment",
                    "agent_id": "researcher.academic_researcher",
                    "sources": [],
                    "search_summary": "done",
                    "coverage_status": "COMPLETE",
                    "limitations": [],
                }
            )

    def test_source_must_trace_to_result_question(self):
        with self.assertRaises(ValidationError):
            ResearchResult.model_validate(
                {
                    "task_id": "task_1",
                    "research_question_id": "rq_other",
                    "agent_id": "researcher.academic_researcher",
                    "sources": [valid_source()],
                    "search_summary": "done",
                    "coverage_status": "COMPLETE",
                    "limitations": [],
                }
            )

    def test_result_rejects_sources_outside_the_assigned_agent_category(self):
        with self.assertRaisesRegex(ValidationError, "may return only ACADEMIC"):
            ResearchResult.model_validate(
                {
                    "task_id": "task_1",
                    "research_question_id": "rq_employment",
                    "agent_id": "researcher.academic_researcher",
                    "sources": [valid_source("GOVERNMENT")],
                    "search_summary": "done",
                    "coverage_status": "COMPLETE",
                    "limitations": [],
                }
            )

    def test_source_rejects_placeholder_identity_metadata(self):
        source = valid_source("POLITICIAN")
        source["source_specific_metadata"]["politician_name"] = "null"

        with self.assertRaisesRegex(ValidationError, "blank or placeholder"):
            ResearchSource.model_validate(source)

    def test_strict_schema_is_specialized_to_the_research_task_category(self):
        schema = strict_output_schema(
            ResearchResult,
            input_data={
                "target_agent_id": "researcher.academic_researcher",
                "research_target": "ACADEMIC",
            },
        )

        self.assertEqual(
            schema["properties"]["agent_id"]["enum"],
            ["researcher.academic_researcher"],
        )
        source_schema = schema["$defs"]["ResearchSource"]
        self.assertNotIn("anyOf", source_schema)
        self.assertEqual(
            source_schema["properties"]["source_type"]["enum"],
            ["ACADEMIC"],
        )
        self.assertEqual(
            source_schema["properties"]["source_specific_metadata"],
            {"$ref": "#/$defs/AcademicMetadata"},
        )

    def test_task_target_must_match_agent(self):
        with self.assertRaises(ValidationError):
            ResearchTask.model_validate(
                {
                    "task_id": "task_1",
                    "research_question_id": "rq_1",
                    "target_agent_id": "researcher.news_researcher",
                    "research_target": "ACADEMIC",
                    "question": "question",
                    "scope": ["日本"],
                    "constraints": ["一次情報"],
                    "max_sources": 5,
                    "revision_context": None,
                }
            )

    def test_revision_review_requires_findings_and_targets(self):
        with self.assertRaises(ValidationError):
            ResearchQualityReviewOutput.model_validate(
                {
                    "status": "revision_required",
                    "reason": "missing",
                    "findings": [],
                    "revision_targets": [],
                    "approved_research_report": None,
                }
            )

    def test_quality_review_input_requires_logical_task_id(self):
        task_field = ResearchQualityReviewInput.model_fields["task_id"]
        self.assertTrue(task_field.is_required())

    def test_quality_finding_can_target_manager_without_routing_provider_revision(self):
        finding = ResearchReviewFinding.model_validate(
            {
                "finding_id": "finding_manager_contract",
                "severity": "MAJOR",
                "research_question_id": None,
                "target_agent_id": "researcher.manager",
                "issue": "The integrated report needs a traceability wording correction",
                "required_action": "Correct the integrated report without new research",
            }
        )
        self.assertEqual(finding.target_agent_id, "researcher.manager")

        with self.assertRaises(ValidationError):
            ResearchQualityReviewOutput.model_validate(
                {
                    "status": "revision_required",
                    "reason": "Manager-only work is not a provider revision target",
                    "findings": [finding.model_dump(mode="json")],
                    "revision_targets": ["researcher.manager"],
                    "approved_research_report": None,
                }
            )

    def test_quality_finding_schema_exposes_manager_as_an_allowed_target(self):
        schema = ResearchReviewFinding.model_json_schema()
        target_schema = schema["properties"]["target_agent_id"]
        target_ref = next(
            item["$ref"]
            for item in target_schema["anyOf"]
            if "$ref" in item
        )
        enum_name = target_ref.rsplit("/", 1)[-1]
        self.assertIn("researcher.manager", schema["$defs"][enum_name]["enum"])


if __name__ == "__main__":
    unittest.main()
