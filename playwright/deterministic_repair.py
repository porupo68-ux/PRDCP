from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from playwright.schemas.citation_manifest import (
    CitationManifest,
    CitationMapping,
    CitationValidatedScript,
    ScriptClaimType,
)
from playwright.schemas.deterministic_repair import (
    PlaywrightDeterministicRepairRecord,
    PlaywrightDeterministicRepairType,
    PlaywrightRepairDisposition,
)
from playwright.schemas.production_context import ProductionContext
from playwright.schemas.script_draft import ScriptDraft
from playwright.schemas.validation import (
    DeterministicValidationResult,
    ValidationFinding,
    ValidationSeverity,
)
from playwright.state import PlaywrightWorkflowState, utc_now
from playwright.validator import canonical_hash, canonical_script_claim_ids


REPAIRABLE_FINDING_CODES = frozenset(
    {
        "CITATION_MAPPING_MISSING",
        "UNSUPPORTED_CLAIM_LIST_NOT_EMPTY",
        "UNSUPPORTED_CLAIM_REMAINS",
    }
)
MAX_DETERMINISTIC_REPAIR_PASSES = 1


class DeterministicRepairIneligible(ValueError):
    """The saved finding cannot be repaired without new judgment or content."""


@dataclass(frozen=True)
class DeterministicRepairResult:
    citation_manifest: CitationManifest
    citation_validated_script: CitationValidatedScript
    record: PlaywrightDeterministicRepairRecord


