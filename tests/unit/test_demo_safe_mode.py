from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.models.errors import (
    NonRetryableAgentError,
    ProviderCapabilityError,
    RetryableAgentError,
)
from common.models.pmp import MessageType, PMPMessage
from config.settings import Settings
from conclusion.manager import ConclusionManager
from conclusion.registry import ConclusionRegistry
from deliberation.registry import DeliberationRegistry
from deliberation.schemas.integrated_analysis import InitialIntegratedAnalysis
from producer.manager import ProducerManager
from producer.registry import ProducerRegistry
from providers.mock_provider import MockModelProvider
from providers.openrouter_provider import OpenRouterModelProvider
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from storage.conclusion_workflow_repository import ConclusionWorkflowRepository
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from storage.workflow_repository import WorkflowRepository
from tests.conclusion_helpers import make_conclusion_handoff
from tests.deliberation_helpers import make_deliberation_handoff, make_manager
from tests.playwright_helpers import make_playwright_handoff, make_playwright_manager
from tests.researcher_helpers import make_handoff


class _RetryableIntegrationProvider(MockModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.integration_attempts = 0

    async def generate_structured(self, **kwargs: object) -> dict:
        if kwargs["output_schema"] is InitialIntegratedAnalysis:
            self.integration_attempts += 1
            raise RetryableAgentError("temporary integration failure")
        return await super().generate_structured(**kwargs)


class _OpenRouterNamespaceProvider(MockModelProvider):
    """Network-free provider double for the logical OpenRouter namespace."""

    provider_id = "openrouter"


class _FailingMockProvider(MockModelProvider):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def generate_structured(self, **_kwargs: object) -> dict:
        self.attempts += 1
        raise NonRetryableAgentError("intentional provider failure")


class _CapabilityFailingProvider(MockModelProvider):
    provider_id = "openrouter"

    def __init__(self) -> None:
        super().__init__()
        self.paid_attempts = 0

    def validate_request_budget(self, **kwargs: object) -> int:
        model = str(kwargs.get("model") or "")
        raise ProviderCapabilityError(
            "MODEL_CAPABILITY_ERROR: required strict structured output; Provider call = 0",
            provider="openrouter",
            model_id=model,
        )

    async def generate_structured(self, **kwargs: object) -> dict:
        self.paid_attempts += 1
        return await super().generate_structured(**kwargs)


class _RecoveringCapabilityProvider(MockModelProvider):
    provider_id = "openrouter"

    def __init__(self) -> None:
        super().__init__()
        self.capability_available = False

    def validate_request_budget(self, **kwargs: object) -> int:
        if not self.capability_available:
            raise ProviderCapabilityError(
                "MODEL_CAPABILITY_ERROR: temporary metadata failure; Provider call = 0",
                provider="openrouter",
                model_id=str(kwargs.get("model") or ""),
            )
        return 1


class DemoSafeModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_data_dir.cleanup)
        self._data_dir_environment = patch.dict(
            os.environ,
            {"PRDCP_DATA_DIR": self._temporary_data_dir.name},
        )
        self._data_dir_environment.start()
        self.addCleanup(self._data_dir_environment.stop)

    def test_setting_defaults_on_and_requires_explicit_false_to_disable(self) -> None:
        self.assertTrue(Settings.__dataclass_fields__["demo_safe_mode"].default)
        with patch.dict(os.environ, {"PRDCP_DEMO_SAFE_MODE": "false"}):
            self.assertFalse(Settings.from_env().demo_safe_mode)
        with patch.dict(os.environ, {"PRDCP_DEMO_SAFE_MODE": "typo"}):
            self.assertTrue(Settings.from_env().demo_safe_mode)

    def test_builtin_providers_expose_stable_logical_ids(self) -> None:
        self.assertEqual(MockModelProvider.provider_id, "mock")
        self.assertEqual(OpenRouterModelProvider.provider_id, "openrouter")

    def test_producer_revision_stops_without_manager_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(review_decisions=["revision_required", "approved"])
            registry = ProducerRegistry(provider, demo_safe_mode=True)
            manager = ProducerManager(
                registry,
                WorkflowRepository(Path(temporary)),
                demo_safe_mode=True,
            )

            state = asyncio.run(manager.start(user_topic="demo safe mode"))

            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 0)
            self.assertIn("Demo Safe Mode", state.error["message"])
            self.assertEqual(provider.calls.count("ResearchPlanOutput"), 1)
            self.assertEqual(provider.calls.count("QualityReviewOutput"), 1)
            self.assertTrue(
                any(
                    message.message_type == MessageType.REVISION_REQUEST.value
                    for message in state.message_history
                )
            )

            revised = asyncio.run(
                manager.revise(
                    state.workflow_id,
                    actor_id="test.operator",
                    actor_source="CLI",
                    reason="Regression test approval",
                )
            )
            self.assertEqual(revised.status, "COMPLETED")
            self.assertEqual(revised.revision_count, 1)
            self.assertEqual(revised.revision_control.phase, "completed")
            self.assertEqual(provider.calls.count("ResearchPlanOutput"), 2)
            self.assertEqual(provider.calls.count("QualityReviewOutput"), 2)
            calls = list(provider.calls)
            replay = asyncio.run(
                manager.revise(
                    state.workflow_id,
                    actor_id="test.operator",
                    actor_source="CLI",
                    reason="Regression test approval",
                )
            )
            self.assertEqual(replay.status, "COMPLETED")
            self.assertEqual(provider.calls, calls)

    def test_same_task_cannot_call_provider_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider()
            registry = ProducerRegistry(provider, demo_safe_mode=True)
            manager = ProducerManager(
                registry,
                WorkflowRepository(Path(temporary)),
                demo_safe_mode=True,
            )
            state = asyncio.run(manager.start(user_topic="duplicate guard"))
            request = next(
                message
                for message in state.message_history
                if message.receiver_agent_id == "producer.topic_scout"
            )

            response = asyncio.run(
                registry.get("producer.topic_scout").execute(request)
            )

            self.assertEqual(response.message_type, MessageType.ERROR.value)
            self.assertEqual(
                response.payload["error_class"],
                "NonRetryableAgentError",
            )
            self.assertEqual(provider.calls.count("TopicScoutOutput"), 1)

    def test_same_task_is_blocked_after_registry_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider()
            registry = ProducerRegistry(provider, demo_safe_mode=True)
            manager = ProducerManager(
                registry,
                WorkflowRepository(Path(temporary)),
                demo_safe_mode=True,
            )
            state = asyncio.run(manager.start(user_topic="persistent duplicate guard"))
            request = next(
                message
                for message in state.message_history
                if message.receiver_agent_id == "producer.topic_scout"
            )

            reservation_path = (
                Path(self._temporary_data_dir.name)
                / "provider_call_reservations"
                / "mock"
                / request.workflow_id
                / "producer.topic_scout.json"
            )
            reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
            self.assertEqual(
                reservation,
                {
                    "workflow_id": request.workflow_id,
                    "task_id": "producer.topic_scout",
                    "agent_id": "producer.topic_scout",
                    "provider": "MockModelProvider",
                    "model_id": "mock",
                    "reserved_at": reservation["reserved_at"],
                },
            )
            self.assertTrue(reservation["reserved_at"])

            recreated_registry = ProducerRegistry(provider, demo_safe_mode=True)
            response = asyncio.run(
                recreated_registry.get("producer.topic_scout").execute(request)
            )

            self.assertEqual(response.message_type, MessageType.ERROR.value)
            self.assertEqual(
                response.payload["error_class"],
                "NonRetryableAgentError",
            )
            self.assertEqual(provider.calls.count("TopicScoutOutput"), 1)

    def test_same_task_can_be_reserved_in_a_different_provider_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            mock_provider = MockModelProvider()
            mock_registry = ProducerRegistry(mock_provider, demo_safe_mode=True)
            manager = ProducerManager(
                mock_registry,
                WorkflowRepository(Path(temporary)),
                demo_safe_mode=True,
            )
            state = asyncio.run(manager.start(user_topic="provider namespaces"))
            request = next(
                message
                for message in state.message_history
                if message.receiver_agent_id == "producer.topic_scout"
            )

            openrouter_provider = _OpenRouterNamespaceProvider()
            openrouter_registry = ProducerRegistry(
                openrouter_provider,
                demo_safe_mode=True,
            )
            response = asyncio.run(
                openrouter_registry.get("producer.topic_scout").execute(request)
            )

            reservations = Path(self._temporary_data_dir.name) / "provider_call_reservations"
            mock_path = (
                reservations
                / "mock"
                / request.workflow_id
                / "producer.topic_scout.json"
            )
            openrouter_path = (
                reservations
                / "openrouter"
                / request.workflow_id
                / "producer.topic_scout.json"
            )
            self.assertEqual(response.message_type, MessageType.RESULT.value)
            self.assertTrue(mock_path.is_file())
            self.assertTrue(openrouter_path.is_file())
            self.assertEqual(
                json.loads(mock_path.read_text(encoding="utf-8"))["provider"],
                "MockModelProvider",
            )
            self.assertEqual(
                json.loads(openrouter_path.read_text(encoding="utf-8"))["provider"],
                "_OpenRouterNamespaceProvider",
            )
            self.assertEqual(mock_provider.calls.count("TopicScoutOutput"), 1)
            self.assertEqual(openrouter_provider.calls.count("TopicScoutOutput"), 1)

    def test_provider_failure_keeps_reservation_and_blocks_second_attempt(self) -> None:
        provider = _FailingMockProvider()
        registry = ProducerRegistry(provider, demo_safe_mode=True)
        request = PMPMessage.create(
            workflow_id="00000000-0000-4000-8000-000000000001",
            sender_agent_id="producer.manager",
            receiver_agent_id="producer.topic_scout",
            message_type=MessageType.TASK,
            objective="verify reservation retention",
            payload={"user_topic": "reservation retention"},
        )
        agent = registry.get("producer.topic_scout")

        first_response = asyncio.run(agent.execute(request))
        second_response = asyncio.run(agent.execute(request))

        reservation_path = (
            Path(self._temporary_data_dir.name)
            / "provider_call_reservations"
            / "mock"
            / request.workflow_id
            / "producer.topic_scout.json"
        )
        self.assertEqual(first_response.message_type, MessageType.ERROR.value)
        self.assertEqual(second_response.message_type, MessageType.ERROR.value)
        self.assertTrue(reservation_path.is_file())
        self.assertEqual(provider.attempts, 1)

    def test_capability_preflight_stops_before_reservation_and_paid_call(self) -> None:
        provider = _CapabilityFailingProvider()
        registry = ProducerRegistry(
            provider,
            {"producer.topic_scout": "unsupported/model"},
            demo_safe_mode=True,
        )
        request = PMPMessage.create(
            workflow_id="00000000-0000-4000-8000-000000000028",
            sender_agent_id="producer.manager",
            receiver_agent_id="producer.topic_scout",
            message_type=MessageType.TASK,
            objective="capability preflight fault",
            payload={"user_topic": "fault"},
        )

        response = asyncio.run(registry.get("producer.topic_scout").execute(request))

        reservation_path = (
            Path(self._temporary_data_dir.name)
            / "provider_call_reservations"
            / "openrouter"
            / request.workflow_id
            / "producer.topic_scout.json"
        )
        self.assertEqual(response.message_type, MessageType.ERROR.value)
        self.assertEqual(response.payload["error_code"], "ProviderCapabilityError")
        self.assertEqual(provider.paid_attempts, 0)
        self.assertFalse(reservation_path.exists())

    def test_deliberation_manager_preflight_failure_leaves_no_invocation_mark(self) -> None:
        provider = _RecoveringCapabilityProvider()
        registry = DeliberationRegistry(
            provider,
            {"deliberation.manager": "strict/model"},
            demo_safe_mode=True,
        )
        workflow_id = "00000000-0000-4000-8000-000000000029"
        arguments = {
            "input_data": {"topic": "fault"},
            "output_schema": InitialIntegratedAnalysis,
            "stage": "initial",
            "workflow_id": workflow_id,
        }

        with self.assertRaises(ProviderCapabilityError):
            asyncio.run(registry.integrate(**arguments))

        reservation_path = (
            Path(self._temporary_data_dir.name)
            / "provider_call_reservations"
            / "openrouter"
            / workflow_id
            / "deliberation_manager_initial.json"
        )
        self.assertFalse(reservation_path.exists())
        self.assertNotIn((workflow_id, "initial"), registry._manager_invocations)

        provider.capability_available = True
        with self.assertRaises(KeyError):
            asyncio.run(registry.integrate(**arguments))
        self.assertTrue(reservation_path.exists())
        self.assertIn((workflow_id, "initial"), registry._manager_invocations)

    def test_researcher_revision_stops_at_human_gate_without_specialist_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                researcher_review_decisions=["revision_required", "approved"]
            )
            manager = ResearcherManager(
                ResearcherRegistry(provider, demo_safe_mode=True),
                ResearcherWorkflowRepository(Path(temporary)),
                demo_safe_mode=True,
            )

            state = asyncio.run(manager.start_from_message(make_handoff()))

            self.assertEqual(state.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
            self.assertEqual(state.revision_count, 0)
            self.assertIsNone(state.error)
            self.assertTrue(
                manager.inspect_human_evidence_gate(
                    state.workflow_id
                ).evidence_sufficiency_findings
            )
            self.assertEqual(
                provider.agent_calls.count("researcher.government_researcher"),
                1,
            )
            self.assertEqual(provider.calls.count("ResearchQualityReviewOutput"), 1)

    def test_deliberation_revision_stops_without_downstream_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required", "approved"]
            )
            manager = make_manager(
                Path(temporary),
                provider,
                demo_safe_mode=True,
            )

            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )

            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 0)
            self.assertIn("Demo Safe Mode", state.error["message"])
            self.assertEqual(
                provider.agent_calls.count("deliberation.argument_analyst"),
                1,
            )
            self.assertEqual(
                provider.agent_calls.count("deliberation.counterargument_analyst"),
                1,
            )
            self.assertEqual(provider.calls.count("DeliberationQualityReviewOutput"), 1)

    def test_deliberation_manager_retryable_error_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = _RetryableIntegrationProvider()
            manager = make_manager(
                Path(temporary),
                provider,
                demo_safe_mode=True,
            )

            state = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )

            self.assertEqual(state.status, "FAILED")
            self.assertEqual(provider.integration_attempts, 1)

    def test_deliberation_quality_failure_can_use_checkpoint_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_schemas={"DeliberationQualityReviewOutput"}
            )
            manager = make_manager(
                Path(temporary),
                provider,
                demo_safe_mode=True,
            )
            failed = asyncio.run(
                manager.start_from_message(make_deliberation_handoff())
            )
            calls_before_recovery = len(provider.calls)

            provider.fail_schemas.clear()
            recovered = asyncio.run(manager.recover(failed.workflow_id))

            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(recovered.status, "COMPLETED")
            self.assertEqual(
                provider.calls[calls_before_recovery:],
                ["DeliberationQualityReviewOutput"],
            )

    def test_conclusion_revision_stops_without_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["revision_required", "approved"]
            )
            manager = ConclusionManager(
                ConclusionRegistry(provider, {}, demo_safe_mode=True),
                ConclusionWorkflowRepository(data_dir),
                demo_safe_mode=True,
            )

            state = asyncio.run(
                manager.start_from_message(make_conclusion_handoff(data_dir, provider))
            )

            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 0)
            self.assertIn("Demo Safe Mode", state.error["message"])
            self.assertEqual(
                provider.agent_calls.count("conclusion.position_generator"),
                1,
            )
            self.assertEqual(
                provider.agent_calls.count("conclusion.quality_reviewer"),
                1,
            )

    def test_playwright_revision_stops_after_one_call_per_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_unsupported_once=True)
            manager = make_playwright_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )

            state = asyncio.run(
                manager.start_from_message(make_playwright_handoff(data_dir, provider))
            )

            calls = [
                agent_id
                for agent_id in provider.agent_calls
                if agent_id.startswith("playwright.")
            ]
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 0)
            self.assertEqual(calls.count("playwright.narrative_architect"), 1)
            self.assertEqual(calls.count("playwright.scriptwriter"), 1)
            self.assertEqual(calls.count("playwright.evidence_citation_editor"), 1)
            self.assertEqual(calls.count("playwright.visual_director"), 1)


if __name__ == "__main__":
    unittest.main()
