from __future__ import annotations

import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from playwright.deterministic_repair import DeterministicRepairIneligible
from playwright.schemas import CitationEditingResult, ScriptDraft
from playwright.validator import canonical_hash
from providers.mock_provider import MockModelProvider
from tests.playwright_helpers import make_playwright_handoff, make_playwright_manager


class MissingMappingAndStaleUnsupportedProvider(MockModelProvider):
    """Reproduce Cycle 047 without adding new evidence or Provider behavior."""

    def __init__(self) -> None:
        super().__init__()
        self.omitted = False

    async def generate_structured(self, **kwargs):
        result = await super().generate_structured(**kwargs)
        if kwargs["output_schema"] is ScriptDraft:
            context = kwargs["input_data"]["production_context"]
            claim_id = context["must_include_claim_ids"][0]
            general = next(
                section
                for section in result["sections"]
                if section["section_type"] == "GENERAL_OPINION"
            )["paragraphs"][0]
            general["claim_ids"] = [claim_id]
            general["evidence_ids"] = []
            general["citation_required"] = False
        elif kwargs["output_schema"] is CitationEditingResult and not self.omitted:
            manifest = result["citation_manifest"]
            evidence_mappings = [
                mapping for mapping in manifest["mappings"] if mapping["evidence_ids"]
            ]
            if len(evidence_mappings) < 2:
                raise AssertionError("fixture requires a donor citation mapping")
            omitted = evidence_mappings[1]
            manifest["mappings"] = [
                mapping
                for mapping in manifest["mappings"]
                if mapping["citation_mapping_id"]
                != omitted["citation_mapping_id"]
            ]
            general = next(
                paragraph
                for section in result["citation_validated_script"]["sections"]
                for paragraph in section["paragraphs"]
                if paragraph["rhetorical_function"] == "general_opinion"
            )
            issue = {
                "paragraph_id": general["paragraph_id"],
                "reason": "stale paragraph-local unsupported classification",
                "claim_ids": list(general["claim_ids"]),
                "evidence_ids": [],
                "source_ids": [],
            }
            manifest["unsupported_claims"] = [issue]
            result["citation_validated_script"][
                "unresolved_citation_issues"
            ] = [issue]
            self.omitted = True
        return result


