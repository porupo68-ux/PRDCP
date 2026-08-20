from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from researcher.schemas.research_report import ResearchReport
from researcher.schemas.source import ResearchSource, ResearchSourceType


SOURCE_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.:-])source_[A-Za-z0-9_.:-]+(?![A-Za-z0-9_.:-])"
)
EVIDENCE_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.:-])evidence_[A-Za-z0-9_.:-]+(?![A-Za-z0-9_.:-])"
)
VERSION_PATTERN = re.compile(
    r"(?:\bver(?:sion)?\.?|バージョン)\s*([0-9]+(?:\.[0-9]+)*)",
    re.IGNORECASE,
)
DUPLICATE_MARKERS = ("duplicate", "重複", "同一文書", "同一ガイドライン")
TRACKING_MARKERS = ("merged_evidence_ids", "追跡", "tracking", "統合")
GUIDELINE_MARKERS = ("guideline", "ガイドライン")
NOTICE_MARKERS = ("notice", "notification", "通知", "改訂について")


class DuplicateTrackingRepairError(ValueError):
    """Fail-closed duplicate tracking repairability error."""


@dataclass(frozen=True)
class DuplicateTrackingPlan:
    finding_id: str
    document_family_id: str
    canonical_source_id: str
    canonical_evidence_id: str
    related_source_ids: tuple[str, ...]
    merged_evidence_ids: tuple[str, ...]


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _source_text(source: ResearchSource) -> str:
    return "\n".join(
        (
            source.title,
            source.summary,
            source.relevant_excerpt or "",
            str(source.source_specific_metadata.get("document_type") or ""),
        )
    )


def _publisher_identity(source: ResearchSource) -> tuple[str, str]:
    host = (urlsplit(str(source.url)).hostname or "").casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    organization = _normalize_text(
        source.source_specific_metadata.get("organization")
        or source.author_or_organization
        or source.source_name
    )
    return host, organization


def _canonical_rank(source: ResearchSource) -> int | None:
    text = _source_text(source).casefold()
    if not any(marker in text for marker in GUIDELINE_MARKERS):
        return None
    title = source.title.casefold()
    path = urlsplit(str(source.url)).path.casefold()
    is_notice = any(marker in title for marker in NOTICE_MARKERS)
    if source.primary_source and path.endswith(".pdf") and not is_notice:
        return 0
    if source.primary_source and not is_notice:
        return 1
    return 2


def _relation_snapshot(report: ResearchReport, source_ids: set[str]) -> dict[str, Any]:
    return {
        source.source_id: list(
            source.source_specific_metadata.get("merged_evidence_ids") or []
        )
        for source in report.sources
        if source.source_id in source_ids
    }


def relation_metadata_sha256(report: ResearchReport, source_ids: set[str]) -> str:
    return canonical_json_sha256(_relation_snapshot(report, source_ids))


def immutable_report_payload(report: ResearchReport) -> dict[str, Any]:
    """Return the Report with only duplicate relation metadata removed."""

    payload = report.model_dump(mode="json")
    for collection_name in ("sources", "source_metadata"):
        for source in payload.get(collection_name, []):
            metadata = dict(source.get("source_specific_metadata") or {})
            metadata.pop("merged_evidence_ids", None)
            source["source_specific_metadata"] = metadata
    return payload


def immutable_report_sha256(report: ResearchReport) -> str:
    return canonical_json_sha256(immutable_report_payload(report))


def is_duplicate_tracking_finding(finding: Any) -> bool:
    if str(getattr(finding, "target_agent_id", "")) != "researcher.manager":
        return False
    text = f"{getattr(finding, 'issue', '')}\n{getattr(finding, 'required_action', '')}".casefold()
    return any(marker in text for marker in DUPLICATE_MARKERS) and any(
        marker in text for marker in TRACKING_MARKERS
    )


