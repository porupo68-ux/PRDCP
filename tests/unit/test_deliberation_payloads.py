import unittest

from pydantic import ValidationError

from deliberation.schemas.analysis_task import DeliberationAnalysisTask
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult
from deliberation.schemas.integrated_analysis import FinalIntegratedAnalysis
from deliberation.schemas.review import DeliberationQualityReviewOutput
from deliberation.schemas.stakeholder_response_analysis import StakeholderResponseAnalysisResult
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

    def test_blocked_review_requires_blocking_finding(self):
        data = self._valid_review(status="blocked")
        with self.assertRaises(ValidationError):
            DeliberationQualityReviewOutput.model_validate(data)

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