class PlaywrightDeterministicRepairer:
    """Allowlisted local repair for mechanically reconstructible artifacts."""

    @staticmethod
    def classify(finding: ValidationFinding) -> PlaywrightRepairDisposition:
        if finding.code in REPAIRABLE_FINDING_CODES:
            return PlaywrightRepairDisposition.DETERMINISTIC_REPAIRABLE
        if finding.upstream_required:
            return PlaywrightRepairDisposition.UPSTREAM_REVISION_REQUIRED
        if finding.target_agent_id:
            return PlaywrightRepairDisposition.AGENT_REVISION_REQUIRED
        return PlaywrightRepairDisposition.NON_REPAIRABLE

    def repair(self, state: PlaywrightWorkflowState) -> DeterministicRepairResult:
        if state.deterministic_repair_count >= MAX_DETERMINISTIC_REPAIR_PASSES:
            raise DeterministicRepairIneligible(
                "Deterministic repair budget is exhausted; repeated repair is blocked"
            )
        if state.deterministic_validation is None or state.final_gate_result is None:
            raise DeterministicRepairIneligible(
                "A complete saved deterministic gate is required"
            )

        validation = DeterministicValidationResult.model_validate(
            state.deterministic_validation
        )
        errors = [
            finding
            for finding in validation.findings
            if finding.severity == ValidationSeverity.ERROR.value
        ]
        if not errors:
            raise DeterministicRepairIneligible(
                "Saved deterministic gate has no blocking finding to repair"
            )
        dispositions = [self.classify(finding) for finding in errors]
        if any(
            disposition != PlaywrightRepairDisposition.DETERMINISTIC_REPAIRABLE
            for disposition in dispositions
        ):
            codes = sorted(
                finding.code
                for finding, disposition in zip(errors, dispositions, strict=True)
                if disposition
                != PlaywrightRepairDisposition.DETERMINISTIC_REPAIRABLE
            )
            raise DeterministicRepairIneligible(
                "Non-repairable blocking findings remain: " + ", ".join(codes)
            )

        gate_blocking = set(state.final_gate_result.get("blocking_finding_ids") or [])
        error_ids = {finding.finding_id for finding in errors}
        if gate_blocking != error_ids:
            raise DeterministicRepairIneligible(
                "Saved Final Gate blocking IDs do not match deterministic validation"
            )

        context = ProductionContext.model_validate(state.production_context)
        script = ScriptDraft.model_validate(state.script_draft)
        validated_script = CitationValidatedScript.model_validate(
            state.citation_validated_script
        )
        manifest = CitationManifest.model_validate(state.citation_manifest)
        # Hash the exact persisted payload before compatibility defaults are
        # materialized so the audit can be matched to the source checkpoint.
        before_hash = canonical_hash(state.citation_manifest or {})

        draft_paragraphs = {
            paragraph.paragraph_id: paragraph
            for section in script.sections
            for paragraph in section.paragraphs
        }
        validated_paragraphs = {
            paragraph.paragraph_id: paragraph
            for section in validated_script.sections
            for paragraph in section.paragraphs
        }
        evidence_to_sources: dict[str, set[str]] = {}
        for item in context.source_manifest:
            evidence_id = item.get("evidence_id") if isinstance(item, dict) else None
            source_id = item.get("source_id") if isinstance(item, dict) else None
            if isinstance(evidence_id, str) and isinstance(source_id, str):
                evidence_to_sources.setdefault(evidence_id, set()).add(source_id)
        manifest_source_pairs = {
            (item.evidence_id, item.source_id) for item in manifest.source_list
        }
        accepted_gap_ids = {
            value
            for gap in context.accepted_evidence_gaps
            for value in (
                gap.finding_id,
                gap.quality_review_id,
                gap.human_decision_id,
                gap.research_question_id,
            )
        }

        additions: list[CitationMapping] = []
        replacements: dict[str, CitationMapping] = {}
        removed_mapping_ids: set[str] = set()
        donor_mapping_ids: list[str] = []
        all_claim_ids: list[str] = []
        all_evidence_ids: list[str] = []
        all_source_ids: list[str] = []
        paragraph_ids: list[str] = []
        missing_mapping_ids: list[str] = []
        cleaned_unsupported_paragraph_ids: list[str] = []
        unsupported_claim_ids: list[str] = []

        mapping_findings = [
            finding
            for finding in errors
            if finding.code == "CITATION_MAPPING_MISSING"
        ]
        unsupported_list_findings = [
            finding
            for finding in errors
            if finding.code == "UNSUPPORTED_CLAIM_LIST_NOT_EMPTY"
        ]
        unsupported_mapping_findings = [
            finding
            for finding in errors
            if finding.code == "UNSUPPORTED_CLAIM_REMAINS"
        ]

        for finding in mapping_findings:
            details = finding.details
            paragraph_id = details.get("paragraph_id")
            if not isinstance(paragraph_id, str) or not paragraph_id:
                raise DeterministicRepairIneligible(
                    "CITATION_MAPPING_MISSING has no paragraph identity"
                )
            draft_paragraph = draft_paragraphs.get(paragraph_id)
            paragraph = validated_paragraphs.get(paragraph_id)
            if draft_paragraph is None or paragraph is None:
                raise DeterministicRepairIneligible(
                    f"Citation paragraph does not exist in both scripts: {paragraph_id}"
                )
            if (
                draft_paragraph.claim_ids != paragraph.claim_ids
                or draft_paragraph.evidence_ids != paragraph.evidence_ids
                or draft_paragraph.speaker_text != paragraph.speaker_text
            ):
                raise DeterministicRepairIneligible(
                    f"Citation paragraph changed across saved scripts: {paragraph_id}"
                )
            if not paragraph.citation_required or not paragraph.evidence_ids:
                raise DeterministicRepairIneligible(
                    f"Citation paragraph is not mechanically repairable: {paragraph_id}"
                )
            if (
                list(details.get("claim_ids") or []) != paragraph.claim_ids
                or list(details.get("evidence_ids") or []) != paragraph.evidence_ids
            ):
                raise DeterministicRepairIneligible(
                    f"Saved finding does not match paragraph traceability: {paragraph_id}"
                )

            existing = [
                mapping
                for mapping in manifest.mappings
                if mapping.paragraph_id == paragraph_id
            ]
            if existing:
                raise DeterministicRepairIneligible(
                    f"CITATION_MAPPING_CONFLICT: {paragraph_id} already has a mapping"
                )

            source_ids: list[str] = []
            for evidence_id in paragraph.evidence_ids:
                if evidence_id not in context.must_include_evidence_ids:
                    raise DeterministicRepairIneligible(
                        f"Evidence is outside canonical Production Context: {evidence_id}"
                    )
                sources = evidence_to_sources.get(evidence_id, set())
                if len(sources) != 1:
                    raise DeterministicRepairIneligible(
                        "CITATION_LOCATOR_MISSING: evidence must resolve to exactly one "
                        f"saved source: {evidence_id}"
                    )
                source_id = next(iter(sources))
                if (evidence_id, source_id) not in manifest_source_pairs:
                    raise DeterministicRepairIneligible(
                        "CITATION_LOCATOR_MISSING: Citation Manifest source registry is "
                        f"missing {evidence_id} -> {source_id}"
                    )
                source_ids.append(source_id)

            referenced_ids = set(
                paragraph.claim_ids + paragraph.evidence_ids + source_ids
            )
            if referenced_ids & accepted_gap_ids:
                raise DeterministicRepairIneligible(
                    "Accepted unresolved gap cannot be promoted into Citation Mapping"
                )

            donors = [
                mapping
                for mapping in manifest.mappings
                if mapping.claim_ids == paragraph.claim_ids
                and mapping.evidence_ids == paragraph.evidence_ids
                and mapping.source_ids == source_ids
                and mapping.citation_locator is not None
                and mapping.citation_locator.source_ids == source_ids
            ]
            claim_types = {mapping.claim_type for mapping in donors}
            support_statuses = {mapping.support_status for mapping in donors}
            if (
                not donors
                or len(claim_types) != 1
                or len(support_statuses) != 1
                or ScriptClaimType.UNSUPPORTED.value in claim_types
            ):
                raise DeterministicRepairIneligible(
                    "No unambiguous saved semantic classification exists for "
                    f"{paragraph_id}; Agent revision is required"
                )

            mapping_id = self._mapping_id(
                paragraph_id=paragraph_id,
                claim_ids=paragraph.claim_ids,
                evidence_ids=paragraph.evidence_ids,
                source_ids=source_ids,
            )
            additions.append(
                CitationMapping(
                    citation_mapping_id=mapping_id,
                    paragraph_id=paragraph_id,
                    claim_text=paragraph.speaker_text,
                    claim_type=next(iter(claim_types)),
                    claim_ids=paragraph.claim_ids,
                    evidence_ids=paragraph.evidence_ids,
                    source_ids=source_ids,
                    citation_locator={"source_ids": source_ids},
                    support_status=next(iter(support_statuses)),
                    wording_risk="deterministic_traceability_reconstruction",
                    required_revision=None,
                )
            )
            donor_mapping_ids.extend(mapping.citation_mapping_id for mapping in donors)
            paragraph_ids.append(paragraph_id)
            missing_mapping_ids.append(paragraph_id)
            all_claim_ids.extend(paragraph.claim_ids)
            all_evidence_ids.extend(paragraph.evidence_ids)
            all_source_ids.extend(source_ids)

        for finding in unsupported_mapping_findings:
            details = finding.details
            paragraph_id = details.get("paragraph_id")
            mapping_id = details.get("citation_mapping_id")
            if not isinstance(paragraph_id, str) or not paragraph_id:
                raise DeterministicRepairIneligible(
                    "UNSUPPORTED_CLAIM_REMAINS has no paragraph identity"
                )
            candidates = [
                mapping
                for mapping in manifest.mappings
                if mapping.paragraph_id == paragraph_id
                and mapping.claim_type == ScriptClaimType.UNSUPPORTED.value
                and (
                    not isinstance(mapping_id, str)
                    or mapping.citation_mapping_id == mapping_id
                )
            ]
            if len(candidates) != 1:
                raise DeterministicRepairIneligible(
                    "Unsupported mapping identity is missing or ambiguous: "
                    + paragraph_id
                )
            unsupported_mapping = candidates[0]
            draft_paragraph = draft_paragraphs.get(paragraph_id)
            paragraph = validated_paragraphs.get(paragraph_id)
            if draft_paragraph is None or paragraph is None:
                raise DeterministicRepairIneligible(
                    f"Unsupported paragraph does not exist in both scripts: {paragraph_id}"
                )
            if (
                draft_paragraph.claim_ids != paragraph.claim_ids
                or draft_paragraph.evidence_ids != paragraph.evidence_ids
            ):
                raise DeterministicRepairIneligible(
                    f"Unsupported paragraph traceability changed: {paragraph_id}"
                )
            if not paragraph.citation_required:
                removed_mapping_ids.add(unsupported_mapping.citation_mapping_id)
            else:
                donors = [
                    mapping
                    for mapping in manifest.mappings
                    if mapping.citation_mapping_id
                    != unsupported_mapping.citation_mapping_id
                    and mapping.claim_ids == paragraph.claim_ids
                    and mapping.evidence_ids == paragraph.evidence_ids
                    and mapping.source_ids == unsupported_mapping.source_ids
                    and mapping.claim_type != ScriptClaimType.UNSUPPORTED.value
                    and mapping.citation_locator is not None
                ]
                claim_types = {mapping.claim_type for mapping in donors}
                support_statuses = {mapping.support_status for mapping in donors}
                if (
                    not donors
                    or len(claim_types) != 1
                    or len(support_statuses) != 1
                ):
                    raise DeterministicRepairIneligible(
                        "No unambiguous saved classification can replace unsupported "
                        f"mapping: {paragraph_id}"
                    )
                replacement = unsupported_mapping.model_copy(
                    update={
                        "claim_text": paragraph.speaker_text,
                        "claim_type": next(iter(claim_types)),
                        "support_status": next(iter(support_statuses)),
                        "wording_risk": "deterministic_contract_reconstruction",
                        "required_revision": None,
                    }
                )
                replacements[unsupported_mapping.citation_mapping_id] = replacement
                donor_mapping_ids.extend(
                    mapping.citation_mapping_id for mapping in donors
                )
                all_evidence_ids.extend(paragraph.evidence_ids)
                all_source_ids.extend(unsupported_mapping.source_ids)
            paragraph_ids.append(paragraph_id)
            cleaned_unsupported_paragraph_ids.append(paragraph_id)
            unsupported_claim_ids.extend(unsupported_mapping.claim_ids)
            all_claim_ids.extend(unsupported_mapping.claim_ids)

        working_mappings = [
            replacements.get(mapping.citation_mapping_id, mapping)
            for mapping in manifest.mappings
            if mapping.citation_mapping_id not in removed_mapping_ids
        ]
        working_mappings.extend(additions)

        canonical_claim_ids = canonical_script_claim_ids(script)
        canonical_claim_set = set(canonical_claim_ids)
        supported_by_saved_mappings = {
            claim_id
            for mapping in working_mappings
            if mapping.claim_type != ScriptClaimType.UNSUPPORTED.value
            and mapping.evidence_ids
            and mapping.source_ids
            and mapping.citation_locator is not None
            for claim_id in mapping.claim_ids
        }
        unsupported_script_claims = canonical_claim_set - supported_by_saved_mappings
        if unsupported_script_claims:
            raise DeterministicRepairIneligible(
                "Script claims have no saved supported citation mapping: "
                + ", ".join(sorted(unsupported_script_claims))
            )

        if manifest.unsupported_claims and not unsupported_list_findings:
            raise DeterministicRepairIneligible(
                "Saved validation omitted the non-empty unsupported claim list"
            )
        for issue in manifest.unsupported_claims:
            paragraph = draft_paragraphs.get(issue.paragraph_id)
            validated_paragraph = validated_paragraphs.get(issue.paragraph_id)
            issue_claims = set(issue.claim_ids)
            if paragraph is None or validated_paragraph is None:
                raise DeterministicRepairIneligible(
                    "Unsupported claim issue references an unknown paragraph: "
                    + issue.paragraph_id
                )
            if (
                paragraph.claim_ids != validated_paragraph.claim_ids
                or not issue_claims
                or not issue_claims.issubset(set(paragraph.claim_ids))
                or not issue_claims.issubset(supported_by_saved_mappings)
            ):
                raise DeterministicRepairIneligible(
                    "Unsupported claim issue is not globally supported by the saved "
                    f"Script/Manifest contract: {issue.paragraph_id}"
                )
            cleaned_unsupported_paragraph_ids.append(issue.paragraph_id)
            paragraph_ids.append(issue.paragraph_id)
            unsupported_claim_ids.extend(issue.claim_ids)
            all_claim_ids.extend(issue.claim_ids)

        # Recheck every citation-required Script paragraph against the rebuilt
        # Manifest.  Existence alone is insufficient: claim and evidence sets
        # must reproduce the Script-owned traceability exactly.
        mappings_by_paragraph: dict[str, list[CitationMapping]] = {}
        for mapping in working_mappings:
            mappings_by_paragraph.setdefault(mapping.paragraph_id, []).append(mapping)
        for paragraph in draft_paragraphs.values():
            if not paragraph.citation_required:
                continue
            mappings = mappings_by_paragraph.get(paragraph.paragraph_id, [])
            mapped_claim_ids = {
                claim_id for mapping in mappings for claim_id in mapping.claim_ids
            }
            mapped_evidence_ids = {
                evidence_id
                for mapping in mappings
                for evidence_id in mapping.evidence_ids
            }
            if (
                not mappings
                or mapped_claim_ids != set(paragraph.claim_ids)
                or mapped_evidence_ids != set(paragraph.evidence_ids)
                or any(
                    mapping.claim_type == ScriptClaimType.UNSUPPORTED.value
                    for mapping in mappings
                )
            ):
                raise DeterministicRepairIneligible(
                    "Rebuilt citation mapping contract is incomplete: "
                    + paragraph.paragraph_id
                )

        repaired = manifest.model_copy(
            update={
                "supported_claim_ids": canonical_claim_ids,
                "mappings": working_mappings,
                "unsupported_claims": [],
            }
        )
        repaired = CitationManifest.model_validate(repaired.model_dump(mode="json"))
        cleaned_issue_keys = {
            (issue.paragraph_id, tuple(issue.claim_ids))
            for issue in manifest.unsupported_claims
        }
        repaired_validated_script = validated_script.model_copy(
            update={
                "unresolved_citation_issues": [
                    issue
                    for issue in validated_script.unresolved_citation_issues
                    if (issue.paragraph_id, tuple(issue.claim_ids))
                    not in cleaned_issue_keys
                ]
            }
        )
        repaired_validated_script = CitationValidatedScript.model_validate(
            repaired_validated_script.model_dump(mode="json")
        )
        after_hash = canonical_hash(repaired.model_dump(mode="json"))
        if before_hash == after_hash:
            raise DeterministicRepairIneligible(
                "Deterministic repair did not change the Citation Manifest"
            )
        repair_id = self._repair_id(
            state.workflow_id,
            before_hash,
            after_hash,
            [finding.finding_id for finding in errors],
        )
        record = PlaywrightDeterministicRepairRecord(
            repair_id=repair_id,
            repair_type=(
                PlaywrightDeterministicRepairType.CITATION_MANIFEST_CONTRACT_RECONSTRUCTION
                if (
                    unsupported_list_findings
                    or unsupported_mapping_findings
                    or manifest.supported_claim_ids != canonical_claim_ids
                )
                else PlaywrightDeterministicRepairType.CITATION_MAPPING_RECONSTRUCTION
            ),
            finding_ids=[finding.finding_id for finding in errors],
            paragraph_ids=list(dict.fromkeys(paragraph_ids)),
            claim_ids=list(dict.fromkeys(all_claim_ids)),
            evidence_ids=list(dict.fromkeys(all_evidence_ids)),
            source_ids=list(dict.fromkeys(all_source_ids)),
            mapping_ids_added=[mapping.citation_mapping_id for mapping in additions],
            donor_mapping_ids=list(dict.fromkeys(donor_mapping_ids)),
            missing_mapping_ids=list(dict.fromkeys(missing_mapping_ids)),
            unsupported_claim_ids=list(dict.fromkeys(unsupported_claim_ids)),
            repaired_mapping_ids=list(
                dict.fromkeys(
                    [mapping.citation_mapping_id for mapping in additions]
                    + list(replacements)
                    + sorted(removed_mapping_ids)
                )
            ),
            cleaned_unsupported_paragraph_ids=list(
                dict.fromkeys(cleaned_unsupported_paragraph_ids)
            ),
            script_claim_count=len(canonical_claim_ids),
            manifest_claim_count_before=len(manifest.supported_claim_ids),
            manifest_claim_count_after=len(repaired.supported_claim_ids),
            unsupported_claim_count_before=len(manifest.unsupported_claims),
            unsupported_claim_count_after=len(repaired.unsupported_claims),
            citation_mapping_count_before=len(manifest.mappings),
            citation_mapping_count_after=len(repaired.mappings),
            citation_manifest_hash_before=before_hash,
            citation_manifest_hash_after=after_hash,
            final_conclusion_hash=canonical_hash(state.final_conclusion),
            production_context_hash=canonical_hash(state.production_context or {}),
            narrative_blueprint_hash=canonical_hash(
                state.narrative_blueprint or {}
            ),
            script_draft_hash=canonical_hash(state.script_draft or {}),
            citation_validated_script_hash=canonical_hash(
                state.citation_validated_script or {}
            ),
            citation_validated_script_hash_before=canonical_hash(
                state.citation_validated_script or {}
            ),
            citation_validated_script_hash_after=canonical_hash(
                repaired_validated_script.model_dump(mode="json")
            ),
            visual_plan_hash=canonical_hash(state.visual_plan or {}),
            provider_calls=0,
            retrieval_calls=0,
            created_at=utc_now(),
        )
        return DeterministicRepairResult(
            citation_manifest=repaired,
            citation_validated_script=repaired_validated_script,
            record=record,
        )

    @staticmethod
    def _mapping_id(
        *,
        paragraph_id: str,
        claim_ids: list[str],
        evidence_ids: list[str],
        source_ids: list[str],
    ) -> str:
        payload = {
            "paragraph_id": paragraph_id,
            "claim_ids": claim_ids,
            "evidence_ids": evidence_ids,
            "source_ids": source_ids,
        }
        return "citation_mapping_repair_" + PlaywrightDeterministicRepairer._digest(
            payload
        )[:24]

    @staticmethod
    def _repair_id(
        workflow_id: str,
        before_hash: str,
        after_hash: str,
        finding_ids: list[str],
    ) -> str:
        return "playwright_repair_" + PlaywrightDeterministicRepairer._digest(
            {
                "workflow_id": workflow_id,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "finding_ids": finding_ids,
            }
        )[:24]

    @staticmethod
    def _digest(value: dict[str, Any]) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