class PlaywrightManifestContractRepairTests(unittest.TestCase):
    def _blocked_fixture(self, data_dir: Path):
        provider = MissingMappingAndStaleUnsupportedProvider()
        manager = make_playwright_manager(
            data_dir,
            provider,
            max_revisions=2,
            demo_safe_mode=True,
        )
        state = asyncio.run(
            manager.start_from_message(make_playwright_handoff(data_dir, provider))
        )
        self.assertEqual("BLOCKED", state.status)
        self.assertEqual(
            ["CITATION_MAPPING_MISSING", "UNSUPPORTED_CLAIM_LIST_NOT_EMPTY"],
            [
                finding["code"]
                for finding in state.deterministic_validation["findings"]
                if finding["severity"] == "ERROR"
            ],
        )
        state.revision_count = 2
        manager.repository.save(state)
        return manager, provider, state

    def test_combined_contract_repair_completes_without_provider_or_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, blocked = self._blocked_fixture(Path(temporary))
            provider_calls = list(provider.calls)
            agent_calls = list(provider.agent_calls)
            protected_fields = (
                "final_conclusion",
                "production_context",
                "narrative_blueprint",
                "script_draft",
                "visual_plan",
            )
            protected = {
                field: canonical_hash(getattr(blocked, field))
                for field in protected_fields
            }

            completed = asyncio.run(manager.recover(blocked.workflow_id))

            self.assertEqual("COMPLETED", completed.status)
            self.assertTrue(completed.delivered)
            self.assertEqual(2, completed.revision_count)
            self.assertEqual(1, completed.deterministic_repair_count)
            self.assertEqual(provider_calls, provider.calls)
            self.assertEqual(agent_calls, provider.agent_calls)
            self.assertEqual([], completed.final_gate_result["blocking_finding_ids"])
            self.assertEqual([], completed.citation_manifest["unsupported_claims"])
            self.assertEqual(
                [],
                completed.citation_validated_script["unresolved_citation_issues"],
            )

            script_claim_ids = {
                claim_id
                for section in completed.script_draft["sections"]
                for paragraph in section["paragraphs"]
                for claim_id in paragraph["claim_ids"]
            }
            self.assertEqual(
                script_claim_ids,
                set(completed.citation_manifest["supported_claim_ids"]),
            )
            mappings_by_paragraph = {
                mapping["paragraph_id"]: mapping
                for mapping in completed.citation_manifest["mappings"]
            }
            for section in completed.script_draft["sections"]:
                for paragraph in section["paragraphs"]:
                    if paragraph["citation_required"]:
                        self.assertIn(paragraph["paragraph_id"], mappings_by_paragraph)

            record = completed.deterministic_repair_history[0]
            self.assertEqual(
                "CITATION_MANIFEST_CONTRACT_RECONSTRUCTION",
                record.repair_type,
            )
            self.assertEqual(0, record.provider_calls)
            self.assertEqual(0, record.retrieval_calls)
            self.assertEqual(len(script_claim_ids), record.script_claim_count)
            self.assertEqual(
                len(script_claim_ids),
                record.manifest_claim_count_before,
            )
            self.assertEqual(len(script_claim_ids), record.manifest_claim_count_after)
            self.assertEqual(1, record.unsupported_claim_count_before)
            self.assertEqual(0, record.unsupported_claim_count_after)
            self.assertEqual(1, len(record.missing_mapping_ids))
            self.assertEqual(1, len(record.cleaned_unsupported_paragraph_ids))
            self.assertEqual(1, len(record.repaired_mapping_ids))
            self.assertEqual(
                protected,
                {
                    field: canonical_hash(getattr(completed, field))
                    for field in protected_fields
                },
            )
            self.assertEqual(6, len(completed.delivery_paths))

            state_path = manager.repository.workflows_dir / f"{blocked.workflow_id}.json"
            state_hash = canonical_hash(
                manager.repository.read_json(state_path)
            )
            repeated = asyncio.run(manager.recover(blocked.workflow_id))
            self.assertEqual("COMPLETED", repeated.status)
            self.assertEqual(
                state_hash,
                canonical_hash(manager.repository.read_json(state_path)),
            )

    def test_cleanup_fails_closed_when_issue_claim_is_not_saved_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, blocked = self._blocked_fixture(Path(temporary))
            manifest_before = deepcopy(blocked.citation_manifest)
            blocked.citation_manifest["unsupported_claims"][0]["claim_ids"] = [
                "claim_not_in_script"
            ]
            tampered_before = deepcopy(blocked.citation_manifest)
            manager.repository.save(blocked)
            provider_calls = list(provider.calls)

            with self.assertRaisesRegex(
                DeterministicRepairIneligible,
                "not globally supported",
            ):
                asyncio.run(manager.recover(blocked.workflow_id))

            reloaded = manager.repository.load(blocked.workflow_id)
            self.assertNotEqual(manifest_before, reloaded.citation_manifest)
            self.assertEqual(tampered_before, reloaded.citation_manifest)
            self.assertEqual(0, reloaded.deterministic_repair_count)
            self.assertEqual(provider_calls, provider.calls)

    def test_fault_after_combined_repair_resumes_without_second_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, blocked = self._blocked_fixture(Path(temporary))
            provider_calls = list(provider.calls)

            async def interrupt_before_final_gate(*args, **kwargs):
                raise RuntimeError("injected stop after Cycle 047 checkpoint")

            with patch.object(
                manager,
                "_run",
                side_effect=interrupt_before_final_gate,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected stop"):
                    asyncio.run(manager.recover(blocked.workflow_id))

            checkpoint = manager.repository.load(blocked.workflow_id)
            self.assertEqual("VALIDATING_PACKAGE", checkpoint.status)
            self.assertEqual(1, checkpoint.deterministic_repair_count)
            self.assertEqual([], checkpoint.citation_manifest["unsupported_claims"])
            self.assertEqual(
                [],
                checkpoint.citation_validated_script[
                    "unresolved_citation_issues"
                ],
            )
            self.assertEqual(provider_calls, provider.calls)

            completed = asyncio.run(manager.recover(blocked.workflow_id))
            self.assertEqual("COMPLETED", completed.status)
            self.assertEqual(1, completed.deterministic_repair_count)
            self.assertEqual(1, len(completed.deterministic_repair_history))
            self.assertEqual(provider_calls, provider.calls)


if __name__ == "__main__":
    unittest.main()
