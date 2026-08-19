from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from cli_app.output import format_state_summary, next_action_for
from playwright.deterministic_repair import (
    DeterministicRepairIneligible,
    PlaywrightDeterministicRepairer,
)
from playwright.schemas import (
    CitationEditingResult,
    PlaywrightRepairDisposition,
    ValidationFinding,
)
from playwright.validator import canonical_hash
from providers.mock_provider import MockModelProvider
from tests.playwright_helpers import make_playwright_handoff, make_playwright_manager


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MissingNonChartCitationProvider(MockModelProvider):
    """Omit one later evidence mapping without invalidating the first chart source."""

    def __init__(self) -> None:
        super().__init__()
        self.omitted = False

    async def generate_structured(self, **kwargs):
        result = await super().generate_structured(**kwargs)
        if kwargs["output_schema"] is CitationEditingResult and not self.omitted:
            evidence_mappings = [
                mapping
                for mapping in result["citation_manifest"]["mappings"]
                if mapping["evidence_ids"]
            ]
            if len(evidence_mappings) < 2:
                raise AssertionError("fixture needs a non-chart evidence mapping")
            omitted_id = evidence_mappings[1]["citation_mapping_id"]
            result["citation_manifest"]["mappings"] = [
                mapping
                for mapping in result["citation_manifest"]["mappings"]
                if mapping["citation_mapping_id"] != omitted_id
            ]
            self.omitted = True
        return result


