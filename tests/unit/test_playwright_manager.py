import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from common.models.errors import ProviderCapabilityError, RetryableAgentError
from common.models.pmp import PMPMessage
from playwright.schemas import (
    CitationEditingResult,
    NarrativeSectionType,
    ScriptDraft,
    VisualPlan,
)
from playwright.workflow import AGENT_ORDER
from providers.mock import playwright_fixtures
from providers.mock_provider import MockModelProvider
from tests.playwright_helpers import make_playwright_handoff, make_playwright_manager


class PersistentVisualErrorProvider(MockModelProvider):
    async def generate_structured(self, **kwargs):
        if kwargs["output_schema"] is VisualPlan:
            self.calls.append("VisualPlan")
            self._count_playwright_call("VisualPlan")
            return await self._playwright_result(
                kwargs["input_data"],
                lambda data: playwright_fixtures.visual_plan(data, mismatch=True),
            )
        return await super().generate_structured(**kwargs)


class InterruptScriptwriterOnceProvider(MockModelProvider):
    def __init__(self):
        super().__init__()
        self.interrupted = False

    async def generate_structured(self, **kwargs):
        if kwargs["output_schema"] is ScriptDraft and not self.interrupted:
            self.interrupted = True
            self.calls.append("ScriptDraft")
            self.agent_calls.append("playwright.scriptwriter")
            raise RetryableAgentError(
                "OpenRouter response body was interrupted before completion",
                automatic_retry_allowed=False,
                provider="OpenRouterModelProvider",
                model_id="~anthropic/claude-sonnet-latest",
            )
        return await super().generate_structured(**kwargs)


class VisualCapabilityErrorOnceProvider(MockModelProvider):
    def __init__(self):
        super().__init__()
        self.capability_failed = False
        self.visual_models: list[str] = []

    async def generate_structured(self, **kwargs):
        if kwargs["output_schema"] is VisualPlan:
            self.visual_models.append(kwargs["model"])
            if not self.capability_failed:
                self.capability_failed = True
                self.calls.append("VisualPlan")
                self.agent_calls.append("playwright.visual_director")
                raise ProviderCapabilityError(
                    "OpenRouter HTTP 404: No endpoints found that can handle "
                    "the requested parameters",
                    http_status=404,
                    provider="openrouter",
                    model_id=kwargs["model"],
                )
        return await super().generate_structured(**kwargs)


class PersistentMissingCitationProvider(MockModelProvider):
    async def generate_structured(self, **kwargs):
        if kwargs["output_schema"] is CitationEditingResult:
            self.calls.append("CitationEditingResult")
            self._count_playwright_call("CitationEditingResult")
            return await self._playwright_result(
                kwargs["input_data"],
                lambda data: playwright_fixtures.citation_editing(
                    data,
                    missing_mapping=True,
                ),
            )
        return await super().generate_structured(**kwargs)


class OmitCanonicalDisclosuresProvider(MockModelProvider):
    async def generate_structured(self, **kwargs):
        result = await super().generate_structured(**kwargs)
        if kwargs["output_schema"] is CitationEditingResult:
            result["citation_validated_script"]["limitations"] = []
            result["citation_manifest"]["disclosure_checks"] = []
        return result


class InterruptVisualDuringExplicitRevisionProvider(MockModelProvider):
    def __init__(self):
        super().__init__(playwright_missing_citation_once=True)
        self.visual_attempts = 0

    async def generate_structured(self, **kwargs):
        if kwargs["output_schema"] is VisualPlan:
            self.visual_attempts += 1
            if self.visual_attempts == 2:
                self.calls.append("VisualPlan")
                self.agent_calls.append("playwright.visual_director")
                raise RetryableAgentError(
                    "OpenRouter response body was interrupted before completion",
                    automatic_retry_allowed=False,
                    provider="OpenRouterModelProvider",
                    model_id=kwargs["model"],
                )
        return await super().generate_structured(**kwargs)


