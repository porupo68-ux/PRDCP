import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from common.models.pmp import MessageType, PMPMessage
from common.prompting import PRDCP_COMMON_RULES, PromptBuilder
from common.structured_outputs import strict_output_schema
from common.role_definitions import (
    RoleBoundaryViolationError,
    RoleDefinitionExtractor,
    RoleDefinitionLoader,
    RoleDefinitionNotFoundError,
    RoleDefinitionRegistry,
    RoleDefinitionSectionNotFoundError,
    RoleDefinitionValidationError,
    RoleDefinitionValidator,
)
from common.role_definitions.boundary import RoleBoundaryValidator
from config.settings import BASE_DIR
from producer.manager import ProducerManager
from producer.registry import ProducerRegistry
from providers.mock_provider import MockModelProvider
from storage.workflow_repository import WorkflowRepository


EXPECTED_AGENT_TIMEOUTS = {
    "producer.general_opinion_analyst": 900,
    "producer.manager": 600,
    "producer.quality_reviewer": 600,
    "producer.research_planner": 900,
    "producer.topic_scout": 900,
    "producer.topic_selector": 600,
    "researcher.academic_researcher": 3600,
    "researcher.expert_researcher": 3600,
    "researcher.government_researcher": 3600,
    "researcher.industry_researcher": 3600,
    "researcher.manager": 3600,
    "researcher.news_researcher": 3600,
    "researcher.politician_researcher": 3600,
    "researcher.public_opinion_researcher": 3600,
    "researcher.quality_reviewer": 3600,
    "deliberation.argument_analyst": 1800,
    "deliberation.causal_structural_analyst": 1800,
    "deliberation.counterargument_analyst": 1800,
    "deliberation.manager": 1800,
    "deliberation.quality_reviewer": 1800,
    "deliberation.stakeholder_response_analyst": 1800,
    "conclusion.decision_evaluator": 900,
    "conclusion.decision_integrator": 1200,
    "conclusion.manager": 600,
    "conclusion.position_generator": 1200,
    "conclusion.quality_reviewer": 600,
    "playwright.evidence_citation_editor": 1200,
    "playwright.manager": 600,
    "playwright.narrative_architect": 1200,
    "playwright.scriptwriter": 1800,
    "playwright.visual_director": 1200,
}


class CaptureMockProvider(MockModelProvider):
    def __init__(self):
        super().__init__()
        self.system_prompts: list[str] = []
        self.output_schemas: list[type] = []

    async def generate_structured(self, **kwargs):
        self.system_prompts.append(kwargs["system_prompt"])
        self.output_schemas.append(kwargs["output_schema"])
        return await super().generate_structured(**kwargs)