class PlaywrightDeterministicRepairTests(unittest.TestCase):
    def _blocked_fixture(self, data_dir: Path):
        provider = MissingNonChartCitationProvider()
        manager = make_playwright_manager(
            data_dir,
            provider,
            max_revisions=2,
            demo_safe_mode=True,
        )
        state = asyncio.run(
            manager.start_from_message(make_playwright_handoff(data_dir, provider))
        )
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(
            [
                finding["code"]
                for finding in state.deterministic_validation["findings"]
                if finding["severity"] == "ERROR"
            ],
            ["CITATION_MAPPING_MISSING"],
        )
        state.revision_count = 2
        manager.repository.save(state)
        return manager, provider, state

    @staticmethod
    def _missing_paragraph_id(state) -> str:
        finding = next(
            item
            for item in state.deterministic_validation["findings"]
            if item["code"] == "CITATION_MAPPING_MISSING"
        )
        return finding["details"]["paragraph_id"]

    def test_revision_exhaustion_allows_zero_provider_local_repair_and_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            paragraph_id = self._missing_paragraph_id(blocked)
            protected = {
                name: canonical_hash(getattr(blocked, name))
                for name in (
                    "final_conclusion",
                    "production_context",
                    "narrative_blueprint",
                    "script_draft",
                    "citation_validated_script",
                    "visual_plan",
                )
            }
            provider_calls = list(provider.calls)
            agent_calls = list(provider.agent_calls)

            completed = asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(completed.revision_count, 2)
            self.assertEqual(completed.deterministic_repair_count, 1)
            self.assertEqual(provider.calls, provider_calls)
            self.assertEqual(provider.agent_calls, agent_calls)
            self.assertEqual(completed.final_gate_result["blocking_finding_ids"], [])
            self.assertIn(
                completed.final_gate_result["status"],
                {"APPROVED", "APPROVED_WITH_LIMITATIONS"},
            )
            repaired = [
                mapping
                for mapping in completed.citation_manifest["mappings"]
                if mapping["paragraph_id"] == paragraph_id
            ]
            self.assertEqual(len(repaired), 1)
            self.assertTrue(
                repaired[0]["citation_mapping_id"].startswith(
                    "citation_mapping_repair_"
                )
            )
            record = completed.deterministic_repair_history[0]
            self.assertEqual(record.provider_calls, 0)
            self.assertEqual(record.retrieval_calls, 0)
            self.assertEqual(record.paragraph_ids, [paragraph_id])
            self.assertEqual(
                record.citation_manifest_hash_after,
                canonical_hash(completed.citation_manifest),
            )
            self.assertEqual(
                {
                    name: canonical_hash(getattr(completed, name))
                    for name in protected
                },
                protected,
            )
            self.assertEqual(len(completed.delivery_paths), 6)
            self.assertTrue(
                all(Path(path).exists() for path in completed.delivery_paths.values())
            )
            audit_files = list(
                (
                    manager.repository.deterministic_repair_dir
                    / completed.workflow_id
                ).glob("*.json")
            )
            self.assertEqual(len(audit_files), 1)

    def test_recover_after_completed_repair_is_exactly_once_no_op(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            completed = asyncio.run(manager.recover(blocked.workflow_id))
            state_path = (
                manager.repository.workflows_dir / f"{completed.workflow_id}.json"
            )
            hash_before = file_hash(state_path)
            calls_before = list(provider.calls)
            messages_before = len(completed.message_history)
            delivery_before = dict(completed.delivery_paths)

            repeated = asyncio.run(manager.recover(completed.workflow_id))

            self.assertEqual(repeated.status, "COMPLETED")
            self.assertEqual(repeated.deterministic_repair_count, 1)
            self.assertEqual(len(repeated.deterministic_repair_history), 1)
            self.assertEqual(len(repeated.message_history), messages_before)
            self.assertEqual(repeated.delivery_paths, delivery_before)
            self.assertEqual(provider.calls, calls_before)
            self.assertEqual(file_hash(state_path), hash_before)

    def test_fault_after_repair_checkpoint_resumes_without_second_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            calls_before = list(provider.calls)

            async def interrupt_before_final_gate(*args, **kwargs):
                raise RuntimeError("injected process stop after repair checkpoint")

            with patch.object(
                manager,
                "_run",
                side_effect=interrupt_before_final_gate,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected process stop"):
                    asyncio.run(manager.recover(blocked.workflow_id))

            checkpoint = manager.repository.load(blocked.workflow_id)
            self.assertEqual(checkpoint.status, "VALIDATING_PACKAGE")
            self.assertEqual(checkpoint.deterministic_repair_count, 1)
            self.assertEqual(len(checkpoint.deterministic_repair_history), 1)
            self.assertEqual(provider.calls, calls_before)

            completed = asyncio.run(manager.recover(blocked.workflow_id))
            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(completed.deterministic_repair_count, 1)
            self.assertEqual(len(completed.deterministic_repair_history), 1)
            self.assertEqual(provider.calls, calls_before)

    def test_mapping_conflict_fails_closed_without_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            paragraph_id = self._missing_paragraph_id(blocked)
            conflict = deepcopy(blocked.citation_manifest["mappings"][0])
            conflict["citation_mapping_id"] = "conflicting_mapping"
            conflict["paragraph_id"] = paragraph_id
            blocked.citation_manifest["mappings"].append(conflict)
            manager.repository.save(blocked)
            calls_before = list(provider.calls)

            with self.assertRaisesRegex(
                DeterministicRepairIneligible,
                "CITATION_MAPPING_CONFLICT",
            ):
                asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(provider.calls, calls_before)
            self.assertEqual(
                manager.repository.load(blocked.workflow_id).status,
                "BLOCKED",
            )

    def test_missing_canonical_evidence_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            finding = next(
                item
                for item in blocked.deterministic_validation["findings"]
                if item["code"] == "CITATION_MAPPING_MISSING"
            )
            evidence_id = finding["details"]["evidence_ids"][0]
            blocked.production_context["source_manifest"] = [
                item
                for item in blocked.production_context["source_manifest"]
                if item.get("evidence_id") != evidence_id
            ]
            manager.repository.save(blocked)
            calls_before = list(provider.calls)

            with self.assertRaisesRegex(
                DeterministicRepairIneligible,
                "CITATION_LOCATOR_MISSING",
            ):
                asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(provider.calls, calls_before)

    def test_missing_manifest_locator_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            evidence_id = next(
                item["details"]["evidence_ids"][0]
                for item in blocked.deterministic_validation["findings"]
                if item["code"] == "CITATION_MAPPING_MISSING"
            )
            blocked.citation_manifest["source_list"] = [
                item
                for item in blocked.citation_manifest["source_list"]
                if item.get("evidence_id") != evidence_id
            ]
            manager.repository.save(blocked)
            calls_before = list(provider.calls)

            with self.assertRaisesRegex(
                DeterministicRepairIneligible,
                "CITATION_LOCATOR_MISSING",
            ):
                asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(provider.calls, calls_before)

    def test_accepted_unresolved_gap_cannot_become_citation_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            evidence_id = next(
                item["details"]["evidence_ids"][0]
                for item in blocked.deterministic_validation["findings"]
                if item["code"] == "CITATION_MAPPING_MISSING"
            )
            blocked.production_context["accepted_evidence_gaps"] = [
                {
                    "finding_id": evidence_id,
                    "quality_review_id": "review_1",
                    "human_decision_id": "decision_1",
                    "research_question_id": "rq_1",
                    "issue": "accepted gap",
                    "required_action": "future research",
                    "status": "accepted_unresolved",
                    "factual_support_confirmed": False,
                }
            ]
            manager.repository.save(blocked)
            calls_before = list(provider.calls)

            with self.assertRaisesRegex(
                DeterministicRepairIneligible,
                "Accepted unresolved gap",
            ):
                asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(provider.calls, calls_before)

    def test_nonrepairable_error_prevents_partial_local_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            extra = {
                "finding_id": "nonrepairable_finding",
                "code": "VISUAL_UNKNOWN_SOURCE",
                "severity": "ERROR",
                "message": "injected non-repairable finding",
                "target_agent_id": "playwright.visual_director",
                "upstream_required": False,
                "details": {},
            }
            blocked.deterministic_validation["findings"].append(extra)
            blocked.deterministic_validation["checked_counts"]["errors"] = 2
            blocked.final_gate_result["findings"].append(extra)
            blocked.final_gate_result["blocking_finding_ids"].append(
                extra["finding_id"]
            )
            manifest_before = deepcopy(blocked.citation_manifest)
            manager.repository.save(blocked)
            calls_before = list(provider.calls)

            with self.assertRaisesRegex(
                DeterministicRepairIneligible,
                "Non-repairable blocking findings remain",
            ):
                asyncio.run(manager.recover(blocked.workflow_id))

            reloaded = manager.repository.load(blocked.workflow_id)
            self.assertEqual(reloaded.citation_manifest, manifest_before)
            self.assertEqual(reloaded.deterministic_repair_count, 0)
            self.assertEqual(provider.calls, calls_before)

    def test_repeated_repair_finding_is_stopped_by_separate_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            manager, provider, blocked = self._blocked_fixture(data_dir)
            blocked.deterministic_repair_count = 1
            manager.repository.save(blocked)
            calls_before = list(provider.calls)

            with self.assertRaisesRegex(
                DeterministicRepairIneligible,
                "Deterministic repair budget is exhausted",
            ):
                asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual(provider.calls, calls_before)
            self.assertEqual(blocked.revision_count, 2)

    def test_finding_classification_keeps_revision_routes_separate(self):
        repairer = PlaywrightDeterministicRepairer()
        repairable = ValidationFinding(
            finding_id="f1",
            code="CITATION_MAPPING_MISSING",
            severity="ERROR",
            message="missing",
            target_agent_id="playwright.evidence_citation_editor",
        )
        agent = repairable.model_copy(update={"code": "SCRIPT_SECTION_MISMATCH"})
        upstream = repairable.model_copy(
            update={"code": "FINAL_CONCLUSION_CHANGED", "upstream_required": True}
        )
        terminal = repairable.model_copy(
            update={"code": "UNKNOWN", "target_agent_id": None}
        )

        self.assertEqual(
            repairer.classify(repairable),
            PlaywrightRepairDisposition.DETERMINISTIC_REPAIRABLE,
        )
        self.assertEqual(
            repairer.classify(agent),
            PlaywrightRepairDisposition.AGENT_REVISION_REQUIRED,
        )
        self.assertEqual(
            repairer.classify(upstream),
            PlaywrightRepairDisposition.UPSTREAM_REVISION_REQUIRED,
        )
        self.assertEqual(
            repairer.classify(terminal),
            PlaywrightRepairDisposition.NON_REPAIRABLE,
        )

    def test_cli_routes_allowlisted_block_to_recover_not_third_revision(self):
        data = {
            "workflow_id": "workflow-1",
            "status": "BLOCKED",
            "completed_agents": ["a", "b", "c", "d"],
            "revision_count": 2,
            "deterministic_repair_count": 0,
            "final_gate_result": {
                "status": "BLOCKED",
                "blocking_finding_ids": ["finding_1"],
            },
            "deterministic_validation": {
                "findings": [
                    {
                        "finding_id": "finding_1",
                        "code": "CITATION_MAPPING_MISSING",
                        "severity": "ERROR",
                        "target_agent_id": "playwright.evidence_citation_editor",
                        "upstream_required": False,
                    }
                ]
            },
        }
        self.assertEqual(
            next_action_for("playwright", data),
            "py main.py --playwright-recover workflow-1 --safe-mode",
        )
        data["deterministic_repair_count"] = 1
        summary = format_state_summary("playwright", data)
        self.assertIn("deterministic repairs: 1", summary)
        self.assertIn("--status workflow-1", summary)


if __name__ == "__main__":
    unittest.main()
