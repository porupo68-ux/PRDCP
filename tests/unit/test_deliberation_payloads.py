import json
import re
import unittest

from pydantic import ValidationError

from common.structured_outputs import strict_output_schema
from deliberation.manager import DeliberationManager
from deliberation.schemas.analysis_task import CounterargumentTask, DeliberationAnalysisTask
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult
from deliberation.schemas.causal_structural_analysis import (
    CausalStructuralAnalysisResult,
    canonicalize_legacy_causal_item_ids,
)
from deliberation.schemas.counterargument_analysis import (
    AlternativeInterpretation,
    CounterargumentAnalysisResult,
    normalize_saved_counterargument_payload,
)
from deliberation.schemas.integrated_analysis import (
    FinalIntegratedAnalysis,
    InitialIntegratedAnalysis,
    TraceabilityEntry,
    integration_provenance_errors,
)
from deliberation.schemas.review import (
    DeliberationQualityReviewOutput,
    DeterministicValidationResult,
)
from deliberation.schemas.research_context import build_deliberation_research_context
from deliberation.schemas.stakeholder_response_analysis import (
    SpecificFact,
    StakeholderResponseAnalysisResult,
)
from deliberation.agents.stakeholder_response_analyst import StakeholderResponseAnalyst
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
    @staticmethod
    def _initial_provenance_input() -> dict:
        return {
            "primary_analyses": {
                "deliberation.argument_analyst": {
                    "analysis_id": "argument_analysis_fixture"
                },
                "deliberation.causal_structural_analyst": {
                    "analysis_id": "causal_analysis_fixture"
                },
                "deliberation.stakeholder_response_analyst": {
                    "analysis_id": "stakeholder_analysis_fixture"
                },
            }
        }

    def test_initial_integration_strict_schema_binds_role_owned_provenance(self):
        schema = strict_output_schema(
            InitialIntegratedAnalysis,
            input_data=self._initial_provenance_input(),
        )
        definitions = schema["$defs"]
        self.assertEqual(
            definitions["IntegratedClaim"]["properties"]["source_analysis_id"][
                "enum"
            ],
            ["argument_analysis_fixture"],
        )
        self.assertEqual(
            definitions["IntegratedCausalStructure"]["properties"][
                "source_analysis_id"
            ]["enum"],
            ["causal_analysis_fixture"],
        )
        self.assertEqual(
            definitions["IntegratedStakeholderStructure"]["properties"][
                "source_analysis_id"
            ]["enum"],
            ["stakeholder_analysis_fixture"],
        )
        self.assertEqual(
            set(
                definitions["ProblemDefinition"]["properties"][
                    "source_analysis_ids"
                ]["items"]["enum"]
            ),
            {
                "argument_analysis_fixture",
                "causal_analysis_fixture",
                "stakeholder_analysis_fixture",
            },
        )
        initial_causal_pattern = definitions["TraceabilityEntry"]["properties"][
            "causal_item_ids"
        ]["items"]["pattern"]
        self.assertIsNotNone(
            re.fullmatch(initial_causal_pattern, "alt_exp_primary_explanation")
        )
        self.assertIsNone(
            re.fullmatch(initial_causal_pattern, "alt_interp_counterargument_only")
        )

    def _production_like_initial_payload(self) -> tuple[dict, dict]:
        input_data = self._initial_provenance_input()
        input_data["research_report"] = build_deliberation_research_context(
            make_report()
        ).model_dump(mode="json")
        input_data["primary_analyses"]["deliberation.argument_analyst"][
            "central_claims"
        ] = [
            {
                "claim_id": "claim_fixture",
                "statement": "fixture claim",
                "claim_type": "DESCRIPTIVE",
                "importance": "HIGH",
                "evidence_ids": ["evidence_0"],
                "support_status": "SUPPORTED",
            }
        ]
        return deliberation_fixtures.initial_integration(input_data), input_data

    def test_initial_integration_fixture_uses_all_authoritative_analysis_ids(self):
        payload, input_data = self._production_like_initial_payload()
        result = InitialIntegratedAnalysis.model_validate(payload)

        self.assertEqual(
            result.key_claims[0].source_analysis_id,
            "argument_analysis_fixture",
        )
        self.assertEqual(
            result.causal_structure.source_analysis_id,
            "causal_analysis_fixture",
        )
        self.assertEqual(
            result.stakeholder_structure.source_analysis_id,
            "stakeholder_analysis_fixture",
        )
        self.assertEqual(
            set(result.problem_definition.source_analysis_ids),
            {
                "argument_analysis_fixture",
                "causal_analysis_fixture",
                "stakeholder_analysis_fixture",
            },
        )
        self.assertEqual(integration_provenance_errors(payload, input_data), [])

    def test_initial_integration_rejects_integration_and_mixed_role_provenance(self):
        payload, input_data = self._production_like_initial_payload()
        wrong_namespace = json.loads(json.dumps(payload))
        wrong_namespace["stakeholder_structure"]["source_analysis_id"] = (
            "integration_initial_wrong_owner"
        )
        with self.assertRaises(ValidationError):
            InitialIntegratedAnalysis.model_validate(wrong_namespace)

        mixed_role = json.loads(json.dumps(payload))
        mixed_role["stakeholder_structure"]["source_analysis_id"] = (
            "causal_analysis_fixture"
        )
        InitialIntegratedAnalysis.model_validate(mixed_role)
        errors = integration_provenance_errors(mixed_role, input_data)
        self.assertEqual(
            errors[0]["loc"],
            ("stakeholder_structure", "source_analysis_id"),
        )

    def test_deliberation_output_identity_prefixes_are_visible_in_strict_schemas(self):
        expected = (
            (ArgumentAnalysisResult, "analysis_id", "^argument_analysis_.+"),
            (CausalStructuralAnalysisResult, "analysis_id", "^causal_analysis_.+"),
            (
                StakeholderResponseAnalysisResult,
                "analysis_id",
                "^stakeholder_analysis_.+",
            ),
            (
                CounterargumentAnalysisResult,
                "analysis_id",
                "^counterargument_analysis_.+",
            ),
            (InitialIntegratedAnalysis, "integration_id", "^integration_initial_.+"),
            (FinalIntegratedAnalysis, "integration_id", "^integration_final_.+"),
        )
        for model, field_name, pattern in expected:
            with self.subTest(model=model.__name__, field=field_name):
                schema = strict_output_schema(model)
                self.assertEqual(schema["properties"][field_name]["pattern"], pattern)

    def test_counterargument_strict_schema_exposes_prefix_and_revision_route_branches(self):
        schema = strict_output_schema(CounterargumentAnalysisResult)
        self.assertEqual(
            schema["properties"]["analysis_id"]["pattern"],
            "^counterargument_analysis_.+",
        )
        branches = schema["properties"]["counterarguments"]["items"]["anyOf"]
        definitions = schema["$defs"]
        branch_models = [
            definitions[branch["$ref"].rsplit("/", 1)[-1]] for branch in branches
        ]
        by_required = {
            model["properties"]["required_revision"]["const"]: model
            for model in branch_models
        }
        self.assertEqual(
            by_required[True]["properties"]["revision_target_agent_ids"]["minItems"],
            1,
        )
        self.assertEqual(
            by_required[False]["properties"]["revision_target_agent_ids"]["maxItems"],
            0,
        )
        target_schema = by_required[True]["properties"]["revision_target_agent_ids"]
        self.assertNotIn("deliberation.researcher", target_schema["items"]["enum"])

    def test_saved_counterargument_contract_errors_have_a_deterministic_read_adapter(self):
        payload = deliberation_fixtures.counterargument_analysis(
            {
                "task_id": "counter_task_revision_1_context_repair_1",
                "evidence_ids": ["evidence_0", "evidence_1"],
                "key_claim_ids": ["claim_1"],
            }
        )
        payload["analysis_id"] = "counteranalysis_saved_raw"
        payload["counterarguments"][0]["revision_target_agent_ids"] = [
            "deliberation.researcher",
            "deliberation.manager",
        ]
        nonrevision = json.loads(json.dumps(payload["counterarguments"][0]))
        nonrevision.update(
            {
                "counterargument_id": "counter_nonrevision_1",
                "required_revision": False,
                "revision_target_agent_ids": ["deliberation.researcher"],
            }
        )
        payload["counterarguments"].append(nonrevision)

        with self.assertRaises(ValidationError):
            CounterargumentAnalysisResult.model_validate(payload)
        normalized, audit = normalize_saved_counterargument_payload(payload)
        result = CounterargumentAnalysisResult.model_validate(normalized)

        self.assertEqual(result.analysis_id, "counterargument_analysis_saved_raw")
        self.assertEqual(
            result.counterarguments[0].revision_target_agent_ids,
            ["deliberation.manager"],
        )
        self.assertEqual(result.counterarguments[1].revision_target_agent_ids, [])
        self.assertIn(
            "counter_nonrevision_1",
            audit["removed_revision_target_agent_ids"],
        )

    def test_deliberation_research_context_preserves_traceability_without_table_duplication(self):
        report = make_report(evidence_count=12)
        context = build_deliberation_research_context(report)

        self.assertEqual(
            [item.evidence_id for item in context.evidence_items],
            [item.evidence_id for item in report.evidence_items],
        )
        self.assertEqual(
            [item.source_id for item in context.evidence_items],
            [item.source_id for item in report.evidence_items],
        )
        for compact, source in zip(context.evidence_items, report.sources, strict=True):
            self.assertEqual(compact.summary, source.summary)
            self.assertEqual(str(compact.url), str(source.url))
            self.assertEqual(compact.source_specific_metadata, source.source_specific_metadata)
        serialized = context.model_dump(mode="json")
        self.assertNotIn("sources", serialized)
        self.assertNotIn("source_metadata", serialized)
        self.assertNotIn("evidence_quality_assessments", serialized)
        self.assertLess(
            len(json.dumps(serialized, ensure_ascii=False)),
            len(json.dumps(report.model_dump(mode="json"), ensure_ascii=False)),
        )

    def test_counterargument_task_rejects_evidence_outside_compact_context(self):
        report = make_report()
        context = build_deliberation_research_context(report)
        initial = deliberation_fixtures.initial_integration(
            {
                "research_report": context.model_dump(mode="json"),
                "primary_analyses": {},
            }
        )
        with self.assertRaisesRegex(ValidationError, "unknown evidence IDs"):
            CounterargumentTask.model_validate(
                {
                    "task_id": "counter_task_revision_0",
                    "target_agent_id": "deliberation.counterargument_analyst",
                    "initial_integration_id": initial["integration_id"],
                    "key_claim_ids": [initial["key_claims"][0]["claim_id"]],
                    "candidate_viewpoint_ids": [
                        initial["candidate_viewpoints"][0]["viewpoint_id"]
                    ],
                    "evidence_ids": ["evidence_unknown"],
                    "initial_integration": initial,
                    "research_report": context.model_dump(mode="json"),
                    "revision_context": None,
                }
            )

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

    def test_primary_analysis_ids_require_a_nonempty_suffix(self):
        cases = (
            (ArgumentAnalysisResult, deliberation_fixtures.argument_analysis(valid_task()), "argument_analysis_"),
            (
                CausalStructuralAnalysisResult,
                deliberation_fixtures.causal_analysis(
                    valid_task(
                        analysis_type="CAUSAL_STRUCTURAL",
                        target_agent_id="deliberation.causal_structural_analyst",
                    )
                ),
                "causal_analysis_",
            ),
            (
                StakeholderResponseAnalysisResult,
                deliberation_fixtures.stakeholder_analysis(
                    valid_task(
                        analysis_type="STAKEHOLDER_RESPONSE",
                        target_agent_id="deliberation.stakeholder_response_analyst",
                    )
                ),
                "stakeholder_analysis_",
            ),
        )
        for model, payload, bare_prefix in cases:
            with self.subTest(model=model.__name__):
                payload["analysis_id"] = bare_prefix
                with self.assertRaises(ValidationError):
                    model.model_validate(payload)

    def test_argument_requires_mapping_for_every_claim(self):
        payload = deliberation_fixtures.argument_analysis(valid_task())
        payload["evidence_mappings"] = []
        with self.assertRaises(ValidationError):
            ArgumentAnalysisResult.model_validate(payload)

    def test_causal_item_namespaces_are_role_specific_and_legacy_read_is_consistent(self):
        task = valid_task(
            analysis_type="CAUSAL_STRUCTURAL",
            target_agent_id="deliberation.causal_structural_analyst",
        )
        payload = deliberation_fixtures.causal_analysis(task)
        payload["causal_claims"][0]["item_id"] = "cc_1"
        payload["mechanisms"][0]["item_id"] = "mech_1"
        payload["structural_factors"][0]["item_id"] = "sf_1"
        payload["alternative_explanations"][0]["item_id"] = "alt_1"
        payload["evidence_mappings"] = [
            {
                "evidence_id": "evidence_0",
                "mapped_item_ids": ["cc_1", "mech_1", "sf_1", "alt_1"],
            }
        ]
        with self.assertRaises(ValidationError):
            CausalStructuralAnalysisResult.model_validate(payload)

        normalized = canonicalize_legacy_causal_item_ids(payload)
        result = CausalStructuralAnalysisResult.model_validate(normalized)

        item_ids = [
            result.causal_claims[0].item_id,
            result.mechanisms[0].item_id,
            result.structural_factors[0].item_id,
            result.alternative_explanations[0].item_id,
        ]
        self.assertTrue(item_ids[0].startswith("causal_"))
        self.assertTrue(item_ids[1].startswith("mechanism_"))
        self.assertTrue(item_ids[2].startswith("structural_"))
        self.assertTrue(item_ids[3].startswith("alternative_"))
        self.assertEqual(result.evidence_mappings[0].mapped_item_ids, item_ids)

    def test_alt_exp_is_not_rewritten_by_legacy_checkpoint_adapter(self):
        task = valid_task(
            analysis_type="CAUSAL_STRUCTURAL",
            target_agent_id="deliberation.causal_structural_analyst",
        )
        payload = deliberation_fixtures.causal_analysis(task)
        payload["alternative_explanations"][0]["item_id"] = (
            "alt_exp_selection_bias_in_adoption"
        )
        payload["evidence_mappings"][0]["mapped_item_ids"] = [
            "alt_exp_selection_bias_in_adoption"
        ]

        normalized = canonicalize_legacy_causal_item_ids(payload)

        self.assertEqual(
            normalized["alternative_explanations"][0]["item_id"],
            "alt_exp_selection_bias_in_adoption",
        )
        self.assertEqual(
            normalized["evidence_mappings"][0]["mapped_item_ids"],
            ["alt_exp_selection_bias_in_adoption"],
        )

    def test_causal_item_ids_reject_bare_namespace_prefixes(self):
        task = valid_task(
            analysis_type="CAUSAL_STRUCTURAL",
            target_agent_id="deliberation.causal_structural_analyst",
        )
        payload = deliberation_fixtures.causal_analysis(task)
        payload["causal_claims"][0]["item_id"] = "causal_"
        with self.assertRaises(ValidationError):
            CausalStructuralAnalysisResult.model_validate(payload)

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

    def test_specific_fact_status_is_a_strict_structured_output_enum(self):
        schema = strict_output_schema(StakeholderResponseAnalysisResult)
        fact_schema = schema["$defs"]["SpecificFact"]
        self.assertEqual(
            set(fact_schema["properties"]["verification_status"]["enum"]),
            {"verified", "inferred", "unknown", "unverified"},
        )
        with self.assertRaises(ValidationError):
            SpecificFact.model_validate(
                {
                    "fact_id": "specific_localized",
                    "statement": "検証対象",
                    "verification_status": "確認済",
                    "evidence_ids": ["evidence_0"],
                    "source_ids": ["source_0"],
                    "research_gap": "",
                }
            )

    def test_stakeholder_strict_schema_binds_assigned_evidence_and_source_ids(self):
        report = make_report()
        task = next(
            item
            for item in DeliberationManager._create_analysis_tasks(report)
            if item.target_agent_id
            == "deliberation.stakeholder_response_analyst"
        )
        schema = strict_output_schema(
            StakeholderResponseAnalysisResult,
            input_data=task.model_dump(mode="json"),
        )
        specific_fact = schema["$defs"]["SpecificFact"]["properties"]
        self.assertEqual(
            set(schema["$defs"]["AssignedEvidenceId"]["enum"]),
            set(task.target_evidence_ids),
        )
        self.assertEqual(
            set(schema["$defs"]["AssignedSourceId"]["enum"]),
            {item.source_id for item in task.evidence_context},
        )
        self.assertEqual(
            schema["properties"]["task_id"]["enum"],
            [task.task_id],
        )
        self.assertEqual(
            specific_fact["evidence_ids"]["items"],
            {"$ref": "#/$defs/AssignedEvidenceId"},
        )
        self.assertEqual(
            specific_fact["source_ids"]["items"],
            {"$ref": "#/$defs/AssignedSourceId"},
        )

    def test_stakeholder_provider_output_hydrates_source_ids_from_evidence(self):
        raw = {
            "specific_facts": [
                {
                    "fact_id": "fact_1",
                    "statement": "verified fact",
                    "verification_status": "verified",
                    "evidence_ids": ["evidence_0"],
                    "source_ids": ["source_invented"],
                    "research_gap": "",
                }
            ]
        }
        normalized = StakeholderResponseAnalyst.normalize_provider_output(
            None,
            raw,
            provider_input={
                "evidence_context": [
                    {"evidence_id": "evidence_0", "source_id": "source_0"}
                ]
            },
        )
        self.assertEqual(
            normalized["specific_facts"][0]["source_ids"],
            ["source_0"],
        )

    def test_stakeholder_specifics_require_evidence_or_a_fact_record(self):
        task = valid_task(
            analysis_type="STAKEHOLDER_RESPONSE",
            target_agent_id="deliberation.stakeholder_response_analyst",
        )
        payload = deliberation_fixtures.stakeholder_analysis(task)
        payload["stakeholders"][0]["name"] = "経済産業省"
        self.assertEqual(
            StakeholderResponseAnalysisResult.model_validate(payload)
            .stakeholders[0]
            .name,
            "経済産業省",
        )
        payload["stakeholders"][0]["evidence_ids"] = []
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

    def test_traceability_keeps_challenges_separate_from_counterarguments(self):
        with self.assertRaisesRegex(ValidationError, "counterargument_ids"):
            TraceabilityEntry.model_validate(
                {
                    "counterargument_ids": ["challenge_a"],
                    "challenge_ids": [],
                }
            )
        entry = TraceabilityEntry.model_validate(
            {
                "counterargument_ids": ["counter_1"],
                "challenge_ids": ["challenge_a"],
            }
        )
        self.assertEqual(entry.counterargument_ids, ["counter_1"])
        self.assertEqual(entry.challenge_ids, ["challenge_a"])
        schema = TraceabilityEntry.model_json_schema()
        self.assertEqual(
            schema["properties"]["counterargument_ids"]["items"]["pattern"],
            r"^(?:counterargument_|counter_).+",
        )
        self.assertEqual(
            schema["properties"]["challenge_ids"]["items"]["pattern"],
            r"^(?:challenge_|steelman_).+",
        )

    def test_final_causal_traceability_accepts_canonical_alternative_namespaces(self):
        entry = TraceabilityEntry.model_validate(
            {
                "causal_item_ids": [
                    "causal_task_efficiency_enhancement",
                    "alt_exp_selection_bias_in_adoption",
                    "alt_interp_task_fragmentation_and_busyness",
                    "alt_interp_skill_ceiling_decay",
                ]
            }
        )

        self.assertEqual(
            entry.causal_item_ids[-2:],
            [
                "alt_interp_task_fragmentation_and_busyness",
                "alt_interp_skill_ceiling_decay",
            ],
        )
        self.assertEqual(
            AlternativeInterpretation.model_validate(
                {
                    "interpretation_id": "alt_interp_task_fragmentation_and_busyness",
                    "summary": "Counterargument-owned interpretation",
                }
            ).interpretation_id,
            "alt_interp_task_fragmentation_and_busyness",
        )

    def test_final_causal_traceability_rejects_unknown_and_mixed_namespaces(self):
        for values in (
            ["foo_interp_x"],
            ["invalid_causal_x"],
            ["causal_valid_x", "alt_interp_valid_x", "invalid_x"],
        ):
            with self.subTest(values=values), self.assertRaisesRegex(
                ValidationError, "causal_item_ids"
            ):
                TraceabilityEntry.model_validate({"causal_item_ids": values})

    def test_alt_interp_is_final_only_and_bound_to_counterargument_artifact(self):
        initial_payload, initial_input = self._production_like_initial_payload()
        initial = InitialIntegratedAnalysis.model_validate(initial_payload)
        counter = deliberation_fixtures.counterargument_analysis(
            {
                "task_id": "counter_task_cycle043",
                "key_claim_ids": [initial.key_claims[0].claim_id],
                "evidence_ids": list(initial.key_claims[0].evidence_ids),
            }
        )
        counter["alternative_interpretations"] = [
            {
                "interpretation_id": "alt_interp_task_fragmentation_and_busyness",
                "summary": "Task fragmentation interpretation",
            },
            {
                "interpretation_id": "alt_interp_skill_ceiling_decay",
                "summary": "Skill ceiling interpretation",
            },
        ]
        final_input = {
            "primary_analysis_ids": {
                agent_id: payload["analysis_id"]
                for agent_id, payload in initial_input["primary_analyses"].items()
            },
            "initial_integration": initial.model_dump(mode="json"),
            "counterargument_analysis": counter,
        }
        raw_final = deliberation_fixtures.final_integration(final_input)
        raw_final["causal_structure"]["alternative_explanations"].append(
            {
                "item_id": "alt_interp_skill_ceiling_decay",
                "description": "Skill ceiling interpretation",
                "evidence_linked": list(initial.key_claims[0].evidence_ids),
                "source_counterargument_ids": [
                    counter["counterarguments"][0]["counterargument_id"]
                ],
            }
        )
        raw_final["traceability_index"][0]["causal_item_ids"].append(
            "alt_interp_skill_ceiling_decay"
        )

        result = FinalIntegratedAnalysis.model_validate(
            json.loads(json.dumps(raw_final))
        )
        self.assertEqual(integration_provenance_errors(raw_final, final_input), [])
        self.assertEqual(
            {
                item
                for entry in result.traceability_index
                for item in entry.causal_item_ids
                if item.startswith("alt_interp_")
            },
            {
                "alt_interp_task_fragmentation_and_busyness",
                "alt_interp_skill_ceiling_decay",
            },
        )

        schema = strict_output_schema(FinalIntegratedAnalysis, input_data=final_input)
        pattern = schema["$defs"]["TraceabilityEntry"]["properties"][
            "causal_item_ids"
        ]["items"]["pattern"]
        self.assertIsNotNone(
            re.fullmatch(pattern, "alt_interp_task_fragmentation_and_busyness")
        )
        self.assertIsNotNone(re.fullmatch(pattern, "alt_interp_skill_ceiling_decay"))
        self.assertIsNone(re.fullmatch(pattern, "foo_interp_x"))

        initial_invalid = initial.model_dump(mode="json")
        initial_invalid["traceability_index"][0]["causal_item_ids"].append(
            "alt_interp_task_fragmentation_and_busyness"
        )
        with self.assertRaisesRegex(
            ValidationError, "initial integration cannot contain"
        ):
            InitialIntegratedAnalysis.model_validate(initial_invalid)

        unknown = json.loads(json.dumps(raw_final))
        unknown["causal_structure"]["alternative_explanations"][-1]["item_id"] = (
            "alt_interp_not_in_counterargument"
        )
        unknown["traceability_index"][0]["causal_item_ids"][-1] = (
            "alt_interp_not_in_counterargument"
        )
        FinalIntegratedAnalysis.model_validate(unknown)
        self.assertTrue(integration_provenance_errors(unknown, final_input))

    def test_final_disposition_resolution_is_a_provider_visible_closed_enum(self):
        schema = strict_output_schema(FinalIntegratedAnalysis)
        resolution = schema["$defs"]["CounterargumentDisposition"]["properties"][
            "resolution"
        ]
        self.assertEqual(
            set(resolution["enum"]),
            {"revised", "rejected", "unresolved", "researcher_return"},
        )
        with self.assertRaises(ValidationError):
            FinalIntegratedAnalysis.model_validate(
                {
                    "counterargument_dispositions": [
                        {"resolution": "revised_with_research_gap_retained"}
                    ]
                }
            )

    def test_counterargument_output_rejects_challenge_id_namespace(self):
        raw = deliberation_fixtures.counterargument_analysis(
            {
                "task_id": "counter_task_1",
                "key_claim_ids": ["claim_1"],
                "evidence_ids": ["evidence_0"],
            }
        )
        raw["counterarguments"][0]["counterargument_id"] = "challenge_wrong_bucket"
        with self.assertRaisesRegex(ValidationError, "counterargument_id"):
            CounterargumentAnalysisResult.model_validate(raw)

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
                    "challenge_1",
                    "counter_task_1",
                ],
            }
        )
        self.assertEqual(entry.evidence_ids, ["evidence_0"])
        self.assertEqual(entry.source_ids, ["source_0"])
        self.assertEqual(entry.analysis_ids, ["argument_analysis_1"])
        self.assertEqual(entry.counterargument_ids, ["counter_1"])
        self.assertEqual(entry.challenge_ids, ["challenge_1"])
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
        readiness = (
            "ready"
            if status in {"approved", "approved_with_conditions"}
            else "not_ready"
        )
        return {
            "review_id": "review_1",
            "status": status,
            "conclusion_readiness": readiness,
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