class RoleDefinitionLoaderTests(unittest.TestCase):
    def build_loader(self, **kwargs) -> RoleDefinitionLoader:
        return RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=None,
            **kwargs,
        )

    def temporary_rd_root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "role_definitions"
        shutil.copytree(BASE_DIR / "role_definitions", root)
        return root

    def test_strict_preload_loads_all_implemented_layers(self):
        loader = self.build_loader(preload=True, strict=True)
        self.assertEqual(len(loader.cache.agent_ids), 31)
        self.assertEqual(
            {agent.split(".", 1)[0] for agent in loader.cache.agent_ids},
            {"producer", "researcher", "deliberation", "conclusion", "playwright"},
        )

    def test_all_agent_timeouts_use_the_single_canonical_runtime_field(self):
        loader = self.build_loader(preload=True, strict=True)
        self.assertEqual(set(EXPECTED_AGENT_TIMEOUTS), loader.registry.agent_ids)
        for agent_id, expected_timeout in EXPECTED_AGENT_TIMEOUTS.items():
            with self.subTest(agent_id=agent_id):
                snapshot = loader.load(agent_id)
                body = snapshot.content.get("role_definition", snapshot.content)
                runtime = RoleDefinitionExtractor().extract_runtime_config(snapshot)
                self.assertEqual(runtime.timeout_seconds, expected_timeout)
                self.assertNotIn("timeout_seconds", body.get("configuration", {}))
                self.assertNotIn("timeout_seconds", body.get("execution_contract", {}))

    def test_validator_rejects_noncanonical_duplicate_timeout(self):
        source = BASE_DIR / "role_definitions" / "producer" / "topic_scout.json"
        content = json.loads(source.read_text(encoding="utf-8"))
        content["configuration"]["timeout_seconds"] = 600
        with self.assertRaisesRegex(RoleDefinitionValidationError, "exactly once"):
            RoleDefinitionValidator().validate(
                content,
                expected_agent_id="producer.topic_scout",
                source_path=source,
            )

    def test_validator_rejects_timeout_below_ten_minute_floor(self):
        source = BASE_DIR / "role_definitions" / "producer" / "topic_scout.json"
        content = json.loads(source.read_text(encoding="utf-8"))
        content["runtime_contract"]["timeout_seconds"] = 599
        with self.assertRaisesRegex(RoleDefinitionValidationError, "at least 600"):
            RoleDefinitionValidator().validate(
                content,
                expected_agent_id="producer.topic_scout",
                source_path=source,
            )

    def test_unknown_agent_is_rejected(self):
        loader = self.build_loader(preload=False)
        with self.assertRaises(RoleDefinitionNotFoundError):
            loader.load("producer.missing")

    def test_version_and_hash_are_stable(self):
        loader = self.build_loader(preload=False)
        first = loader.load("producer.topic_scout")
        loader.cache.clear()
        second = loader.load("producer.topic_scout")
        self.assertEqual(first.role_definition_version, "1.0.0")
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertTrue(first.content_hash.startswith("sha256:"))

    def test_cache_hit_is_observable(self):
        loader = self.build_loader(preload=False)
        loader.load("researcher.academic_researcher")
        loader.load("researcher.academic_researcher")
        self.assertEqual(loader.access_log.metrics["role_definition_cache_hit"], 1)

    def test_section_access_uses_fixed_snapshot(self):
        loader = self.build_loader(preload=False)
        snapshot = loader.load("deliberation.argument_analyst")
        section = loader.get_section(
            "deliberation.argument_analyst",
            "prohibited_actions",
            snapshot=snapshot,
        )
        self.assertTrue(section)

    def test_specialist_cannot_read_another_agents_rd(self):
        loader = self.build_loader(preload=False)
        with self.assertRaises(RoleDefinitionSectionNotFoundError):
            loader.get_section(
                "deliberation.causal_structural_analyst",
                "responsibilities",
                requester_agent_id="deliberation.argument_analyst",
            )

    def test_quality_reviewer_can_read_same_layer_boundaries(self):
        loader = self.build_loader(preload=False)
        sections = loader.get_sections(
            "producer.topic_scout",
            ["responsibilities", "prohibited_actions", "output_requirements"],
            requester_agent_id="producer.quality_reviewer",
        )
        self.assertEqual(set(sections), {"responsibilities", "prohibited_actions", "output_requirements"})

    def test_deliberation_quality_gate_uses_repairability_first_semantics(self):
        path = BASE_DIR / "role_definitions" / "deliberation" / "quality_reviewer.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        role = document["role_definition"]
        policy = role["quality_gate_policy"]
        self.assertEqual(
            policy["repairability_first"]["evaluation_order"],
            [
                "finding_detection",
                "repairability_assessment",
                "repair_layer_and_agent_identification",
                "severity_assessment",
                "routing_generation",
                "gate_decision",
            ],
        )
        self.assertEqual(policy["decision_precedence"][0], "revision_required_if_repairable")
        self.assertNotIn(
            "Researcher Evidenceへ追跡できない",
            policy["blocked"]["conditions"],
        )
        researcher_return = role["revision_policy"][
            "researcher_return_request"
        ]["routing_rule"]
        self.assertIn("status=revision_required", researcher_return)
        self.assertIn("revision_scope=researcher_return", researcher_return)
        self.assertIn("target_agent_idはresearcher.manager", researcher_return)

        evaluation = role["evaluation_and_testing"]["required_test_categories"]
        evidence_case = next(
            item for item in evaluation
            if item["test_category"] == "missing_research_evidence"
        )
        self.assertEqual(evidence_case["expected_result"], "revision_required")
        self.assertEqual(evidence_case["expected_revision_scope"], "researcher_return")
        self.assertEqual(evidence_case["minimum_upstream_revision_requests"], 1)
        snapshot = self.build_loader(preload=False).load(
            "deliberation.quality_reviewer"
        )
        context = RoleDefinitionExtractor().extract_llm_context(snapshot)
        self.assertTrue(
            any("repairability" in rule.lower() for rule in context.decision_rules)
        )
        self.assertTrue(
            any("status=revision_required" in rule for rule in context.revision_rules)
        )

    def test_deliberation_quality_prompt_distinguishes_blocking_from_blocked(self):
        prompt = (
            BASE_DIR / "deliberation" / "prompts" / "quality_reviewer.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Repairability First", prompt)
        self.assertIn(
            "A blocking finding does not automatically imply `status=blocked`",
            prompt,
        )
        self.assertIn("self retryは不要ですが、Researcher returnは必要", prompt)

    def test_validator_rejects_missing_responsibilities(self):
        source = BASE_DIR / "role_definitions" / "producer" / "topic_scout.json"
        content = json.loads(source.read_text(encoding="utf-8"))
        content["responsibilities"] = []
        with self.assertRaises(RoleDefinitionValidationError):
            RoleDefinitionValidator().validate(
                content,
                expected_agent_id="producer.topic_scout",
                source_path=source,
            )

    def test_validator_rejects_unknown_message_type(self):
        source = BASE_DIR / "role_definitions" / "producer" / "topic_scout.json"
        content = json.loads(source.read_text(encoding="utf-8"))
        content["runtime_contract"]["accepted_message_types"] = ["not_registered"]
        with self.assertRaises(RoleDefinitionValidationError):
            RoleDefinitionValidator().validate(
                content,
                expected_agent_id="producer.topic_scout",
                source_path=source,
            )

    def test_reload_uses_new_valid_version_on_next_run(self):
        root = self.temporary_rd_root()
        loader = RoleDefinitionLoader(
            RoleDefinitionRegistry(root),
            reload_on_change=True,
        )
        first = loader.load("producer.topic_scout")
        path = root / "producer" / "topic_scout.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        content["role_definition_version"] = "1.0.1"
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.utime(path, None)
        second = loader.load("producer.topic_scout")
        self.assertEqual(first.role_definition_version, "1.0.0")
        self.assertEqual(second.role_definition_version, "1.0.1")
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_invalid_reload_does_not_fall_back_to_old_cache(self):
        root = self.temporary_rd_root()
        loader = RoleDefinitionLoader(
            RoleDefinitionRegistry(root),
            reload_on_change=True,
        )
        loader.load("producer.topic_scout")
        path = root / "producer" / "topic_scout.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        content["responsibilities"] = []
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.utime(path, None)
        with self.assertRaises(RoleDefinitionValidationError):
            loader.load("producer.topic_scout")

    def test_runtime_and_llm_extraction_are_separate(self):
        loader = self.build_loader(preload=False)
        snapshot = loader.load("researcher.news_researcher")
        extractor = RoleDefinitionExtractor()
        role = extractor.extract_llm_context(snapshot)
        runtime = extractor.extract_runtime_config(snapshot)
        self.assertIn("research_revision_request", runtime.accepted_message_types)
        self.assertTrue(role.mission)
        self.assertFalse(hasattr(role, "timeout_seconds"))

    def test_prompt_builder_preserves_precedence_order(self):
        loader = self.build_loader(preload=False)
        role = RoleDefinitionExtractor().extract_llm_context(
            loader.load("producer.topic_selector")
        )
        prompt = PromptBuilder().build(
            common_rules=PRDCP_COMMON_RULES,
            role_context=role,
            agent_prompt="AGENT PROMPT",
            task_constraints={"x": True},
            output_schema={"type": "object"},
        )
        order = [
            prompt.index("PRDCP共通実行規則"),
            prompt.index("# Agent Identity"),
            prompt.index("# Mission"),
            prompt.index("# Responsibilities"),
            prompt.index("# Prohibited Actions"),
            prompt.index("# Task Constraints"),
            prompt.index("# Agent-specific Prompt"),
            prompt.index("# Output Schema"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('"runtime_contract"', prompt)

    def test_boundary_validator_rejects_prohibited_action(self):
        loader = self.build_loader(preload=False)
        snapshot = loader.load("deliberation.argument_analyst")
        runtime = RoleDefinitionExtractor().extract_runtime_config(snapshot)
        message = PMPMessage.create(
            sender_agent_id="deliberation.manager",
            receiver_agent_id="deliberation.argument_analyst",
            message_type=MessageType.DELIBERATION_TASK_ASSIGNMENT,
            objective="boundary test",
            payload={"requested_action": "solution_generation"},
        )
        with self.assertRaises(RoleBoundaryViolationError):
            RoleBoundaryValidator().validate(
                message=message,
                runtime_config=runtime,
                snapshot=snapshot,
                expected_output_message_type="deliberation_task_result",
            )

    def test_agent_prompt_and_pmp_contain_rd_trace(self):
        provider = CaptureMockProvider()
        rd_loader = RoleDefinitionLoader.from_project(BASE_DIR)
        with tempfile.TemporaryDirectory() as temporary:
            manager = ProducerManager(
                ProducerRegistry(
                    provider,
                    rd_loader=rd_loader,
                    demo_safe_mode=False,
                ),
                WorkflowRepository(Path(temporary)),
                demo_safe_mode=False,
            )
            state = asyncio.run(manager.start(user_topic="RD Loader test"))
        self.assertEqual(state.status, "COMPLETED")
        self.assertIn("# Mission", provider.system_prompts[0])
        self.assertIn("# Prohibited Actions", provider.system_prompts[0])
        for prompt, output_model in zip(
            provider.system_prompts,
            provider.output_schemas,
            strict=True,
        ):
            expected_schema = json.dumps(
                strict_output_schema(output_model),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            self.assertIn("# Output Schema\n" + expected_schema, prompt)
        result_messages = [m for m in state.message_history if m.sender_agent_id != "producer.manager"]
        trace = result_messages[0].metadata.extensions["role_definition"]
        self.assertEqual(trace["agent_id"], "producer.topic_scout")
        self.assertTrue(trace["role_definition_hash"].startswith("sha256:"))
        self.assertEqual(state.role_definition_usage[0]["agent_id"], "producer.manager")


if __name__ == "__main__":
    unittest.main()
