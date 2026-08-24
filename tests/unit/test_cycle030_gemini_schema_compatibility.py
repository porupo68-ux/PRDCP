from __future__ import annotations

from copy import deepcopy
import json
import unittest

from common.models.errors import ProviderRequestSchemaError
from common.provider_schema_compatibility import (
    GEMINI_MAX_ANY_OF_BRANCHES_PER_NODE,
    GEMINI_BATCH_SCHEMA_KEYWORDS,
    _walk_schema_nodes,
    gemini_schema_violations,
    specialize_provider_output_schema,
    validate_provider_schema_compatibility,
)
from common.structured_outputs import strict_output_schema, strict_schema_violations
from conclusion.schemas.position_candidate import PositionGenerationResult
from producer.schemas.general_opinion import GeneralOpinionOutput
from producer.schemas.research_plan import ResearchTarget
from producer.schemas.topic_scout import TopicScoutOutput
from providers.openrouter_provider import OpenRouterModelProvider
from researcher.schemas.research_result import ResearchResult
from researcher.schemas.research_task import RESEARCH_TARGET_MAP
from tests.unit.test_structured_output_schemas import OPENROUTER_OUTPUT_SCHEMAS


GEMINI_MODEL = "google/gemini-3.7-flash"
GEMINI_BATCH_MODEL = GEMINI_MODEL + ":batch"


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
        fixed = specialize_provider_output_schema(
            GEMINI_MODEL,
            strict_output_schema(
                GeneralOpinionOutput,
                input_data={"retrieval_context": self.context},
            ),
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
        schema = specialize_provider_output_schema(
            GEMINI_MODEL,
            strict_output_schema(
                GeneralOpinionOutput,
                input_data={"retrieval_context": self.context},
            ),
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

    def test_repeated_dynamic_enum_complexity_fails_before_http(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                f"selector_{index}": {
                    "type": "string",
                    "enum": [f"source_{item}" for item in range(25)],
                }
                for index in range(4)
            },
            "required": [f"selector_{index}" for index in range(4)],
            "additionalProperties": False,
        }

        with self.assertRaisesRegex(
            ProviderRequestSchemaError,
            "reuse a shared \\$defs selector",
        ):
            validate_provider_schema_compatibility(GEMINI_MODEL, schema)

    def test_all_eight_retrieval_agents_survive_long_payload_faults(self) -> None:
        # General Opinion plus all seven Researcher retrieval specialists.
        opinion = specialize_provider_output_schema(
            GEMINI_MODEL,
            strict_output_schema(
                GeneralOpinionOutput,
                input_data={"retrieval_context": self.context},
            ),
        )
        validate_provider_schema_compatibility(GEMINI_MODEL, opinion)
        checked = ["producer.general_opinion_analyst"]

        for target, agent_id in RESEARCH_TARGET_MAP.items():
            with self.subTest(agent_id=agent_id):
                schema = specialize_provider_output_schema(
                    GEMINI_MODEL,
                    strict_output_schema(
                        ResearchResult,
                        input_data={
                            "research_target": ResearchTarget(target).value,
                            "target_agent_id": agent_id,
                            "retrieval_context": self.context,
                        },
                    ),
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

    def test_topic_scout_batch_wire_schema_removes_only_unsupported_constraints(self) -> None:
        application_schema = strict_output_schema(TopicScoutOutput)
        candidate = application_schema["$defs"]["TopicCandidate"]["properties"]
        self.assertEqual(candidate["url"]["format"], "uri")
        self.assertEqual(candidate["published_at"]["format"], "date-time")
        self.assertIn("minLength", candidate["title"])

        wire_schema = specialize_provider_output_schema(
            GEMINI_BATCH_MODEL,
            application_schema,
        )
        wire_candidate = wire_schema["properties"]["topic_candidates"]["items"][
            "properties"
        ]
        self.assertNotIn("format", wire_candidate["url"])
        self.assertEqual(wire_candidate["published_at"]["format"], "date-time")
        self.assertNotIn("minLength", wire_candidate["title"])
        self.assertNotIn("$defs", wire_schema)
        self.assertFalse(any("$ref" in node for node in _all_dicts(wire_schema)))
        self.assertEqual(strict_schema_violations(wire_schema), [])
        self.assertEqual(gemini_schema_violations(wire_schema, batch=True), [])

        # Provider specialization is copy-on-write. Pydantic/local validation
        # therefore remains the authoritative URI/length contract.
        self.assertEqual(candidate["url"]["format"], "uri")
        self.assertIn("minLength", candidate["title"])

    def test_const_is_preserved_as_singleton_enum_for_gemini_batch(self) -> None:
        application_schema = strict_output_schema(ResearchResult)
        self.assertTrue(
            any(
                isinstance(value, dict) and "const" in value
                for value in _all_dicts(application_schema)
            )
        )
        wire_schema = specialize_provider_output_schema(
            GEMINI_BATCH_MODEL,
            application_schema,
        )
        self.assertFalse(
            any(
                isinstance(value, dict) and "const" in value
                for value in _all_dicts(wire_schema)
            )
        )
        self.assertEqual(gemini_schema_violations(wire_schema, batch=True), [])

    def test_all_22_root_schemas_are_gemini_batch_compiler_safe(self) -> None:
        self.assertEqual(len(OPENROUTER_OUTPUT_SCHEMAS), 22)
        for output_model in OPENROUTER_OUTPUT_SCHEMAS:
            with self.subTest(output_model=output_model.__name__):
                wire_schema = specialize_provider_output_schema(
                    GEMINI_BATCH_MODEL,
                    strict_output_schema(output_model),
                )
                validate_provider_schema_compatibility(
                    GEMINI_BATCH_MODEL,
                    wire_schema,
                )
                self.assertEqual(strict_schema_violations(wire_schema), [])
                self.assertEqual(
                    gemini_schema_violations(wire_schema, batch=True),
                    [],
                )
                self.assertNotIn("$defs", wire_schema)
                self.assertFalse(
                    any("$ref" in node for node in _all_dicts(wire_schema))
                )
                self.assertLessEqual(
                    len(
                        json.dumps(
                            wire_schema,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                    48_000,
                )

    def test_all_22_root_schemas_are_gemini_sync_compiler_safe(self) -> None:
        self.assertEqual(len(OPENROUTER_OUTPUT_SCHEMAS), 22)
        for output_model in OPENROUTER_OUTPUT_SCHEMAS:
            with self.subTest(output_model=output_model.__name__):
                wire_schema = specialize_provider_output_schema(
                    GEMINI_MODEL,
                    strict_output_schema(output_model),
                )
                validate_provider_schema_compatibility(
                    GEMINI_MODEL,
                    wire_schema,
                )
                self.assertEqual(strict_schema_violations(wire_schema), [])
                for _path, node in _walk_schema_nodes(wire_schema):
                    self.assertNotIn("title", node)
                    self.assertNotIn("minItems", node)
                    self.assertNotIn("maxItems", node)

    def test_position_schema_compaction_preserves_strict_shape_and_local_bounds(self) -> None:
        application_schema = strict_output_schema(PositionGenerationResult)
        candidate = application_schema["$defs"]["PositionCandidate"]
        self.assertEqual(len(candidate["properties"]), 27)
        self.assertIn("minItems", candidate["properties"]["proposed_actions"])

        wire_schema = specialize_provider_output_schema(
            GEMINI_MODEL,
            application_schema,
        )
        wire_candidate = wire_schema["$defs"]["PositionCandidate"]
        self.assertEqual(len(wire_candidate["properties"]), 27)
        self.assertEqual(
            set(wire_candidate["required"]),
            set(wire_candidate["properties"]),
        )
        self.assertFalse(wire_candidate["additionalProperties"])
        self.assertFalse(
            any("title" in node for _path, node in _walk_schema_nodes(wire_schema))
        )
        self.assertFalse(
            any("minItems" in node for _path, node in _walk_schema_nodes(wire_schema))
        )
        self.assertFalse(
            any("maxItems" in node for _path, node in _walk_schema_nodes(wire_schema))
        )

        # The application contract is unchanged and still rejects an empty
        # candidate collection after the provider response is decoded.
        with self.assertRaises(ValueError):
            PositionGenerationResult.model_validate(
                {
                    "position_generation_result_id": "position_generation_x",
                    "task_id": "task_x",
                    "decision_context_id": "decision_context_x",
                    "position_candidates": [],
                    "diversity_dimensions": ["implementation"],
                    "generation_notes": [],
                    "missing_information": [],
                }
            )

    def test_unknown_gemini_batch_schema_keyword_fails_before_http(self) -> None:
        schema = specialize_provider_output_schema(
            GEMINI_BATCH_MODEL,
            strict_output_schema(TopicScoutOutput),
        )
        schema["unsupportedFutureKeyword"] = True
        with self.assertRaisesRegex(
            ProviderRequestSchemaError,
            "unsupported Gemini JSON Schema keyword",
        ):
            validate_provider_schema_compatibility(GEMINI_BATCH_MODEL, schema)
        self.assertNotIn("unsupportedFutureKeyword", GEMINI_BATCH_SCHEMA_KEYWORDS)

    def test_gemini_rejects_union_branch_explosion_before_http(self) -> None:
        branch = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "anyOf": [deepcopy(branch) for _ in range(
                            GEMINI_MAX_ANY_OF_BRANCHES_PER_NODE + 1
                        )]
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(
            ProviderRequestSchemaError,
            "union contains",
        ):
            validate_provider_schema_compatibility(GEMINI_MODEL, schema)

    def test_batch_request_builder_uses_specialized_wire_schema(self) -> None:
        body = OpenRouterModelProvider.build_request_body(
            model=GEMINI_BATCH_MODEL,
            system_prompt="batch schema regression",
            input_data={"topic": "test"},
            output_schema=TopicScoutOutput,
        )
        schema = body["response_format"]["json_schema"]["schema"]
        candidate = schema["properties"]["topic_candidates"]["items"]["properties"]
        self.assertNotIn("format", candidate["url"])
        self.assertEqual(candidate["published_at"]["format"], "date-time")
        self.assertNotIn("$defs", schema)
        self.assertFalse(any("$ref" in node for node in _all_dicts(schema)))
        self.assertEqual(gemini_schema_violations(schema, batch=True), [])

    def test_cyclic_gemini_batch_schema_fails_before_http(self) -> None:
        schema = {
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                    "required": ["child"],
                    "additionalProperties": False,
                }
            },
            "$ref": "#/$defs/Node",
        }
        with self.assertRaisesRegex(
            ProviderRequestSchemaError,
            "cyclic local reference",
        ):
            specialize_provider_output_schema(GEMINI_BATCH_MODEL, schema)


def _all_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _all_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_dicts(child)


if __name__ == "__main__":
    unittest.main()
