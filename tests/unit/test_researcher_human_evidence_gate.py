import asyncio
import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from deliberation.manager import DeliberationManager
from deliberation.registry import DeliberationRegistry
from providers.mock_provider import MockModelProvider
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from researcher.schemas.human_evidence import (
    HumanActorSource,
    HumanEvidenceDecisionType,
    validate_human_evidence_integrity_repair,
)
from researcher.schemas.research_report import ResearchReport, ResearchReportReview
from researcher.schemas.source import ResearchSource
from storage.deliberation_workflow_repository import DeliberationWorkflowRepository
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from tests.researcher_helpers import make_handoff


class HumanEvidenceGateTests(unittest.TestCase):
    def setUp(self):
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

    def start_waiting(self):
        state = asyncio.run(self.manager.start_from_message(make_handoff()))
        self.assertEqual(state.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertFalse(state.deliberation_sent)
        self.assertFalse(
            (self.repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )
        return state

    def decide_for_summary(self, workflow_id):
        summary = self.manager.inspect_human_evidence_gate(workflow_id)
        decision = (
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
            if summary.evidence_sufficiency_findings
            else HumanEvidenceDecisionType.ACCEPT
        )
        return self.manager.decide_human_evidence(
            workflow_id,
            decision,
            reason="Explicit Human Evidence Gate unit-test decision",
            actor_source=HumanActorSource.MOCK_FIXTURE,
        )

    def install_review(self, state, review):
        state.review_result = copy.deepcopy(review)
        report = ResearchReport.model_validate(state.research_report)
        report.review = ResearchReportReview.model_validate(copy.deepcopy(review))
        state.research_report = report.model_dump(mode="json")
        for message in reversed(state.message_history):
            if message.sender_agent_id == "researcher.quality_reviewer":
                message.payload = copy.deepcopy(review)
                break
        else:
            self.fail("Quality Review PMP message was not found")
        self.repository.save_report(report)
        self.repository.save(state)
        return state

    @staticmethod
    def evidence_gap_review():
        return {
            "status": "revision_required",
            "reason": "More government evidence is required",
            "findings": [
                {
                    "finding_id": "qf_gap_001",
                    "finding_type": "EVIDENCE_SUFFICIENCY_FINDING",
                    "severity": "MAJOR",
                    "research_question_id": "rq_employment",
                    "target_agent_id": "researcher.government_researcher",
                    "issue": "Government evidence coverage is insufficient",
                    "required_action": "Collect one additional government source",
                }
            ],
            "revision_targets": ["researcher.government_researcher"],
        }

    @staticmethod
    def recognized_media_source(source, research_question_id=None):
        data = source.model_dump(mode="json")
        if research_question_id is not None:
            data["research_question_ids"] = [research_question_id]
        data.update(
            {
                "title": "AIを利用するほど仕事喪失の不安、でも雇用創出に期待",
                "source_name": "xtech.nikkei.com",
                "url": "https://xtech.nikkei.com/atcl/nxt/column/18/00001/10814/",
                "author_or_organization": "xtech.nikkei.com",
                "summary": "JILPT調査結果を紹介する保存済み報道記事。",
                "relevant_excerpt": "JILPTは結果を2025年5月に公表した。",
                "source_specific_metadata": {
                    "expert_name": None,
                    "field": "労働実態調査・技術動向報道",
                    "affiliation": None,
                    "statement_context": "日経クロステックによるJILPT調査結果の報道紹介",
                },
            }
        )
        return ResearchSource.model_validate(data)

    def install_report_integrity_fixture(
        self,
        state,
        *,
        media_answers_news_question,
        include_gap=True,
        include_unresolved_hard=False,
    ):
        report = ResearchReport.model_validate(state.research_report)
        report_data = report.model_dump(mode="json")
        news_question = next(
            item
            for item in report_data["research_questions"]
            if "NEWS" in item["required_categories"]
        )["research_question_id"]
        other_question = next(
            item["research_question_id"]
            for item in report_data["research_questions"]
            if item["research_question_id"] != news_question
        )

        # Make NEWS genuinely absent for the target question before the repair.
        for raw_source in report_data["sources"]:
            if raw_source["source_type"] == "NEWS":
                raw_source["research_question_ids"] = [other_question]
                evidence = next(
                    item
                    for item in report_data["evidence_items"]
                    if item["evidence_id"] == raw_source["evidence_id"]
                )
                evidence["research_question_ids"] = [other_question]

        expert_data = next(
            item for item in report_data["sources"] if item["source_type"] == "EXPERT"
        )
        expert = ResearchSource.model_validate(expert_data)
        media_question = news_question if media_answers_news_question else other_question
        media = self.recognized_media_source(expert, media_question)
        expert_data.clear()
        expert_data.update(media.model_dump(mode="json"))
        evidence = next(
            item
            for item in report_data["evidence_items"]
            if item["evidence_id"] == media.evidence_id
        )
        evidence["research_question_ids"] = [media_question]
        metadata = next(
            item
            for item in report_data["source_metadata"]
            if item["source_id"] == media.source_id
        )
        metadata.update(
            {
                "source_type": "EXPERT",
                "title": media.title,
                "source_name": media.source_name,
                "url": str(media.url),
                "author_or_organization": media.author_or_organization,
                "published_at": media.published_at,
                "retrieved_at": media.retrieved_at,
                "geographic_scope": media.geographic_scope,
                "time_scope": media.time_scope,
                "source_specific_metadata": dict(media.source_specific_metadata),
            }
        )

        source_limitation = "EXACT SOURCE LIMITATION"
        expert_data["limitations"] = list(
            dict.fromkeys(expert_data["limitations"] + [source_limitation])
        )
        report_data["research_limitations"].extend(
            [source_limitation, source_limitation]
        )
        self.manager._recompute_report_coverage_data(state, report_data)
        report = ResearchReport.model_validate(report_data)
        state.research_report = report.model_dump(mode="json")
        state.collected_sources = [
            item.model_dump(mode="json") for item in report.sources
        ]
        self.repository.save_report(report)
        self.repository.save(state)

        findings = [
            {
                "finding_id": "fqr_limitations_duplication",
                "finding_type": "HARD_INTEGRITY_FAILURE",
                "severity": "MAJOR",
                "research_question_id": None,
                "target_agent_id": "researcher.manager",
                "issue": "research_limitations contains exact source-level duplicates",
                "required_action": "Deduplicate research_limitations exactly",
            },
            {
                "finding_id": "fqr_source_classification_expert_news",
                "finding_type": "HARD_INTEGRITY_FAILURE",
                "severity": "MAJOR",
                "research_question_id": media_question,
                "target_agent_id": "researcher.manager",
                "issue": (
                    f"{media.source_id} is EXPERT without identity but its saved "
                    "xtech.nikkei.com context is reporting"
                ),
                "required_action": "Repair the canonical source_type classification",
            },
        ]
        if include_gap:
            findings.append(
                {
                    "finding_id": "fqr_news_missing",
                    "finding_type": "EVIDENCE_SUFFICIENCY_FINDING",
                    "severity": "MAJOR",
                    "research_question_id": news_question,
                    "target_agent_id": "researcher.news_researcher",
                    "issue": "NEWS evidence is missing for the target question",
                    "required_action": "Collect NEWS evidence if it remains missing",
                }
            )
        if include_unresolved_hard:
            findings.append(
                {
                    "finding_id": "fqr_unknown_hard",
                    "finding_type": "HARD_INTEGRITY_FAILURE",
                    "severity": "CRITICAL",
                    "research_question_id": None,
                    "target_agent_id": "researcher.manager",
                    "issue": "An unrelated traceability corruption remains",
                    "required_action": "Repair using a separately proven deterministic rule",
                }
            )
        review = {
            "status": "revision_required",
            "reason": "Saved report requires deterministic integrity repair",
            "findings": findings,
            "revision_targets": ["researcher.news_researcher"],
        }
        self.install_review(state, review)
        return state, report, media, news_question, review

    def test_fault_01_limitation_exact_duplication_preserves_first_order(self):
        self.assertEqual(
            self.manager._dedupe_report_limitations(["A", "B", "A"], []),
            ["A", "B"],
        )

    def test_fault_02_non_identical_limitations_are_not_semantically_deduped(self):
        limitations = ["長期効果は未検証", "長期的な有効性には不確実性がある"]
        self.assertEqual(
            self.manager._dedupe_report_limitations(limitations, []),
            limitations,
        )

    def test_fault_03_expert_without_identity_is_detected(self):
        state = self.start_waiting()
        report = ResearchReport.model_validate(state.research_report)
        expert = next(item for item in report.sources if item.source_type == "EXPERT")
        media = self.recognized_media_source(expert)

        self.assertTrue(self.manager._expert_identity_is_absent(media))

    def test_fault_04_recognized_media_is_repaired_without_provider(self):
        state = self.start_waiting()
        report = ResearchReport.model_validate(state.research_report)
        expert = next(item for item in report.sources if item.source_type == "EXPERT")
        media = self.recognized_media_source(expert)
        calls = len(self.provider.calls)

        repaired, changed = self.manager._canonicalize_recognized_media_source(media)

        self.assertTrue(changed)
        self.assertEqual(repaired.source_type, "NEWS")
        self.assertEqual(len(self.provider.calls), calls)

    def test_fault_05_media_repair_preserves_source_identity_and_content(self):
        state = self.start_waiting()
        report = ResearchReport.model_validate(state.research_report)
        expert = next(item for item in report.sources if item.source_type == "EXPERT")
        media = self.recognized_media_source(expert)
        repaired, _changed = self.manager._canonicalize_recognized_media_source(media)
        before = media.model_dump(mode="json")
        after = repaired.model_dump(mode="json")

        for field_name in (
            "source_id",
            "evidence_id",
            "url",
            "summary",
            "relevant_excerpt",
            "retrieved_at",
            "research_question_ids",
        ):
            self.assertEqual(after[field_name], before[field_name])

    def test_fault_06_news_repair_recomputes_and_resolves_target_coverage(self):
        state = self.start_waiting()
        state, _report, _media, _question, _review = self.install_report_integrity_fixture(
            state,
            media_answers_news_question=True,
        )

        waiting = self.manager.recover_human_evidence_gate(state.workflow_id)
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)

        self.assertEqual(waiting.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertIn(
            "fqr_news_missing",
            [item.finding_id for item in summary.resolved_integrity_findings],
        )
        self.assertFalse(summary.evidence_sufficiency_findings)

    def test_fault_07_news_repair_does_not_resolve_unrelated_question(self):
        state = self.start_waiting()
        state, _report, _media, _question, _review = self.install_report_integrity_fixture(
            state,
            media_answers_news_question=False,
        )

        self.manager.recover_human_evidence_gate(state.workflow_id)
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)

        self.assertEqual(
            [item.finding_id for item in summary.evidence_sufficiency_findings],
            ["fqr_news_missing"],
        )

    def test_fault_08_human_gate_is_eligible_with_zero_hard_and_one_gap(self):
        state = self.start_waiting()
        state, _report, _media, _question, _review = self.install_report_integrity_fixture(
            state,
            media_answers_news_question=False,
        )

        self.manager.recover_human_evidence_gate(state.workflow_id)
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)

        self.assertTrue(summary.eligible)
        self.assertFalse(summary.hard_integrity_findings)
        self.assertEqual(len(summary.evidence_sufficiency_findings), 1)

    def test_fault_09_unresolved_hard_keeps_human_gate_closed(self):
        state = self.start_waiting()
        state, _report, _media, _question, _review = self.install_report_integrity_fixture(
            state,
            media_answers_news_question=False,
            include_unresolved_hard=True,
        )

        blocked = self.manager.recover_human_evidence_gate(state.workflow_id)
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)

        self.assertEqual(blocked.status, "BLOCKED")
        self.assertFalse(summary.eligible)
        self.assertEqual(
            [item.finding_id for item in summary.hard_integrity_findings],
            ["fqr_unknown_hard"],
        )

    def test_fault_10_all_report_repairs_make_zero_provider_or_retrieval_calls(self):
        state = self.start_waiting()
        state, original_report, _media, _question, original_review = (
            self.install_report_integrity_fixture(
                state,
                media_answers_news_question=False,
            )
        )
        provider_calls = len(self.provider.calls)
        provider_reservations = list(
            (self.data_dir / "provider_call_reservations").rglob("*.json")
        )
        retrieval_reservations = list(
            (self.data_dir / "retrieval_reservations").rglob("*.json")
        )

        self.manager.recover_human_evidence_gate(state.workflow_id)
        repaired = self.repository.load(state.workflow_id)

        self.assertEqual(len(self.provider.calls), provider_calls)
        self.assertEqual(
            list((self.data_dir / "provider_call_reservations").rglob("*.json")),
            provider_reservations,
        )
        self.assertEqual(
            list((self.data_dir / "retrieval_reservations").rglob("*.json")),
            retrieval_reservations,
        )
        self.assertEqual(repaired.review_result, original_review)
        self.assertEqual(
            len(ResearchReport.model_validate(repaired.research_report).sources),
            len(original_report.sources),
        )
        self.assertEqual(
            [item.provider_calls for item in repaired.human_evidence_integrity_repairs],
            [0, 0],
        )
        self.assertEqual(
            [item.retrieval_calls for item in repaired.human_evidence_integrity_repairs],
            [0, 0],
        )

    def test_quality_review_always_stops_at_human_boundary_without_outbox(self):
        state = self.start_waiting()
        calls = len(self.provider.calls)

        recovered = self.manager.recover_human_evidence_gate(state.workflow_id)

        self.assertEqual(recovered.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(len(self.provider.calls), calls)
        self.assertFalse(recovered.deliberation_sent)

    def test_acceptance_is_separate_audited_pmp_and_zero_provider_calls(self):
        state = self.start_waiting()
        calls = len(self.provider.calls)
        completed = self.decide_for_summary(state.workflow_id)

        self.assertEqual(completed.status, "COMPLETED")
        self.assertEqual(len(self.provider.calls), calls)
        self.assertFalse(completed.human_evidence_decision.provider_calls_authorized)
        decision_messages = [
            item
            for item in completed.message_history
            if item.message_type == "human_evidence_decision"
        ]
        self.assertEqual(len(decision_messages), 1)
        outbox = self.repository.load_deliberation_outbox(state.workflow_id)
        self.assertEqual(
            outbox.parent_message_id,
            decision_messages[0].message_id,
        )
        self.assertEqual(
            outbox.payload["human_evidence_decision"]["decision_id"],
            completed.human_evidence_decision.decision_id,
        )

    def test_accept_with_limitations_does_not_turn_gap_into_evidence(self):
        state = self.start_waiting()
        self.install_review(state, self.evidence_gap_review())
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)

        with self.assertRaisesRegex(ValueError, "ACCEPT is allowed only"):
            self.manager.decide_human_evidence(
                state.workflow_id,
                HumanEvidenceDecisionType.ACCEPT,
                reason="Invalid acceptance",
                actor_source=HumanActorSource.MOCK_FIXTURE,
            )
        completed = self.manager.decide_human_evidence(
            state.workflow_id,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS,
            reason="Accept disclosed gaps as unresolved limitations",
            actor_source=HumanActorSource.MOCK_FIXTURE,
        )

        self.assertEqual(completed.status, "COMPLETED")
        self.assertTrue(completed.accepted_evidence_gaps)
        self.assertTrue(
            all(not item.factual_support_confirmed for item in completed.accepted_evidence_gaps)
        )

    def test_hard_integrity_failure_cannot_be_human_overridden(self):
        state = self.start_waiting()
        review = {
            "status": "revision_required",
            "reason": "A machine-enforced integrity contract is violated",
            "findings": [
                {
                    "finding_id": "qf_hard_001",
                    "finding_type": "HARD_INTEGRITY_FAILURE",
                    "severity": "CRITICAL",
                    "research_question_id": None,
                    "target_agent_id": "researcher.manager",
                    "issue": "Evidence ID traceability is structurally invalid",
                    "required_action": "Repair the ID contract deterministically",
                }
            ],
            "revision_targets": [],
        }
        self.install_review(state, review)

        with self.assertRaisesRegex(ValueError, "cannot override integrity failures"):
            self.manager.decide_human_evidence(
                state.workflow_id,
                HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS,
                reason="Must not override",
                actor_source=HumanActorSource.MOCK_FIXTURE,
            )
        blocked = self.repository.load(state.workflow_id)
        self.assertEqual(blocked.status, "BLOCKED")
        self.assertEqual(blocked.error["code"], "HARD_INTEGRITY_FAILURE")

    def test_official_subdomain_industry_misclassification_is_repaired_before_gate(self):
        state = self.start_waiting()
        report = ResearchReport.model_validate(state.research_report)
        industry = next(item for item in report.sources if item.source_type == "INDUSTRY")
        report_data = report.model_dump(mode="json")
        next(
            item
            for item in report_data["sources"]
            if item["source_id"] == industry.source_id
        )["url"] = "https://www.cas.go.jp/policy/official-document.pdf"
        report = ResearchReport.model_validate(report_data)
        state.research_report = report.model_dump(mode="json")
        state.collected_sources = [item.model_dump(mode="json") for item in report.sources]
        self.repository.save_report(report)
        self.repository.save(state)
        review = self.evidence_gap_review()
        review["findings"].append(
            {
                "finding_id": "qf_006",
                "finding_type": "UNCLASSIFIED",
                "severity": "MINOR",
                "research_question_id": "rq_employment",
                "target_agent_id": "researcher.manager",
                "issue": (
                    f"INDUSTRYの{industry.source_id}はcas.go.jp政府文書で、"
                    "カテゴリ分類と追跡構造の確認が必要"
                ),
                "required_action": "source_type分類を修正し実質欠落を反映する",
            }
        )
        self.install_review(state, review)

        waiting = self.manager.recover_human_evidence_gate(state.workflow_id)
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)
        repaired_report = self.repository.load_report(state.workflow_id)
        repaired_source = next(
            item for item in repaired_report.sources if item.source_id == industry.source_id
        )

        self.assertEqual(
            waiting.status,
            "WAITING_HUMAN_EVIDENCE_REVIEW",
            summary.model_dump(mode="json"),
        )
        self.assertEqual(repaired_source.source_type, "GOVERNMENT")
        self.assertEqual(len(repaired_report.sources), len(report.sources))
        self.assertEqual(
            [item.finding_id for item in summary.resolved_integrity_findings],
            ["qf_006"],
        )
        self.assertEqual(
            [item.finding_id for item in summary.evidence_sufficiency_findings],
            ["qf_gap_001"],
        )
        self.assertEqual(state.review_result["findings"][-1]["finding_id"], "qf_006")

    def test_revise_records_plan_but_never_authorizes_or_calls_provider(self):
        state = self.start_waiting()
        self.install_review(state, self.evidence_gap_review())
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)
        calls = len(self.provider.calls)

        revised = asyncio.run(
            self.manager.revise(
                state.workflow_id,
                reason="Human requests more evidence",
                actor_source=HumanActorSource.MOCK_FIXTURE,
            )
        )

        self.assertEqual(revised.status, "BLOCKED")
        self.assertEqual(len(self.provider.calls), calls)
        self.assertIsNotNone(revised.evidence_revision_plan)
        self.assertTrue(revised.evidence_revision_plan.provider_authorization_required)
        self.assertFalse(revised.human_evidence_decision.provider_calls_authorized)
        self.assertFalse(revised.deliberation_sent)
        self.assertEqual(revised.revision_control.phase, "authorization_required")

    def test_separate_execute_command_consumes_revision_authorization_once(self):
        state = self.start_waiting()
        self.install_review(state, self.evidence_gap_review())
        blocked = asyncio.run(
            self.manager.revise(
                state.workflow_id,
                reason="Human requests more evidence",
                actor_source=HumanActorSource.MOCK_FIXTURE,
            )
        )
        calls_before = len(self.provider.calls)

        executed = asyncio.run(
            self.manager.execute_authorized_revision(
                blocked.workflow_id,
                actor_id="test.operator",
                actor_source="CLI",
                authorization_reason="Explicit unit-test execution approval",
            )
        )

        self.assertEqual(executed.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(executed.revision_count, 1)
        self.assertEqual(executed.revision_control.phase, "completed")
        self.assertEqual(len(self.provider.calls) - calls_before, 2)
        request_id = executed.revision_control.consumed_request_ids[0]
        authorization = self.manager.revision_exchange.load_authorization(
            executing_layer="researcher",
            workflow_id=executed.workflow_id,
            revision_request_id=request_id,
        )
        self.assertEqual(authorization.status, "consumed")
        self.assertEqual(len(authorization.provider_reservation_ids), 2)
        self.assertEqual(len(authorization.retrieval_reservation_ids), 1)

    def test_duplicate_and_concurrent_decisions_have_one_winner(self):
        state = self.start_waiting()
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)
        decision = (
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
            if summary.evidence_sufficiency_findings
            else HumanEvidenceDecisionType.ACCEPT
        )

        def submit(index):
            manager = ResearcherManager(
                ResearcherRegistry(self.provider, demo_safe_mode=True),
                ResearcherWorkflowRepository(self.data_dir),
                demo_safe_mode=True,
            )
            try:
                result = manager.decide_human_evidence(
                    state.workflow_id,
                    decision,
                    reason=f"Concurrent operator {index}",
                    actor_source=HumanActorSource.MOCK_FIXTURE,
                )
                return result.status
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, (1, 2)))

        self.assertEqual(outcomes.count("COMPLETED"), 1)
        self.assertTrue(
            any("decision already exists" in item.lower() for item in outcomes)
        )
        self.assertEqual(
            len(self.repository.list_human_evidence_decisions(state.workflow_id)), 1
        )

    def test_recovery_after_decision_artifact_written_before_state(self):
        state = self.start_waiting()
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)
        decision = (
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
            if summary.evidence_sufficiency_findings
            else HumanEvidenceDecisionType.ACCEPT
        )
        calls = len(self.provider.calls)
        original_save = self.repository.save
        injected = {"done": False}

        def fail_once(candidate):
            if candidate.human_evidence_decision is not None and not injected["done"]:
                injected["done"] = True
                raise OSError("injected state-save failure")
            return original_save(candidate)

        with patch.object(self.repository, "save", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected state-save failure"):
                self.manager.decide_human_evidence(
                    state.workflow_id,
                    decision,
                    reason="Fault injection",
                    actor_source=HumanActorSource.MOCK_FIXTURE,
                )

        recovered = self.manager.recover_human_evidence_gate(state.workflow_id)
        self.assertEqual(recovered.status, "COMPLETED")
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(
            len(self.repository.list_human_evidence_decisions(state.workflow_id)), 1
        )

    def test_recovery_reuses_outbox_after_final_state_save_failure(self):
        state = self.start_waiting()
        summary = self.manager.inspect_human_evidence_gate(state.workflow_id)
        decision = (
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
            if summary.evidence_sufficiency_findings
            else HumanEvidenceDecisionType.ACCEPT
        )
        original_save = self.repository.save
        injected = {"done": False}

        def fail_once(candidate):
            if candidate.status == "COMPLETED" and not injected["done"]:
                injected["done"] = True
                raise OSError("injected final state-save failure")
            return original_save(candidate)

        with patch.object(self.repository, "save", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected final state-save failure"):
                self.manager.decide_human_evidence(
                    state.workflow_id,
                    decision,
                    reason="Outbox fault injection",
                    actor_source=HumanActorSource.MOCK_FIXTURE,
                )
        outbox_before = self.repository.load_deliberation_outbox(state.workflow_id)

        recovered = self.manager.recover_human_evidence_gate(state.workflow_id)
        outbox_after = self.repository.load_deliberation_outbox(state.workflow_id)
        self.assertEqual(recovered.status, "COMPLETED")
        self.assertEqual(outbox_before.message_id, outbox_after.message_id)
        self.assertEqual(
            len(
                [
                    item
                    for item in recovered.message_history
                    if item.message_id == outbox_after.message_id
                ]
            ),
            1,
        )

    def test_deliberation_requires_and_propagates_human_decision(self):
        state = self.start_waiting()
        completed = self.decide_for_summary(state.workflow_id)
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
            context.human_evidence_decision.decision_id,
            completed.human_evidence_decision.decision_id,
        )
        self.assertEqual(
            {item.finding_id for item in context.accepted_evidence_gaps},
            set(completed.human_evidence_decision.accepted_finding_ids),
        )

        invalid = handoff.model_copy(deep=True)
        invalid.payload.pop("human_evidence_decision")
        with self.assertRaisesRegex(ValueError, "no Human Evidence Decision"):
            deliberation._validate_researcher_handoff(invalid)

    def test_cycle041_canonical_union_accepts_classification_and_deduplication(self):
        official = validate_human_evidence_integrity_repair(
            {
                "repair_id": "repair_official_001",
                "workflow_id": "workflow_cycle041",
                "quality_review_id": "review_cycle041",
                "finding_id": "finding_official_001",
                "repair_kind": "official_industry_source_reclassification",
                "source_id": "source_official_001",
                "previous_source_type": "INDUSTRY",
                "repaired_source_type": "GOVERNMENT",
                "rationale": "Official host classification repair",
            }
        )
        deduplication = validate_human_evidence_integrity_repair(
            {
                "repair_id": "repair_dedup_001",
                "workflow_id": "workflow_cycle041",
                "quality_review_id": "review_cycle041",
                "finding_id": "finding_dedup_001",
                "repair_kind": "report_limitation_exact_deduplication",
                "source_id": None,
                "previous_source_type": None,
                "repaired_source_type": None,
                "removed_report_limitation_count": 1,
                "removed_report_limitations": ["duplicate limitation"],
                "report_sha256_before": "a" * 64,
                "report_sha256_after": "b" * 64,
                "evidence_set_sha256_before": "c" * 64,
                "evidence_set_sha256_after": "c" * 64,
                "provider_calls": 0,
                "retrieval_calls": 0,
                "rationale": "Exact duplicate removal",
            }
        )

        self.assertEqual(
            official.repair_kind,
            "official_industry_source_reclassification",
        )
        self.assertEqual(
            deduplication.repair_kind,
            "report_limitation_exact_deduplication",
        )

    def test_cycle041_persist_reload_outbox_and_deliberation_accept_mixed_repairs(self):
        state = self.start_waiting()
        state, _report, _media, _question, _review = (
            self.install_report_integrity_fixture(
                state,
                media_answers_news_question=False,
                include_gap=True,
            )
        )
        repaired = self.manager.recover_human_evidence_gate(state.workflow_id)
        completed = self.decide_for_summary(state.workflow_id)

        reloaded = self.repository.load(state.workflow_id)
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
        expected_kinds = {
            "recognized_media_source_reclassification",
            "report_limitation_exact_deduplication",
        }

        self.assertEqual(repaired.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(completed.status, "COMPLETED")
        self.assertEqual(
            {item.repair_kind for item in reloaded.human_evidence_integrity_repairs},
            expected_kinds,
        )
        self.assertEqual(
            {item.repair_kind for item in context.human_evidence_integrity_repairs},
            expected_kinds,
        )

    def test_cycle041_unknown_repair_kind_fails_closed_at_consumer_boundary(self):
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported HumanEvidenceIntegrityRepair kind",
        ):
            validate_human_evidence_integrity_repair(
                {
                    "repair_kind": "future_unreviewed_repair",
                    "repair_id": "repair_unknown_001",
                }
            )


if __name__ == "__main__":
    unittest.main()
