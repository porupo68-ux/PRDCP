from __future__ import annotations

from copy import deepcopy
import unittest

from common.models.errors import ProviderRequestSchemaError
from common.provider_schema_compatibility import (
    gemini_schema_violations,
    validate_provider_schema_compatibility,
)
from common.structured_outputs import strict_output_schema
from producer.schemas.general_opinion import GeneralOpinionOutput
from producer.schemas.research_plan import ResearchTarget
from providers.openrouter_provider import OpenRouterModelProvider
from researcher.schemas.research_result import ResearchResult
from researcher.schemas.research_task import RESEARCH_TARGET_MAP


GEMINI_MODEL = "google/gemini-3.7-flash"


def adversarial_retrieval_sources() -> list[dict]:
    return [
        {
            "source_id": f"source_{index:024d}",
            "title": ("Very long PDF table-of-contents title " + "x" * 1_801)
            if index == 0
            else f"Bounded title {index}",
            "url": f"https://example.invalid/documents/{index}.pdf",
            "content": ("retrieved document excerpt " + "y" * 5_000)
            if index == 1
            else f"retrieved excerpt {index}",
        }
        for index in range(5)
    ]


def all_enum_strings(value):
    if isinstance(value, dict):
        if isinstance(value.get("enum"), list):
            yield from (item for item in value["enum"] if isinstance(item, str))
        for child in value.values():
            yield from all_enum_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_enum_strings(child)


class Cycle030GeminiSchemaCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = adversarial_retrieval_sources()
        self.context = {"sources": self.sources}

    def test_original_long_title_enum_failure_is_reproduced_locally(self) -> None:
        fixed = strict_output_schema(
            GeneralOpinionOutput,
            input_data={"retrieval_context": self.context},
        )
        legacy = deepcopy(fixed)
        properties = legacy["$defs"]["SupportingSource"]["properties"]
        properties["source"] = {
            "type": "string",
            "enum": [item["title"] for item in self.sources],
        }
        properties["url"] = {
            "type": "string",
            "enum": [item["url"] for item in self.sources],
        }

        with self.assertRaisesRegex(
            ProviderRequestSchemaError,
            "dynamic enums must use compact stable identifiers",
        ):
            validate_provider_schema_compatibility(GEMINI_MODEL, legacy)

        violations = gemini_schema_violations(legacy)
        self.assertEqual(len(violations), 1)
        self.assertIn("SupportingSource/properties/source/enum", violations[0].path)

    def test_general_opinion_schema_binds_only_compact_source_ids(self) -> None:
        schema = strict_output_schema(
            GeneralOpinionOutput,
            input_data={"retrieval_context": self.context},
        )
        validate_provider_schema_compatibility(GEMINI_MODEL, schema)
        properties = schema["$defs"]["SupportingSource"]["properties"]
        self.assertEqual(
            properties["source_id"]["enum"],
            [item["source_id"] for item in self.sources],
        )
        self.assertEqual(set(properties), {"source_id"})
        self.assertEqual(
            schema["$defs"]["SupportingSource"]["required"],
            ["source_id"],
        )
        self.assertNotIn(self.sources[0]["title"], list(all_enum_strings(schema)))

    def test_all_eight_retrieval_agents_survive_long_payload_faults(self) -> None:
        # General Opinion plus all seven Researcher retrieval specialists.
        opinion = strict_output_schema(
            GeneralOpinionOutput,
            input_data={"retrieval_context": self.context},
        )
        validate_provider_schema_compatibility(GEMINI_MODEL, opinion)
        checked = ["producer.general_opinion_analyst"]

        for target, agent_id in RESEARCH_TARGET_MAP.items():
            with self.subTest(agent_id=agent_id):
                schema = strict_output_schema(
                    ResearchResult,
                    input_data={
                        "research_target": ResearchTarget(target).value,
                        "target_agent_id": agent_id,
                        "retrieval_context": self.context,
                    },
                )
                validate_provider_schema_compatibility(GEMINI_MODEL, schema)
                source = schema["$defs"]["ResearchSource"]["properties"]
                self.assertEqual(
                    source["source_id"]["enum"],
                    [item["source_id"] for item in self.sources],
                )
                self.assertNotIn("title", source)
                self.assertNotIn("url", source)
                self.assertNotIn("retrieved_at", source)
                self.assertNotIn("relevant_excerpt", source)
                self.assertNotIn(self.sources[0]["title"], list(all_enum_strings(schema)))
                self.assertNotIn(self.sources[1]["content"], list(all_enum_strings(schema)))
                checked.append(agent_id)

        self.assertEqual(len(checked), 8)

    def test_secret_free_request_builder_exposes_exact_provider_parameters(self) -> None:
        input_data = {
            "selected_topic": {
                "topic_id": "topic_cycle030",
                "title": "Generative AI and copyright",
                "selection_reason": "saved Producer selection",
            },
            "revision_context": None,
            "retrieval_context": self.context,
        }
        body = OpenRouterModelProvider.build_request_body(
            model=GEMINI_MODEL,
            system_prompt="cycle030 reconstructed system prompt",
            input_data=input_data,
            output_schema=GeneralOpinionOutput,
        )

        self.assertEqual(body["provider"], {"require_parameters": True})
        self.assertNotIn("plugins", body)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["messages"][1]["role"], "user")
        self.assertNotIn("Authorization", str(body))
        self.assertNotIn("api_key", str(body).lower())


if __name__ == "__main__":
    unittest.main()
