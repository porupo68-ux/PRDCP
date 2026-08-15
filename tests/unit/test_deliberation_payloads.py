import unittest

from pydantic import ValidationError

from deliberation.schemas.analysis_task import DeliberationAnalysisTask
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult
from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.integrated_analysis import (
    FinalIntegratedAnalysis,
    InitialIntegratedAnalysis,
    TraceabilityEntry,
)
from deliberation.schemas.review import (
    DeliberationQualityReviewOutput,
    DeterministicValidationResult,
)
from deliberation.schemas.stakeholder_response_analysis import (
    SpecificFact,
    StakeholderResponseAnalysisResult,
)
from deliberation.validator import DeliberationValidator
from providers.mock import deliberation_fixtures
from tests.deliberation_helpers import make_report


def valid_task(**overrides) -> dict:
    data = {
        "task_id": "task_1",
        "analysis_type": "ARGUMENT",
        "target_agent_id": "deliberation.argument_analyst",
        "research_report_id": "report_1",
        "research_question_ids": ["rq_1"],
        "target_evidence_ids": ["evidence_0", "evidence_1"],
        "problem_definition": "test problem",
        "shared_definitions": {},
        "geographic_scope": ["日本"],
        "time_scope": {"from": "2022"},
        "analysis_constraints": ["evidence only"],
        "completion_conditions": ["traceable"],
    }
    data.update(overrides)
    return data


