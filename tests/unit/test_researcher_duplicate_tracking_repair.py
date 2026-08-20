from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deliberation.manager import DeliberationManager
from deliberation.registry import DeliberationRegistry
from providers.mock_provider import MockModelProvider
from researcher.integrity_repair import (
    DuplicateTrackingRepairError,
    immutable_report_sha256,
)
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from researcher.schemas.human_evidence import (
    HumanActorSource,
    HumanEvidenceDecisionType,
)
from researcher.schemas.research_report import ResearchReport, ResearchReportReview
from storage.deliberation_workflow_repository import DeliberationWorkflowRepository
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from tests.researcher_helpers import make_handoff, valid_source


CANONICAL_SOURCE_ID = "source_mext_guideline_body"
LANDING_SOURCE_ID = "source_mext_guideline_landing"
NOTICE_SOURCE_ID = "source_mext_guideline_notice"
CANONICAL_EVIDENCE_ID = "evidence_mext_guideline_body"
LANDING_EVIDENCE_ID = "evidence_mext_guideline_landing"
NOTICE_EVIDENCE_ID = "evidence_mext_guideline_notice"
GUIDELINE_TITLE = "初等中等教育段階における生成AIの利活用に関するガイドライン"
HARD_FINDING_ID = "finding_mext_guideline_duplicate_tracking"


class ResearcherDuplicateTrackingRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.provider = MockModelProvider(
            reservation_root=self.data_dir / "provider_call_reservations"
        )
        self.repository = ResearcherWorkflowRepository(self.data_dir)
        self.manager = ResearcherManager(
            ResearcherRegistry(self.provider, demo_safe_mode=True),
            self.repository,
            demo_safe_mode=True,
        )

    @staticmethod
    def _government_source(
        *,
        source_id: str,
        evidence_id: str,
        url: str,
        title: str,
        summary: str,
        question_id: str,
        merged_evidence_ids: list[str] | None = None,
        organization: str = "文部科学省",
    ) -> dict:
        return valid_source(
            "GOVERNMENT",
            source_id=source_id,
            evidence_id=evidence_id,
            research_question_ids=[question_id],
            title=title,
            source_name=organization,
            url=url,
            author_or_organization=organization,
            summary=summary,
            relevant_excerpt=f"{GUIDELINE_TITLE} Ver. 2.0",
            primary_source=True,
            source_specific_metadata={
                "organization": organization,
                "country": "Japan",
                "document_type": "Guideline",
                "merged_evidence_ids": merged_evidence_ids or [evidence_id],
            },
        )

    def _install_fixture(
        self,
        *,
        second_version: str | None = None,
        second_organization: str = "文部科学省",
        ambiguous_canonical: bool = False,
        canonical_existing_relations: list[str] | None = None,
    ):
        state = asyncio.run(self.manager.start_from_message(make_handoff()))
        report_data = ResearchReport.model_validate(state.research_report).model_dump(
            mode="json"
        )
        question_ids = [
            item["research_question_id"] for item in report_data["research_questions"]
        ]
        version = second_version or "2.0"
        sources = [
            self._government_source(
                source_id=CANONICAL_SOURCE_ID,
                evidence_id=CANONICAL_EVIDENCE_ID,
                url="https://www.mext.go.jp/content/guideline.pdf",
                title=GUIDELINE_TITLE,
                summary=f"{GUIDELINE_TITLE} Ver. 2.0 本文",
                question_id=question_ids[0],
                merged_evidence_ids=(
                    canonical_existing_relations or [CANONICAL_EVIDENCE_ID]
                ),
            ),
            self._government_source(
                source_id=LANDING_SOURCE_ID,
                evidence_id=LANDING_EVIDENCE_ID,
                url=(
                    "https://www.mext.go.jp/content/guideline-landing.pdf"
                    if ambiguous_canonical
                    else "https://www.mext.go.jp/a_menu/shotou/zyouhou/detail.htm"
                ),
                title=GUIDELINE_TITLE,
                summary=f"{GUIDELINE_TITLE} Ver. {version} 掲載ページ",
                question_id=question_ids[0],
                organization=second_organization,
            ),
            self._government_source(
                source_id=NOTICE_SOURCE_ID,
                evidence_id=NOTICE_EVIDENCE_ID,
                url="https://www.mext.go.jp/content/guideline-notice.pdf",
                title=f"{GUIDELINE_TITLE} Ver. {version} 改訂について",
                summary=f"{GUIDELINE_TITLE} Ver. {version} 改訂通知",
                question_id=question_ids[-1],
                organization=second_organization,
            ),
        ]
        report_data["sources"].extend(sources)
        for source in sources:
            report_data["evidence_items"].append(
                {
                    "evidence_id": source["evidence_id"],
                    "source_id": source["source_id"],
                    "research_question_ids": source["research_question_ids"],
                    "summary": source["summary"],
                    "stance": source["stance"],
                    "directness": source["directness"],
                }
            )
            report_data["source_metadata"].append(
                {
                    key: copy.deepcopy(source[key])
                    for key in (
                        "source_id",
                        "source_type",
                        "title",
                        "source_name",
                        "url",
                        "author_or_organization",
                        "published_at",
                        "retrieved_at",
                        "geographic_scope",
                        "time_scope",
                        "source_specific_metadata",
                    )
                }
            )
            report_data["evidence_quality_assessments"].append(
                {
                    "evidence_id": source["evidence_id"],
                    "source_id": source["source_id"],
                    "reliability": source["reliability"],
                    "directness": source["directness"],
                    "primary_source": source["primary_source"],
                    "limitations": source["limitations"],
                }
            )

        review = {
            "status": "revision_required",
            "reason": "Two evidence gaps and one deterministic relation defect remain",
            "findings": [
                {
                    "finding_id": "finding_rq001_missing_industry",
                    "finding_type": "EVIDENCE_SUFFICIENCY_FINDING",
                    "severity": "MAJOR",
                    "research_question_id": question_ids[0],
                    "target_agent_id": "researcher.industry_researcher",
                    "issue": "Industry evidence is insufficient",
                    "required_action": "Collect one additional industry source",
                },
                {
                    "finding_id": "finding_rq003_missing_news",
                    "finding_type": "EVIDENCE_SUFFICIENCY_FINDING",
                    "severity": "MAJOR",
                    "research_question_id": question_ids[-1],
                    "target_agent_id": "researcher.news_researcher",
                    "issue": "News evidence is insufficient",
                    "required_action": "Collect one additional news source",
                },
                {
                    "finding_id": HARD_FINDING_ID,
                    "finding_type": "HARD_INTEGRITY_FAILURE",
                    "severity": "CRITICAL",
                    "research_question_id": None,
                    "target_agent_id": "researcher.manager",
                    "issue": (
                        "Same-document duplicate tracking is absent for "
                        f"{CANONICAL_SOURCE_ID}, {LANDING_SOURCE_ID}, {NOTICE_SOURCE_ID}; "
                        f"Evidence IDs are {CANONICAL_EVIDENCE_ID}, "
                        f"{LANDING_EVIDENCE_ID}, {NOTICE_EVIDENCE_ID}"
                    ),
                    "required_action": (
                        "Record the same guideline family using merged_evidence_ids "
                        "without deleting any Source"
                    ),
                },
            ],
            "revision_targets": [
                "researcher.industry_researcher",
                "researcher.news_researcher",
            ],
        }
        report_data["review"] = review
        report = ResearchReport.model_validate(report_data)
        state.research_report = report.model_dump(mode="json")
        state.collected_sources = [
            item.model_dump(mode="json") for item in report.sources
        ]
        state.review_result = copy.deepcopy(review)
        state.status = "BLOCKED"
        state.error = {"code": "HARD_INTEGRITY_FAILURE", "message": "duplicate tracking"}
        for message in reversed(state.message_history):
            if message.sender_agent_id == "researcher.quality_reviewer":
                message.payload = copy.deepcopy(review)
                break
        self.repository.save_report(report)
        self.repository.save(state)
        return state, report

    @staticmethod
    def _protected_source_payload(report: ResearchReport) -> dict[str, dict]:
        return {
            source.source_id: {
                key: value
                for key, value in source.model_dump(mode="json").items()
                if key != "source_specific_metadata"
            }
            for source in report.sources
        }

    def test_repair_opens_gate_preserves_report_and_is_exactly_idempotent(self) -> None:
        state, report = self._install_fixture()
        provider_calls = len(self.provider.calls)
        provider_reservations = list(
            (self.data_dir / "provider_call_reservations").rglob("*.json")
        )
        before_protected = self._protected_source_payload(report)
        before_immutable = immutable_report_sha256(report)

        waiting = self.manager.repair_human_evidence_integrity(state.workflow_id)
        repaired = ResearchReport.model_validate(waiting.research_report)
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)
        state_path = self.repository.workflows_dir / f"{state.workflow_id}.json"
        report_path = self.repository.reports_dir / f"{state.workflow_id}.json"
        first_state = state_path.read_bytes()
        first_report = report_path.read_bytes()

        self.assertEqual(waiting.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(waiting.revision_count, 0)
        self.assertEqual(len(repaired.sources), len(report.sources))
        self.assertEqual(self._protected_source_payload(repaired), before_protected)
        self.assertEqual(immutable_report_sha256(repaired), before_immutable)
        self.assertFalse(summary.hard_integrity_findings)
        self.assertEqual(len(summary.evidence_sufficiency_findings), 2)
        self.assertEqual(
            [item.finding_id for item in summary.resolved_integrity_findings],
            [HARD_FINDING_ID],
        )
        canonical = next(
            item for item in repaired.sources if item.source_id == CANONICAL_SOURCE_ID
        )
        self.assertEqual(
            canonical.source_specific_metadata["merged_evidence_ids"],
            [LANDING_EVIDENCE_ID, NOTICE_EVIDENCE_ID],
        )
        self.assertEqual(len(waiting.human_evidence_integrity_repairs), 1)
        repair = waiting.human_evidence_integrity_repairs[0]
        self.assertEqual(repair.repair_kind, "research_source_duplicate_tracking")
        self.assertEqual(repair.provider_calls, 0)
        self.assertEqual(repair.retrieval_calls, 0)
        self.assertEqual(
            repair.immutable_content_sha256_before,
            repair.immutable_content_sha256_after,
        )
        self.assertEqual(
            len(self.repository.list_human_evidence_integrity_repairs(state.workflow_id)),
            1,
        )
        self.assertEqual(len(self.provider.calls), provider_calls)
        self.assertEqual(
            list((self.data_dir / "provider_call_reservations").rglob("*.json")),
            provider_reservations,
        )
        self.assertFalse((self.data_dir / "retrieval_reservations").exists())

        repeated = self.manager.repair_human_evidence_integrity(state.workflow_id)
        self.assertEqual(repeated.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(state_path.read_bytes(), first_state)
        self.assertEqual(report_path.read_bytes(), first_report)
        self.assertEqual(
            len(self.repository.list_human_evidence_integrity_repairs(state.workflow_id)),
            1,
        )

    def test_generic_recover_does_not_implicitly_apply_new_relation_repair(self) -> None:
        state, report = self._install_fixture()
        before = report.model_dump(mode="json")
        provider_calls = len(self.provider.calls)

        blocked = self.manager.recover_human_evidence_gate(state.workflow_id)

        self.assertEqual(blocked.status, "BLOCKED")
        self.assertFalse(blocked.human_evidence_integrity_repairs)
        self.assertEqual(
            self.repository.load_report(state.workflow_id).model_dump(mode="json"),
            before,
        )
        self.assertEqual(len(self.provider.calls), provider_calls)

    def test_different_version_fails_closed_without_persistence(self) -> None:
        state, report = self._install_fixture(second_version="3.0")
        before_state = self.repository.load(state.workflow_id).model_dump(mode="json")
        before_report = report.model_dump(mode="json")

        with self.assertRaisesRegex(
            DuplicateTrackingRepairError, "VERSION_CONFLICT"
        ):
            self.manager.repair_human_evidence_integrity(state.workflow_id)

        self.assertEqual(
            self.repository.load(state.workflow_id).model_dump(mode="json"), before_state
        )
        self.assertEqual(
            self.repository.load_report(state.workflow_id).model_dump(mode="json"),
            before_report,
        )

    def test_different_publisher_and_ambiguous_canonical_fail_closed(self) -> None:
        for kwargs, expected in (
            ({"second_organization": "別機関"}, "issuing organization"),
            ({"ambiguous_canonical": True}, "AMBIGUOUS_CANONICAL"),
        ):
            with self.subTest(kwargs=kwargs):
                state, report = self._install_fixture(**kwargs)
                before = report.model_dump(mode="json")
                with self.assertRaisesRegex(DuplicateTrackingRepairError, expected):
                    self.manager.repair_human_evidence_integrity(state.workflow_id)
                self.assertEqual(
                    self.repository.load_report(state.workflow_id).model_dump(mode="json"),
                    before,
                )

    def test_conflicting_relation_fails_closed(self) -> None:
        state, report = self._install_fixture(
            canonical_existing_relations=["evidence_unrelated_family"]
        )
        before = report.model_dump(mode="json")
        with self.assertRaisesRegex(DuplicateTrackingRepairError, "CONFLICT"):
            self.manager.repair_human_evidence_integrity(state.workflow_id)
        self.assertEqual(
            self.repository.load_report(state.workflow_id).model_dump(mode="json"),
            before,
        )

    def test_existing_correct_relation_gets_audit_artifact_without_report_change(self) -> None:
        state, report = self._install_fixture(
            canonical_existing_relations=[LANDING_EVIDENCE_ID, NOTICE_EVIDENCE_ID]
        )
        before = report.model_dump(mode="json")
        waiting = self.manager.repair_human_evidence_integrity(state.workflow_id)

        self.assertEqual(waiting.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(
            self.repository.load_report(state.workflow_id).model_dump(mode="json"),
            before,
        )
        repair = waiting.human_evidence_integrity_repairs[0]
        self.assertEqual(repair.report_sha256_before, repair.report_sha256_after)
        self.assertEqual(
            repair.relation_metadata_sha256_before,
            repair.relation_metadata_sha256_after,
        )

    def test_artifact_write_fault_is_recoverable_without_provider_replay(self) -> None:
        state, _report = self._install_fixture()
        provider_calls = len(self.provider.calls)
        with patch.object(
            self.repository,
            "create_human_evidence_integrity_repair_once",
            side_effect=RuntimeError("injected artifact write failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.manager.repair_human_evidence_integrity(state.workflow_id)

        interrupted = self.repository.load(state.workflow_id)
        self.assertEqual(interrupted.status, "BLOCKED")
        self.assertEqual(len(interrupted.human_evidence_integrity_repairs), 1)
        artifact_path = (
            self.repository.human_evidence_integrity_repairs_dir
            / state.workflow_id
            / f"{interrupted.human_evidence_integrity_repairs[0].repair_id}.json"
        )
        self.assertFalse(artifact_path.exists())

        recovered = self.manager.repair_human_evidence_integrity(state.workflow_id)
        self.assertEqual(recovered.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertTrue(artifact_path.exists())
        self.assertEqual(len(self.provider.calls), provider_calls)

    def test_accept_limitations_persists_and_deliberation_consumes_new_contract(self) -> None:
        state, _report = self._install_fixture()
        self.manager.repair_human_evidence_integrity(state.workflow_id)
        completed = self.manager.decide_human_evidence(
            state.workflow_id,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS,
            reason="Two disclosed evidence gaps are explicitly accepted in this test",
            actor_source=HumanActorSource.MOCK_FIXTURE,
        )

        self.assertEqual(completed.status, "COMPLETED")
        self.assertEqual(len(completed.accepted_evidence_gaps), 2)
        handoff = self.repository.load_deliberation_outbox(state.workflow_id)
        deliberation = DeliberationManager(
            DeliberationRegistry(self.provider, demo_safe_mode=True),
            DeliberationWorkflowRepository(self.data_dir),
            demo_safe_mode=True,
        )
        report = deliberation._validate_researcher_handoff(handoff)
        context = deliberation._research_context_from_state(
            type(
                "StateView",
                (),
                {"researcher_handoff": handoff.model_dump(mode="json")},
            )(),
            report,
        )
        self.assertEqual(
            [item.repair_kind for item in context.human_evidence_integrity_repairs],
            ["research_source_duplicate_tracking"],
        )

    def test_revise_after_repair_creates_plan_without_consuming_revision_budget(self) -> None:
        state, _report = self._install_fixture()
        self.manager.repair_human_evidence_integrity(state.workflow_id)
        provider_calls = len(self.provider.calls)

        revised = self.manager.decide_human_evidence(
            state.workflow_id,
            HumanEvidenceDecisionType.REVISE,
            reason="Collect the two still-missing evidence categories",
            actor_source=HumanActorSource.MOCK_FIXTURE,
        )

        self.assertEqual(revised.revision_count, 0)
        self.assertIsNotNone(revised.evidence_revision_plan)
        self.assertEqual(len(revised.evidence_revision_plan.finding_ids), 2)
        self.assertEqual(len(self.provider.calls), provider_calls)


if __name__ == "__main__":
    unittest.main()