def plan_duplicate_tracking_repair(
    report: ResearchReport,
    finding: Any,
) -> DuplicateTrackingPlan | None:
    """Build a narrow same-document-family plan or fail closed.

    Returning ``None`` means that the finding is not this repair class. Once a
    finding identifies this class, every ambiguity is an error rather than a
    best-effort guess.
    """

    if not is_duplicate_tracking_finding(finding):
        return None

    text = f"{finding.issue}\n{finding.required_action}"
    source_ids = tuple(dict.fromkeys(SOURCE_ID_PATTERN.findall(text)))
    if len(source_ids) < 2:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: finding must identify at least two source_ids"
        )
    source_by_id = {source.source_id: source for source in report.sources}
    missing_sources = sorted(set(source_ids) - set(source_by_id))
    if missing_sources:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: unknown source_ids: "
            + ", ".join(missing_sources)
        )
    sources = [source_by_id[source_id] for source_id in source_ids]
    if any(
        source.source_type != ResearchSourceType.GOVERNMENT.value
        or not source.primary_source
        for source in sources
    ):
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: all family members must be primary GOVERNMENT sources"
        )

    publisher_identities = {_publisher_identity(source) for source in sources}
    if len(publisher_identities) != 1 or not all(publisher_identities.pop()):
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: issuing organization or official host differs"
        )

    ranked = [(rank, source) for source in sources if (rank := _canonical_rank(source)) is not None]
    if not ranked:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: no official guideline body candidate"
        )
    best_rank = min(rank for rank, _source in ranked)
    candidates = [source for rank, source in ranked if rank == best_rank]
    if len(candidates) != 1:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_AMBIGUOUS_CANONICAL: canonical source is not unique"
        )
    canonical = candidates[0]
    canonical_title = _normalize_text(canonical.title)
    if not canonical_title:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: canonical title is empty"
        )
    for source in sources:
        if canonical_title not in _normalize_text(_source_text(source)):
            raise DuplicateTrackingRepairError(
                "DUPLICATE_TRACKING_UNPROVEN: normalized document title relation is absent"
            )

    versions = {
        version
        for source in sources
        for version in VERSION_PATTERN.findall(_source_text(source))
    }
    if not versions:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: document version is absent"
        )
    if len(versions) != 1:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_VERSION_CONFLICT: document versions differ"
        )

    referenced_evidence_ids = set(EVIDENCE_ID_PATTERN.findall(text))
    expected_evidence_ids = {source.evidence_id for source in sources}
    if referenced_evidence_ids and referenced_evidence_ids != expected_evidence_ids:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: finding Evidence IDs do not match its Source IDs"
        )

    target_evidence_ids = set(expected_evidence_ids)
    for source in sources:
        existing = set(source.source_specific_metadata.get("merged_evidence_ids") or [])
        unknown = existing - target_evidence_ids
        if unknown:
            raise DuplicateTrackingRepairError(
                "DUPLICATE_TRACKING_CONFLICT: existing relation references another family"
            )
        if source.source_id != canonical.source_id and existing - {source.evidence_id}:
            raise DuplicateTrackingRepairError(
                "DUPLICATE_TRACKING_CONFLICT: a related source already claims a canonical relation"
            )

    related_sources = sorted(
        (source for source in sources if source.source_id != canonical.source_id),
        key=lambda source: source.source_id,
    )
    merged_evidence_ids = tuple(source.evidence_id for source in related_sources)
    family_material = "\x1f".join(
        (
            *_publisher_identity(canonical),
            canonical_title,
            next(iter(versions)),
        )
    )
    document_family_id = "docfam_" + hashlib.sha256(
        family_material.encode("utf-8")
    ).hexdigest()[:24]
    return DuplicateTrackingPlan(
        finding_id=str(finding.finding_id),
        document_family_id=document_family_id,
        canonical_source_id=canonical.source_id,
        canonical_evidence_id=canonical.evidence_id,
        related_source_ids=tuple(source.source_id for source in related_sources),
        merged_evidence_ids=merged_evidence_ids,
    )


def apply_duplicate_tracking_plan(
    report: ResearchReport,
    plan: DuplicateTrackingPlan,
) -> ResearchReport:
    data = report.model_dump(mode="json")
    source_ids = {source["source_id"] for source in data["sources"]}
    expected_ids = {plan.canonical_source_id, *plan.related_source_ids}
    if not expected_ids <= source_ids:
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_UNPROVEN: repair plan no longer matches the Report"
        )
    for collection_name in ("sources", "source_metadata"):
        canonical = next(
            (
                source
                for source in data[collection_name]
                if source["source_id"] == plan.canonical_source_id
            ),
            None,
        )
        if canonical is None:
            raise DuplicateTrackingRepairError(
                f"DUPLICATE_TRACKING_UNPROVEN: canonical {collection_name} entry is absent"
            )
        metadata = dict(canonical.get("source_specific_metadata") or {})
        metadata["merged_evidence_ids"] = list(plan.merged_evidence_ids)
        canonical["source_specific_metadata"] = metadata
    repaired = ResearchReport.model_validate(data)
    if immutable_report_sha256(repaired) != immutable_report_sha256(report):
        raise DuplicateTrackingRepairError(
            "DUPLICATE_TRACKING_CONTENT_CHANGED: repair changed protected Report content"
        )
    return repaired