class PlaywrightManagerTests(unittest.TestCase):
    def test_mock_script_fixture_supports_every_narrative_section_type(self):
        context = {
            "topic": "topic",
            "central_question": "question",
            "final_recommendation": "recommendation",
        }
        for section_type in NarrativeSectionType:
            self.assertTrue(
                playwright_fixtures._key_message(section_type.value, context)
            )
            self.assertTrue(
                playwright_fixtures._speaker_text(section_type.value, context)
            )

    def test_normal_flow_delivers_six_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(handoff))
            self.assertEqual(state.status, "COMPLETED")
            self.assertTrue(state.delivered)
            self.assertEqual(set(state.delivery_paths), {
                "final_script_package", "script", "citation_manifest",
                "source_list", "visual_plan", "production_notes",
            })
            self.assertTrue(all(Path(path).exists() for path in state.delivery_paths.values()))
            self.assertEqual(state.message_history[-1].message_type, "final_script_delivery")
            self.assertEqual(state.message_history[-1].receiver_agent_id, "system.final_output")

    def test_agents_execute_in_required_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            calls = [item for item in provider.agent_calls if item.startswith("playwright.")]
            self.assertEqual(calls, AGENT_ORDER)
            self.assertEqual(state.completed_agents, AGENT_ORDER)

    def test_start_is_idempotent_after_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            manager = make_playwright_manager(data_dir, provider)
            manager.repository.write_json_atomic(
                manager.repository.conclusion_outbox_dir / f"{handoff.workflow_id}.json",
                handoff.model_dump(mode="json"),
            )
            first = asyncio.run(manager.start(handoff.workflow_id))
            call_count = len(provider.calls)
            second = asyncio.run(manager.start(handoff.workflow_id))
            self.assertEqual(first.status, second.status)
            self.assertEqual(len(provider.calls), call_count)

    def test_human_selection_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            raw = make_playwright_handoff(data_dir, provider).model_dump(mode="json")
            raw["payload"]["human_selection"] = {}
            state = asyncio.run(
                make_playwright_manager(data_dir, provider).start_from_message(
                    PMPMessage.model_validate(raw)
                )
            )
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.error["code"], "HUMAN_SELECTION_MISSING")
            self.assertFalse(state.delivered)

    def test_invalid_conclusion_routing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            raw = make_playwright_handoff(data_dir, provider).model_dump(mode="json")
            raw["sender_agent_id"] = "deliberation.manager"
            with self.assertRaises(ValueError):
                asyncio.run(
                    make_playwright_manager(data_dir, provider).start_from_message(
                        PMPMessage.model_validate(raw)
                    )
                )

    def test_legacy_uppercase_conclusion_readiness_is_accepted_on_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            raw = make_playwright_handoff(data_dir, provider).model_dump(mode="json")
            for review in (
                raw["payload"]["quality_review"],
                raw["payload"]["conclusion_package"]["quality_review"],
            ):
                review["playwright_readiness"] = "READY"
            problems = make_playwright_manager(data_dir, provider)._handoff_problems(
                raw["payload"]
            )
            self.assertNotIn(
                "PLAYWRIGHT_NOT_READY",
                {item["code"] for item in problems},
            )

    def test_mismatched_conclusion_quality_review_copies_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            raw = make_playwright_handoff(data_dir, provider).model_dump(mode="json")
            raw["payload"]["quality_review"]["review_id"] = "review_conflict"
            problems = make_playwright_manager(data_dir, provider)._handoff_problems(
                raw["payload"]
            )
            self.assertIn(
                "CONCLUSION_QUALITY_REVIEW_MISMATCH",
                {item["code"] for item in problems},
            )

            state = asyncio.run(
                make_playwright_manager(data_dir, provider).start_from_message(
                    PMPMessage.model_validate(raw)
                )
            )
            self.assertEqual(state.status, "WAITING_UPSTREAM_REVISION")
            self.assertFalse(
                any(
                    agent_id.startswith("playwright.")
                    for agent_id in provider.agent_calls
                )
            )

    def test_incomplete_traceability_requests_conclusion_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            raw = make_playwright_handoff(data_dir, provider).model_dump(mode="json")
            raw["payload"]["traceability_manifest"]["claim_ids"] = []
            raw["message_id"] = str(uuid4())
            state = asyncio.run(
                make_playwright_manager(data_dir, provider).start_from_message(
                    PMPMessage.model_validate(raw)
                )
            )
            self.assertEqual(state.status, "WAITING_UPSTREAM_REVISION")
            path = data_dir / "outbox" / "conclusion_revision" / f"{state.workflow_id}.json"
            message = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "revision_request")
            self.assertEqual(message["receiver_agent_id"], "conclusion.manager")

    def test_upstream_revision_can_resume_with_new_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            good = make_playwright_handoff(data_dir, provider)
            bad = good.model_dump(mode="json")
            bad["message_id"] = str(uuid4())
            bad["payload"]["traceability_manifest"]["claim_ids"] = []
            manager = make_playwright_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(PMPMessage.model_validate(bad)))
            self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
            resumed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(resumed.status, "COMPLETED")
            self.assertEqual(resumed.upstream_revision_count, 1)

    def test_agent_failure_fails_without_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(fail_agent_ids={"playwright.scriptwriter"})
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            self.assertEqual(state.status, "FAILED")
            self.assertIn("playwright.scriptwriter", state.failed_agents)
            self.assertFalse(state.delivered)

    def test_operator_retry_reuses_narrative_and_retries_scriptwriter_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = InterruptScriptwriterOnceProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            self.assertEqual(failed.status, "FAILED")
            self.assertIsNotNone(failed.narrative_blueprint)
            calls_before_recover = list(provider.agent_calls)
            with self.assertRaisesRegex(ValueError, "explicit provider retry"):
                asyncio.run(manager.recover(failed.workflow_id))
            self.assertEqual(provider.agent_calls, calls_before_recover)

            completed = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(
                provider.agent_calls.count("playwright.narrative_architect"),
                1,
            )
            self.assertEqual(provider.agent_calls.count("playwright.scriptwriter"), 2)
            self.assertEqual(
                provider.agent_calls.count("playwright.evidence_citation_editor"),
                1,
            )
            self.assertEqual(provider.agent_calls.count("playwright.visual_director"), 1)
            script_task_ids = [
                message.payload.get("task_id")
                for message in completed.message_history
                if message.sender_agent_id == "playwright.manager"
                and message.receiver_agent_id == "playwright.scriptwriter"
            ]
            self.assertEqual(
                script_task_ids,
                [
                    "playwright_script_upstream_0_revision_0",
                    "playwright_script_upstream_0_revision_0_operator_retry_1",
                ],
            )
            with self.assertRaises(ValueError):
                asyncio.run(manager.retry_provider_call(failed.workflow_id))

    def test_visual_capability_repair_uses_one_distinct_model_and_persists_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = VisualCapabilityErrorOnceProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            manager.registry.get("playwright.visual_director").model = (
                "z-ai/glm-4.5-air"
            )
            failed = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.failed_agents, ["playwright.visual_director"])
            self.assertEqual(provider.visual_models, ["z-ai/glm-4.5-air"])
            # Checkpoints written before ProviderCapabilityError existed used
            # the generic non-retryable class for this exact OpenRouter 404.
            failed.message_history[-1].payload["error_class"] = (
                "NonRetryableAgentError"
            )
            manager.repository.save(failed)
            completed_before = list(failed.completed_agents)

            with self.assertRaisesRegex(ValueError, "capability repair"):
                asyncio.run(manager.recover(failed.workflow_id))
            with self.assertRaisesRegex(ValueError, "not eligible"):
                asyncio.run(manager.retry_provider_call(failed.workflow_id))
            with self.assertRaisesRegex(ValueError, "different model"):
                manager.authorize_provider_capability_repair(
                    failed.workflow_id,
                    repair_model_id="z-ai/glm-4.5-air",
                )
            self.assertEqual(provider.visual_models, ["z-ai/glm-4.5-air"])

            completed = asyncio.run(
                manager.repair_provider_capability(
                    failed.workflow_id,
                    repair_model_id="openai/gpt-5-mini",
                )
            )
            self.assertEqual(completed.status, "COMPLETED")
            self.assertTrue(completed.delivered)
            self.assertEqual(
                completed_before,
                [
                    "playwright.narrative_architect",
                    "playwright.scriptwriter",
                    "playwright.evidence_citation_editor",
                ],
            )
            self.assertEqual(
                provider.visual_models,
                ["z-ai/glm-4.5-air", "openai/gpt-5-mini"],
            )
            visual_task_ids = [
                message.payload.get("task_id")
                for message in completed.message_history
                if message.sender_agent_id == "playwright.manager"
                and message.receiver_agent_id == "playwright.visual_director"
            ]
            self.assertEqual(
                visual_task_ids,
                [
                    "playwright_visual_upstream_0_revision_0",
                    "playwright_visual_upstream_0_revision_0_provider_capability_repair_1",
                ],
            )
            authorization = (
                manager.provider_capability_repair_store.for_original_task(
                    workflow_id=completed.workflow_id,
                    provider_id="mock",
                    original_task_id="playwright_visual_upstream_0_revision_0",
                )
            )
            self.assertEqual(authorization.status, "CONSUMED")
            self.assertEqual(
                authorization.source_error_class,
                "NonRetryableAgentError",
            )
            binding = manager.provider_model_compatibility_store.resolve(
                provider_id="mock",
                agent_id="playwright.visual_director",
                output_schema_id=(
                    "playwright.schemas.visual_plan.VisualPlan"
                ),
                configured_model_id="z-ai/glm-4.5-air",
            )
            self.assertEqual(binding.compatible_model_id, "openai/gpt-5-mini")

            next_handoff = make_playwright_handoff(data_dir, provider)
            next_state = asyncio.run(manager.start_from_message(next_handoff))
            self.assertEqual(next_state.status, "COMPLETED")
            self.assertEqual(provider.visual_models[-1], "openai/gpt-5-mini")

    def test_visual_capability_repair_rejects_uncorrelated_404_without_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = VisualCapabilityErrorOnceProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            manager.registry.get("playwright.visual_director").model = (
                "z-ai/glm-4.5-air"
            )
            failed = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            failed.message_history[-1].payload["message"] = (
                "OpenRouter HTTP 404: unrelated resource not found"
            )
            manager.repository.save(failed)
            calls_before = list(provider.visual_models)

            with self.assertRaisesRegex(ValueError, "endpoint-capability 404"):
                manager.authorize_provider_capability_repair(
                    failed.workflow_id,
                    repair_model_id="openai/gpt-5-mini",
                )
            self.assertEqual(provider.visual_models, calls_before)
            self.assertFalse(
                (data_dir / "provider_capability_repair_authorizations").exists()
            )

    def test_legacy_agent_id_reservation_can_use_one_explicit_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = InterruptScriptwriterOnceProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            request = failed.message_history[-2]
            error = failed.message_history[-1]
            self.assertEqual(request.receiver_agent_id, "playwright.scriptwriter")
            request.payload.pop("task_id")
            error.payload["task_id"] = None
            reservation_dir = (
                data_dir
                / "provider_call_reservations"
                / "mock"
                / failed.workflow_id
            )
            (reservation_dir / "playwright_script_upstream_0_revision_0.json").replace(
                reservation_dir / "playwright.scriptwriter.json"
            )
            reservation = json.loads(
                (reservation_dir / "playwright.scriptwriter.json").read_text(
                    encoding="utf-8"
                )
            )
            reservation["task_id"] = "playwright.scriptwriter"
            manager.repository.write_json_atomic(
                reservation_dir / "playwright.scriptwriter.json",
                reservation,
            )
            manager.repository.save(failed)

            completed = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            self.assertEqual(completed.status, "COMPLETED")
            retry_requests = [
                message
                for message in completed.message_history
                if message.receiver_agent_id == "playwright.scriptwriter"
                and message.payload.get("task_id")
                == "playwright.scriptwriter_operator_retry_1"
            ]
            self.assertEqual(len(retry_requests), 1)
            self.assertEqual(
                provider.agent_calls.count("playwright.narrative_architect"),
                1,
            )

    def test_recover_promotes_saved_result_without_provider_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            completed = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            completed.status = "FAILED"
            completed.script_draft = None
            completed.final_script_package = None
            completed.delivery_paths = {}
            completed.delivered = False
            completed.completed_at = None
            completed.error = {"stage": "checkpoint", "message": "injected write fault"}
            manager.repository.save(completed)
            calls_before = list(provider.agent_calls)

            recovered = asyncio.run(manager.recover(completed.workflow_id))
            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(provider.agent_calls, calls_before)
            self.assertIsNotNone(recovered.script_draft)

    def test_recover_blocks_unanswered_provider_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = InterruptScriptwriterOnceProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            failed.message_history.pop()
            manager.repository.save(failed)
            calls_before = list(provider.agent_calls)
            with self.assertRaisesRegex(ValueError, "unanswered Provider request"):
                asyncio.run(manager.recover(failed.workflow_id))
            self.assertEqual(provider.agent_calls, calls_before)

    def test_recover_rejects_corrupted_failure_correlation_without_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = InterruptScriptwriterOnceProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            failed.message_history[-1].payload["task_id"] = "different_task"
            manager.repository.save(failed)
            calls_before = list(provider.agent_calls)
            with self.assertRaisesRegex(ValueError, "task identity is not correlated"):
                asyncio.run(manager.recover(failed.workflow_id))
            self.assertEqual(provider.agent_calls, calls_before)

    def test_playwright_revision_uses_distinct_cycle_task_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_unsupported_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            script_task_ids = [
                message.payload.get("task_id")
                for message in state.message_history
                if message.sender_agent_id == "playwright.manager"
                and message.receiver_agent_id == "playwright.scriptwriter"
            ]
            self.assertEqual(
                script_task_ids,
                [
                    "playwright_script_upstream_0_revision_0",
                    "playwright_script_upstream_0_revision_1",
                ],
            )
            self.assertEqual(len(script_task_ids), len(set(script_task_ids)))

    def test_unsupported_claim_revision_reruns_script_and_dependents(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_unsupported_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            calls = [item for item in provider.agent_calls if item.startswith("playwright.")]
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.revision_count, 1)
            self.assertEqual(calls.count("playwright.narrative_architect"), 1)
            self.assertEqual(calls.count("playwright.scriptwriter"), 2)
            self.assertEqual(calls.count("playwright.evidence_citation_editor"), 2)
            self.assertEqual(calls.count("playwright.visual_director"), 2)
            revision_requests = [
                message for message in state.message_history
                if message.receiver_agent_id == "playwright.scriptwriter"
                and message.message_type == "revision_request"
            ]
            self.assertEqual(len(revision_requests), 1)

    def test_missing_citation_revision_starts_at_evidence_editor(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_missing_citation_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            calls = [item for item in provider.agent_calls if item.startswith("playwright.")]
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(calls.count("playwright.scriptwriter"), 1)
            self.assertEqual(calls.count("playwright.evidence_citation_editor"), 2)
            self.assertEqual(calls.count("playwright.visual_director"), 2)

    def test_safe_mode_explicit_revision_runs_exactly_one_dependency_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_missing_citation_once=True)
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            self.assertEqual(blocked.status, "BLOCKED")
            self.assertEqual(blocked.revision_count, 0)
            self.assertEqual(blocked.final_gate_result["status"], "REVISION_REQUIRED")
            narrative_before = blocked.narrative_blueprint
            script_before = blocked.script_draft

            # Compatibility with the saved Cycle 026 gate, which was labelled
            # BLOCKED only because the old constructor collapsed the limit to 0.
            blocked.final_gate_result["status"] = "BLOCKED"
            manager.repository.save(blocked)
            completed = asyncio.run(manager.revise(blocked.workflow_id))

            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(completed.revision_count, 1)
            self.assertEqual(completed.narrative_blueprint, narrative_before)
            self.assertEqual(completed.script_draft, script_before)
            self.assertEqual(
                provider.agent_calls.count("playwright.evidence_citation_editor"),
                2,
            )
            self.assertEqual(
                provider.agent_calls.count("playwright.visual_director"),
                2,
            )
            task_ids = [
                message.payload.get("task_id")
                for message in completed.message_history
                if message.sender_agent_id == "playwright.manager"
                and message.receiver_agent_id
                == "playwright.evidence_citation_editor"
            ]
            self.assertEqual(
                task_ids,
                [
                    "playwright_citation_upstream_0_revision_0",
                    "playwright_citation_upstream_0_revision_1",
                ],
            )

    def test_safe_mode_never_enters_second_automatic_playwright_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = PersistentMissingCitationProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            blocked_again = asyncio.run(manager.revise(blocked.workflow_id))
            self.assertEqual(blocked_again.status, "BLOCKED")
            self.assertEqual(blocked_again.revision_count, 1)
            self.assertEqual(
                provider.agent_calls.count("playwright.evidence_citation_editor"),
                2,
            )
            self.assertEqual(
                provider.agent_calls.count("playwright.visual_director"),
                2,
            )

    def test_manager_preserves_all_canonical_disclosures_without_llm_recopy(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = OmitCanonicalDisclosuresProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            raw = handoff.model_dump(mode="json")
            canonical = [f"limitation_{index}" for index in range(113)]
            raw["payload"]["limitations_to_disclose"] = canonical
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(
                manager.start_from_message(PMPMessage.model_validate(raw))
            )
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(
                state.citation_validated_script["limitations"],
                canonical,
            )
            self.assertEqual(
                [
                    item["limitation"]
                    for item in state.citation_manifest["disclosure_checks"]
                ],
                canonical,
            )

    def test_explicit_revision_fault_recovers_without_replaying_citation(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = InterruptVisualDuringExplicitRevisionProvider()
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            blocked = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )
            failed = asyncio.run(manager.revise(blocked.workflow_id))
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.revision_count, 1)
            citation_calls = provider.agent_calls.count(
                "playwright.evidence_citation_editor"
            )
            completed = asyncio.run(manager.retry_provider_call(failed.workflow_id))
            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(
                provider.agent_calls.count("playwright.evidence_citation_editor"),
                citation_calls,
            )
            self.assertEqual(provider.visual_attempts, 3)

    def test_visual_revision_reruns_only_visual_director(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_visual_mismatch_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            calls = [item for item in provider.agent_calls if item.startswith("playwright.")]
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(calls.count("playwright.evidence_citation_editor"), 1)
            self.assertEqual(calls.count("playwright.visual_director"), 2)

    def test_chart_without_source_is_revised(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_missing_chart_source_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.revision_count, 1)
            self.assertIn(state.final_gate_result["status"], {"APPROVED", "APPROVED_WITH_LIMITATIONS"})

    def test_two_failed_revisions_block_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = PersistentVisualErrorProvider()
            manager = make_playwright_manager(data_dir, provider, max_revisions=2)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 2)
            self.assertFalse(state.delivered)
            self.assertEqual(provider.agent_calls.count("playwright.visual_director"), 3)

    def test_rd_trace_is_recorded_for_manager_and_four_agents(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            traces = state.role_definition_usage
            agent_ids = {item["agent_id"] for item in traces}
            hashes = {item["role_definition_hash"] for item in traces}
            self.assertEqual(agent_ids, {"playwright.manager", *AGENT_ORDER})
            self.assertEqual(len(hashes), 5)
            self.assertTrue(all(value.startswith("sha256:") for value in hashes))

    def test_final_conclusion_identity_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(handoff))
            package = manager.repository.load_final_package(state.workflow_id)
            self.assertEqual(
                package.final_conclusion_id,
                handoff.payload["final_conclusion"]["final_conclusion_id"],
            )
            self.assertEqual(
                package.human_selection_id,
                handoff.payload["human_selection"]["selection_id"],
            )

    def test_no_independent_playwright_quality_reviewer_is_registered(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_playwright_manager(Path(temporary), MockModelProvider())
            self.assertEqual(manager.registry.agent_ids, set(AGENT_ORDER))
            self.assertNotIn("playwright.quality_reviewer", manager.registry.agent_ids)


if __name__ == "__main__":
    unittest.main()