class DeliberationPayloadTests(unittest.TestCase):
    def test_analysis_task_target_must_match_type(self):
        with self.assertRaises(ValidationError):
            DeliberationAnalysisTask.model_validate(
                valid_task(target_agent_id="deliberation.causal_structural_analyst")
            )

    def test_analysis_task_rejects_duplicate_evidence_ids(self):
        with self.assertRaises(ValidationError):
            DeliberationAnalysisTask.model_validate(
                valid_task(target_evidence_ids=["evidence_0", "evidence_0"])
            )

    def test_argument_claim_ids_must_be_unique(self):
        payload = deliberation_fixtures.argument_analysis(valid_task())
        payload["central_claims"].append(dict(payload["central_claims"][0]))
        with self.assertRaises(ValidationError):
            ArgumentAnalysisResult.model_validate(payload)

    def test_argument_requires_mapping_for_every_claim(self):
        payload = deliberation_fixtures.argument_analysis(valid_task())
        payload["evidence_mappings"] = []
        with self.assertRaises(ValidationError):
            ArgumentAnalysisResult.model_validate(payload)

    def test_stakeholder_ids_must_be_unique(self):
        task = valid_task(
            analysis_type="STAKEHOLDER_RESPONSE",
            target_agent_id="deliberation.stakeholder_response_analyst",
        )
        payload = deliberation_fixtures.stakeholder_analysis(task)
        payload["stakeholders"].append(dict(payload["stakeholders"][0]))
        with self.assertRaises(ValidationError):
            StakeholderResponseAnalysisResult.model_validate(payload)

    def test_verified_specific_fact_requires_evidence_and_source(self):
        with self.assertRaises(ValidationError):
            SpecificFact.model_validate(
                {
                    "fact_id": "specific_1",
                    "statement": "経済産業省が5,000人を支援した",
                    "verification_status": "verified",
                    "evidence_ids": [],
                    "source_ids": [],
                    "research_gap": "",
                }
            )
        fact = SpecificFact.model_validate(
            {
                "fact_id": "specific_2",
                "statement": "経済産業省が5,000人を支援した",
                "verification_status": "unverified",
                "evidence_ids": [],
                "source_ids": [],
                "research_gap": "支援人数を確認できる一次資料が必要",
            }
        )
        self.assertEqual(fact.verification_status, "unverified")

    def test_stakeholder_specifics_cannot_be_asserted_without_fact_record(self):
        task = valid_task(
            analysis_type="STAKEHOLDER_RESPONSE",
            target_agent_id="deliberation.stakeholder_response_analyst",
        )
        payload = deliberation_fixtures.stakeholder_analysis(task)
        payload["stakeholders"][0]["name"] = "経済産業省"
        with self.assertRaisesRegex(ValidationError, "SpecificFact"):
            StakeholderResponseAnalysisResult.model_validate(payload)
        payload["specific_facts"] = [
            {
                "fact_id": "specific_ministry",
                "statement": "経済産業省",
                "verification_status": "verified",
                "evidence_ids": ["evidence_0"],
                "source_ids": ["source_0"],
                "research_gap": "",
            }
        ]
        self.assertEqual(
            StakeholderResponseAnalysisResult.model_validate(payload)
            .specific_facts[0]
            .fact_id,
            "specific_ministry",
        )

    def test_new_traceability_rejects_mixed_id_types_in_evidence_ids(self):
        with self.assertRaisesRegex(ValidationError, "evidence_ids"):
            TraceabilityEntry.model_validate(
                {
                    "schema_version": "2.0",
                    "claim_ids": ["claim_1"],
                    "viewpoint_ids": [],
                    "causal_item_ids": [],
                    "integration_change_ids": [],
                    "evidence_ids": ["evidence_0", "source_0"],
                    "source_ids": ["source_0"],
                    "analysis_ids": ["argument_analysis_1"],
                    "counterargument_ids": [],
                    "integration_ids": [],
                    "task_ids": [],
                }
            )

    def test_legacy_traceability_is_split_by_identifier_type(self):
        entry = TraceabilityEntry.model_validate(
            {
                "schema_version": "1.0",
                "claim_id": "claim_1",
                "evidence_ids": [
                    "arg_analysis_1",
                    "evidence_0",
                    "source_0",
                    "counter_1",
                    "counter_task_1",
                ],
            }
        )
        self.assertEqual(entry.evidence_ids, ["evidence_0"])
        self.assertEqual(entry.source_ids, ["source_0"])
        self.assertEqual(entry.analysis_ids, ["argument_analysis_1"])
        self.assertEqual(entry.counterargument_ids, ["counter_1"])
        self.assertEqual(entry.task_ids, ["counter_task_1"])

    def test_integrated_analysis_normalizes_all_legacy_analysis_references(self):
        report = make_report()
        argument = deliberation_fixtures.argument_analysis(
            {
                "task_id": "delib_task_argument",
                "target_evidence_ids": ["evidence_0"],
                "geographic_scope": ["Japan"],
            }
        )
        causal = deliberation_fixtures.causal_analysis(
            {"task_id": "delib_task_causal", "target_evidence_ids": ["evidence_1"]}
        )
        stakeholder = deliberation_fixtures.stakeholder_analysis(
            {
                "task_id": "delib_task_stakeholder",
                "target_evidence_ids": ["evidence_2"],
            }
        )
        raw = deliberation_fixtures.initial_integration(
            {
                "research_report": report.model_dump(mode="json"),
                "primary_analyses": {
                    "deliberation.argument_analyst": argument,
                    "deliberation.causal_structural_analyst": causal,
                    "deliberation.stakeholder_response_analyst": stakeholder,
                },
            }
        )
        raw = InitialIntegratedAnalysis.model_validate(raw).model_dump(mode="json")
        raw["problem_definition"] = {
            "topic": "生成AIと雇用",
            "general_opinion_under_review": "生成AIは多くの仕事を奪う",
            "refined_problem_statement": "雇用への条件別影響を検証する",
            "scope": {"geographic": ["Japan"], "temporal": [], "domain": []},
            "key_dimensions": ["employment"],
            "source_analysis_ids": ["arg_analysis_legacy"],
            "revision_note": None,
        }
        raw["key_claims"][0]["source_analysis_id"] = "arg_analysis_legacy"
        raw["causal_structure"]["source_analysis_id"] = "analysis_causal_legacy"
        raw["stakeholder_structure"]["source_analysis_id"] = "analysis_task_legacy"
        raw["agreements"][0]["supporting_analysis_ids"] = ["arg_analysis_legacy"]
        raw["conflicts"][0]["involved_analysis_ids"] = ["analysis_task_legacy"]

        integrated = InitialIntegratedAnalysis.model_validate(raw)

        self.assertEqual(
            integrated.problem_definition.source_analysis_ids,
            ["argument_analysis_legacy"],
        )
        self.assertEqual(
            integrated.key_claims[0].source_analysis_id,
            "argument_analysis_legacy",
        )
        self.assertEqual(
            integrated.causal_structure.source_analysis_id,
            "causal_analysis_legacy",
        )
        self.assertEqual(
            integrated.stakeholder_structure.source_analysis_id,
            "stakeholder_analysis_legacy",
        )
        self.assertEqual(
            integrated.agreements[0].supporting_analysis_ids,
            ["argument_analysis_legacy"],
        )
        self.assertEqual(
            integrated.conflicts[0].involved_analysis_ids,
            ["stakeholder_analysis_legacy"],
        )
    def test_final_integration_rejects_four_viewpoints(self):
        report = make_report().model_dump(mode="json")
        argument = deliberation_fixtures.argument_analysis(valid_task())
        initial = deliberation_fixtures.initial_integration(
            {"research_report": report, "primary_analyses": {"deliberation.argument_analyst": argument}}
        )
        counter = deliberation_fixtures.counterargument_analysis(
            {
                "task_id": "counter_1",
                "key_claim_ids": [argument["central_claims"][0]["claim_id"]],
                "evidence_ids": ["evidence_0", "evidence_1"],
            }
        )
        final = deliberation_fixtures.final_integration(
            {"initial_integration": initial, "counterargument_analysis": counter}
        )
        final["major_viewpoints"] = [dict(final["major_viewpoints"][0]) for _ in range(4)]
        for index, viewpoint in enumerate(final["major_viewpoints"]):
            viewpoint["viewpoint_id"] = f"viewpoint_{index}"
        with self.assertRaises(ValidationError):
            FinalIntegratedAnalysis.model_validate(final)

    def test_approved_review_cannot_route_revision(self):
        data = self._valid_review()
        data["revision_targets"] = ["deliberation.argument_analyst"]
        with self.assertRaises(ValidationError):
            DeliberationQualityReviewOutput.model_validate(data)

    def test_researcher_return_requires_upstream_request(self):
        data = self._valid_review(status="revision_required")
        data.update(
            {
                "revision_scope": "researcher_return",
                "findings": [
                    {
                        "finding_id": "finding_1",
                        "severity": "MAJOR",
                        "category": "evidence_gap",
                        "issue": "missing evidence",
                        "required_action": "research",
                        "affected_agent_ids": [],
                        "evidence_ids": [],
                    }
                ],
            }
        )
        with self.assertRaises(ValidationError):
            DeliberationQualityReviewOutput.model_validate(data)

    def test_revision_required_without_upstream_or_internal_route_is_rejected(self):
        data = self._valid_review(status="revision_required")
        data.update(
            {
                "revision_scope": "targeted",
                "revision_targets": [],
                "upstream_revision_requests": [],
                "findings": [
                    {
                        "finding_id": "finding_unrouted",
                        "severity": "MAJOR",
                        "category": "workflow_integrity",
                        "issue": "revision has no route",
                        "required_action": "assign a revision route",
                        "affected_agent_ids": [],
                        "evidence_ids": [],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValidationError, "must route"):
            DeliberationQualityReviewOutput.model_validate(data)

    def test_researcher_manager_is_separated_from_internal_revision_targets(self):
        data = self._valid_review(status="revision_required")
        data.update(
            {
                "revision_scope": "researcher_return",
                "revision_targets": ["researcher.manager"],
                "findings": [
                    {
                        "finding_id": "finding_evidence_gap",
                        "severity": "MAJOR",
                        "category": "evidence_gap",
                        "issue": "missing evidence",
                        "required_action": "research",
                        "affected_agent_ids": ["deliberation.stakeholder_response_analyst"],
                        "evidence_ids": [],
                    }
                ],
                "upstream_revision_requests": [
                    {
                        "revision_request_id": "upstream_revision_1",
                        "research_question_id": "rq_1",
                        "affected_claim_ids": ["claim_1"],
                        "missing_evidence_description": "missing direct evidence",
                        "preferred_source_categories": ["GOVERNMENT"],
                        "required_scope": {"research_scope": ["Japan"]},
                        "acceptance_conditions": ["evidence_id and source_id are present"],
                        "requesting_agent_id": "deliberation.quality_reviewer",
                        "source_finding_ids": ["finding_evidence_gap"],
                    }
                ],
            }
        )
        review = DeliberationQualityReviewOutput.model_validate(data)
        self.assertEqual(review.revision_targets, [])
        self.assertEqual(
            review.upstream_revision_requests[0].target_agent_id,
            "researcher.manager",
        )

    def test_revision_target_schema_excludes_researcher_manager(self):
        schema = DeliberationQualityReviewOutput.model_json_schema()
        target_ref = schema["properties"]["revision_targets"]["items"]["$ref"]
        target_name = target_ref.rsplit("/", 1)[-1]
        self.assertNotIn("researcher.manager", schema["$defs"][target_name]["enum"])

    def test_blocked_review_requires_blocking_finding(self):
        data = self._valid_review(status="blocked")
        with self.assertRaises(ValidationError):
            DeliberationQualityReviewOutput.model_validate(data)

    def test_blocked_review_rejects_executable_internal_route(self):
        data = self._valid_review(status="blocked")
        data.update(
            {
                "findings": [
                    {
                        "finding_id": "finding_repairable",
                        "severity": "CRITICAL",
                        "category": "counterargument_quality",
                        "issue": "required counterargument revision was omitted",
                        "required_action": "rerun counterargument and reintegrate",
                        "affected_agent_ids": [
                            "deliberation.counterargument_analyst",
                            "deliberation.manager",
                        ],
                        "evidence_ids": ["evidence_0"],
                    }
                ],
                "blocking_finding_ids": ["finding_repairable"],
                "revision_scope": "targeted",
                "revision_targets": [
                    "deliberation.counterargument_analyst",
                    "deliberation.manager",
                ],
            }
        )
        with self.assertRaisesRegex(ValidationError, "repairable findings"):
            DeliberationQualityReviewOutput.model_validate(data)

    def test_blocked_review_rejects_executable_researcher_route(self):
        data = self._valid_review(status="blocked")
        data.update(
            {
                "findings": [
                    {
                        "finding_id": "finding_evidence_gap",
                        "severity": "CRITICAL",
                        "category": "evidence_gap",
                        "issue": "stakeholder fact lacks evidence",
                        "required_action": "collect evidence or remove the fact",
                        "affected_agent_ids": [
                            "deliberation.stakeholder_response_analyst"
                        ],
                        "evidence_ids": [],
                    }
                ],
                "blocking_finding_ids": ["finding_evidence_gap"],
                "revision_scope": "researcher_return",
                "upstream_revision_requests": [
                    {
                        "revision_request_id": "upstream_revision_1",
                        "target_agent_id": "researcher.manager",
                        "research_question_id": "rq_1",
                        "affected_claim_ids": ["claim_1"],
                        "missing_evidence_description": "stakeholder fact evidence",
                        "preferred_source_categories": ["GOVERNMENT"],
                        "required_scope": {"research_scope": ["Japan"]},
                        "acceptance_conditions": ["return evidence_id and source_id"],
                        "requesting_agent_id": "deliberation.quality_reviewer",
                        "source_finding_ids": ["finding_evidence_gap"],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValidationError, "repairable findings"):
            DeliberationQualityReviewOutput.model_validate(data)

    def test_blocking_finding_can_route_as_revision_required(self):
        data = self._valid_review(status="revision_required")
        data.update(
            {
                "conclusion_readiness": "NOT_READY",
                "findings": [
                    {
                        "finding_id": "finding_blocking_but_repairable",
                        "severity": "CRITICAL",
                        "category": "counterargument_quality",
                        "issue": "important counterargument was not routed",
                        "required_action": "rerun counterargument and Manager integration",
                        "affected_agent_ids": [
                            "deliberation.counterargument_analyst",
                            "deliberation.manager",
                        ],
                        "evidence_ids": ["evidence_0"],
                    }
                ],
                "blocking_finding_ids": ["finding_blocking_but_repairable"],
                "revision_scope": "targeted",
                "revision_targets": [
                    "deliberation.counterargument_analyst",
                    "deliberation.manager",
                ],
            }
        )
        review = DeliberationQualityReviewOutput.model_validate(data)
        self.assertEqual(review.status, "revision_required")
        self.assertEqual(
            review.revision_targets,
            [
                "deliberation.counterargument_analyst",
                "deliberation.manager",
            ],
        )

    def test_deterministic_validator_detects_unknown_evidence(self):
        report_model = make_report()
        report = report_model.model_dump(mode="json")
        argument = deliberation_fixtures.argument_analysis(valid_task())
        causal_task = valid_task(
            analysis_type="CAUSAL_STRUCTURAL",
            target_agent_id="deliberation.causal_structural_analyst",
        )
        causal = deliberation_fixtures.causal_analysis(causal_task)
        primary = {
            "deliberation.argument_analyst": argument,
            "deliberation.causal_structural_analyst": causal,
        }
        initial_raw = deliberation_fixtures.initial_integration(
            {"research_report": report, "primary_analyses": primary}
        )
        counter_raw = deliberation_fixtures.counterargument_analysis(
            {
                "task_id": "counter_1",
                "key_claim_ids": [argument["central_claims"][0]["claim_id"]],
                "evidence_ids": ["evidence_0", "evidence_1"],
            }
        )
        final_raw = deliberation_fixtures.final_integration(
            {"initial_integration": initial_raw, "counterargument_analysis": counter_raw}
        )
        final_raw["major_viewpoints"][0]["supporting_evidence_ids"].append("unknown_evidence")
        from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
        from deliberation.schemas.integrated_analysis import InitialIntegratedAnalysis

        validation = DeliberationValidator().validate(
            report=report_model,
            primary_analyses=primary,
            initial_integration=InitialIntegratedAnalysis.model_validate(initial_raw),
            counterargument=CounterargumentAnalysisResult.model_validate(counter_raw),
            final_integration=FinalIntegratedAnalysis.model_validate(final_raw),
            revision_count=0,
        )
        self.assertFalse(validation.passed)
        self.assertTrue(any(item.category == "traceability" for item in validation.findings))

    def test_validator_rejects_task_analysis_identifier_collision(self):
        report_model = make_report()
        report = report_model.model_dump(mode="json")
        argument = deliberation_fixtures.argument_analysis(valid_task())
        argument["task_id"] = argument["analysis_id"]
        primary = {"deliberation.argument_analyst": argument}
        causal_task = valid_task(
            task_id="delib_task_causal",
            analysis_type="CAUSAL_STRUCTURAL",
            target_agent_id="deliberation.causal_structural_analyst",
        )
        primary["deliberation.causal_structural_analyst"] = (
            deliberation_fixtures.causal_analysis(causal_task)
        )
        initial_raw = deliberation_fixtures.initial_integration(
            {"research_report": report, "primary_analyses": primary}
        )
        counter_raw = deliberation_fixtures.counterargument_analysis(
            {
                "task_id": "counter_task_1",
                "key_claim_ids": [argument["central_claims"][0]["claim_id"]],
                "evidence_ids": ["evidence_0", "evidence_1"],
            }
        )
        final_raw = deliberation_fixtures.final_integration(
            {
                "initial_integration": initial_raw,
                "counterargument_analysis": counter_raw,
            }
        )
        validation = DeliberationValidator().validate(
            report=report_model,
            primary_analyses=primary,
            initial_integration=InitialIntegratedAnalysis.model_validate(initial_raw),
            counterargument=CounterargumentAnalysisResult.model_validate(counter_raw),
            final_integration=FinalIntegratedAnalysis.model_validate(final_raw),
            revision_count=0,
        )
        self.assertFalse(validation.passed)
        self.assertTrue(
            any(
                item.category == "identifier" and "collide" in item.message
                for item in validation.findings
            )
        )

    def test_deterministic_metrics_cross_check_actual_targets(self):
        report_model = make_report()
        report = report_model.model_dump(mode="json")
        argument = deliberation_fixtures.argument_analysis(
            valid_task(task_id="delib_task_argument")
        )
        causal = deliberation_fixtures.causal_analysis(
            valid_task(
                task_id="delib_task_causal",
                analysis_type="CAUSAL_STRUCTURAL",
                target_agent_id="deliberation.causal_structural_analyst",
            )
        )
        primary = {
            "deliberation.argument_analyst": argument,
            "deliberation.causal_structural_analyst": causal,
        }
        initial_raw = deliberation_fixtures.initial_integration(
            {"research_report": report, "primary_analyses": primary}
        )
        counter_raw = deliberation_fixtures.counterargument_analysis(
            {
                "task_id": "counter_task_1",
                "key_claim_ids": [argument["central_claims"][0]["claim_id"]],
                "evidence_ids": ["evidence_0", "evidence_1"],
            }
        )
        final_raw = deliberation_fixtures.final_integration(
            {
                "initial_integration": initial_raw,
                "counterargument_analysis": counter_raw,
            }
        )
        validation = DeliberationValidator().validate(
            report=report_model,
            primary_analyses=primary,
            initial_integration=InitialIntegratedAnalysis.model_validate(initial_raw),
            counterargument=CounterargumentAnalysisResult.model_validate(counter_raw),
            final_integration=FinalIntegratedAnalysis.model_validate(final_raw),
            revision_count=0,
        )
        self.assertTrue(validation.passed)
        self.assertEqual(
            validation.metrics.claim_count,
            len(validation.validation_targets.claim_ids),
        )
        self.assertEqual(
            validation.metrics.integration_change_count,
            len(validation.validation_targets.integration_change_ids),
        )
        corrupted = validation.model_dump(mode="json")
        corrupted["metrics"]["claim_count"] += 1
        with self.assertRaisesRegex(ValidationError, "metrics"):
            DeterministicValidationResult.model_validate(corrupted)

    def test_blocking_counterargument_cannot_drop_from_revision_routing(self):
        raw = deliberation_fixtures.counterargument_analysis(
            {
                "task_id": "counter_task_1",
                "key_claim_ids": ["claim_1"],
                "evidence_ids": ["evidence_0"],
            }
        )
        second = dict(raw["counterarguments"][0])
        second["counterargument_id"] = "counterargument_2"
        raw["counterarguments"].append(second)
        result = CounterargumentAnalysisResult.model_validate(raw)
        self.assertEqual(
            result.unrouted_required_counterargument_ids(),
            ["counterargument_2"],
        )

    @staticmethod
    def _valid_review(status: str = "approved") -> dict:
        return {
            "review_id": "review_1",
            "status": status,
            "conclusion_readiness": "READY",
            "reason": "test",
            "findings": [],
            "blocking_finding_ids": [],
            "revision_scope": "none",
            "revision_targets": [],
            "upstream_revision_requests": [],
            "limitations_to_disclose": [],
            "reviewed_analysis_ids": ["analysis_1"],
            "reviewed_evidence_ids": ["evidence_0"],
        }


if __name__ == "__main__":
    unittest.main()
