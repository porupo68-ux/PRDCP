from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from typing_extensions import TypedDict

from researcher.schemas.trace_ids import EvidenceId, SourceId


class ResearchSourceType(str, Enum):
    EXPERT = "EXPERT"
    ACADEMIC = "ACADEMIC"
    GOVERNMENT = "GOVERNMENT"
    NEWS = "NEWS"
    PUBLIC_OPINION = "PUBLIC_OPINION"
    POLITICIAN = "POLITICIAN"
    INDUSTRY = "INDUSTRY"


class EvidenceStance(str, Enum):
    SUPPORTS = "SUPPORTS"
    OPPOSES = "OPPOSES"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ReliabilityLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class EvidenceDirectness(str, Enum):
    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"
    CONTEXTUAL = "CONTEXTUAL"
    UNKNOWN = "UNKNOWN"


REQUIRED_METADATA = {
    ResearchSourceType.EXPERT: {"expert_name", "field", "affiliation", "statement_context"},
    ResearchSourceType.ACADEMIC: {"doi", "peer_reviewed", "journal_name", "study_type"},
    ResearchSourceType.GOVERNMENT: {"organization", "country", "document_type"},
    ResearchSourceType.NEWS: {"media_name", "article_type"},
    ResearchSourceType.PUBLIC_OPINION: {
        "platform",
        "engagement_count",
        "sample_size",
        "representativeness_warning",
    },
    ResearchSourceType.POLITICIAN: {
        "politician_name",
        "party",
        "position",
        "statement_type",
    },
    ResearchSourceType.INDUSTRY: {
        "organization_name",
        "organization_type",
        "industry",
    },
}

REQUIRED_IDENTITY_METADATA = {
    ResearchSourceType.EXPERT: {"expert_name", "affiliation"},
    ResearchSourceType.ACADEMIC: {"journal_name", "study_type"},
    ResearchSourceType.GOVERNMENT: {"organization", "country"},
    ResearchSourceType.NEWS: {"media_name", "article_type"},
    ResearchSourceType.PUBLIC_OPINION: {"platform"},
    ResearchSourceType.POLITICIAN: {"politician_name", "statement_type"},
    ResearchSourceType.INDUSTRY: {"organization_name", "organization_type"},
}

# Only names which identify the person, publication, institution or platform
# must be textually grounded in Retrieval.  Values such as study_type,
# article_type and organization_type are analytical classifications; requiring
# them to occur verbatim in a source confuses classification with quotation.
GROUNDED_IDENTITY_METADATA = {
    ResearchSourceType.EXPERT: {"expert_name", "affiliation"},
    ResearchSourceType.ACADEMIC: {"journal_name"},
    ResearchSourceType.GOVERNMENT: {"organization"},
    ResearchSourceType.NEWS: {"media_name"},
    ResearchSourceType.PUBLIC_OPINION: {"platform"},
    ResearchSourceType.POLITICIAN: {"politician_name"},
    ResearchSourceType.INDUSTRY: {"organization_name"},
}
# These fields describe source provenance rather than analytical conclusions.
# Retrieval currently persists URL/title/content but does not persist a separate
# author/publisher record.  The Provider must therefore not invent these values:
# the Researcher adapter supplies a deterministic hostname/official-domain value,
# or None when a person/affiliation cannot be established without inference.
PROVENANCE_HYDRATED_METADATA = {
    ResearchSourceType.EXPERT: {"expert_name", "affiliation"},
    ResearchSourceType.ACADEMIC: {"journal_name"},
    ResearchSourceType.GOVERNMENT: {"organization", "country"},
    ResearchSourceType.NEWS: {"media_name"},
    ResearchSourceType.PUBLIC_OPINION: {"platform"},
    ResearchSourceType.POLITICIAN: {"politician_name", "party", "position"},
    ResearchSourceType.INDUSTRY: {"organization_name"},
}
NULLABLE_UNVERIFIED_IDENTITY_METADATA = {
    ResearchSourceType.EXPERT: {"expert_name", "affiliation"},
    ResearchSourceType.GOVERNMENT: {"country"},
    ResearchSourceType.POLITICIAN: {"politician_name"},
}
PLACEHOLDER_IDENTITY_VALUES = {"null", "none", "unknown", "n/a", "na", "-"}


class MetadataExtensions(TypedDict, total=False):
    merged_evidence_ids: list[EvidenceId]


class ExpertMetadata(MetadataExtensions):
    __pydantic_config__ = ConfigDict(extra="forbid")

    expert_name: str | None
    field: str
    affiliation: str | None
    statement_context: str


class AcademicMetadata(MetadataExtensions):
    __pydantic_config__ = ConfigDict(extra="forbid")

    doi: str | None
    peer_reviewed: bool
    journal_name: str
    study_type: str


class GovernmentMetadata(MetadataExtensions):
    __pydantic_config__ = ConfigDict(extra="forbid")

    organization: str
    country: str | None
    document_type: str


class NewsMetadata(MetadataExtensions):
    __pydantic_config__ = ConfigDict(extra="forbid")

    media_name: str
    article_type: str


class PublicOpinionMetadata(MetadataExtensions):
    __pydantic_config__ = ConfigDict(extra="forbid")

    platform: str
    engagement_count: int
    sample_size: int | None
    representativeness_warning: bool


class PoliticianMetadata(MetadataExtensions):
    __pydantic_config__ = ConfigDict(extra="forbid")

    politician_name: str | None
    party: str | None
    position: str | None
    statement_type: str


class IndustryMetadata(MetadataExtensions):
    __pydantic_config__ = ConfigDict(extra="forbid")

    organization_name: str
    organization_type: str
    industry: str


SourceSpecificMetadata: TypeAlias = (
    ExpertMetadata
    | AcademicMetadata
    | GovernmentMetadata
    | NewsMetadata
    | PublicOpinionMetadata
    | PoliticianMetadata
    | IndustryMetadata
)


SOURCE_METADATA_MODELS = {
    ResearchSourceType.EXPERT: ExpertMetadata,
    ResearchSourceType.ACADEMIC: AcademicMetadata,
    ResearchSourceType.GOVERNMENT: GovernmentMetadata,
    ResearchSourceType.NEWS: NewsMetadata,
    ResearchSourceType.PUBLIC_OPINION: PublicOpinionMetadata,
    ResearchSourceType.POLITICIAN: PoliticianMetadata,
    ResearchSourceType.INDUSTRY: IndustryMetadata,
}


def _correlate_source_metadata_schema(schema: dict[str, Any]) -> None:
    """Expose the source-type/metadata contract to Structured Output validators."""

    title = schema.get("title", "ResearchSource")
    metadata_options = schema["properties"]["source_specific_metadata"]["anyOf"]
    metadata_refs = {
        metadata_model.__name__: deepcopy(
            next(
                option
                for option in metadata_options
                if metadata_model.__name__ in option.get("$ref", "")
            )
        )
        for metadata_model in SOURCE_METADATA_MODELS.values()
    }
    branches: list[dict[str, Any]] = []
    for source_type, metadata_model in SOURCE_METADATA_MODELS.items():
        branch = deepcopy(schema)
        branch.pop("title", None)
        branch["properties"]["source_type"] = {
            "const": source_type.value,
            "title": "Source Type",
            "type": "string",
        }
        branch["properties"]["source_specific_metadata"] = metadata_refs[
            metadata_model.__name__
        ]
        branches.append(branch)

    schema.clear()
    schema.update({"anyOf": branches, "title": title})


class ResearchSource(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
        json_schema_extra=_correlate_source_metadata_schema,
    )

    source_id: SourceId
    evidence_id: EvidenceId
    research_question_ids: list[str] = Field(min_length=1)
    source_type: ResearchSourceType
    title: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    url: HttpUrl
    author_or_organization: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    summary: str = Field(min_length=1)
    relevant_excerpt: str | None = None
    stance: EvidenceStance = EvidenceStance.UNKNOWN
    reliability: ReliabilityLevel = ReliabilityLevel.UNKNOWN
    directness: EvidenceDirectness = EvidenceDirectness.UNKNOWN
    primary_source: bool
    geographic_scope: list[str] = Field(default_factory=list)
    time_scope: str | None = None
    limitations: list[str] = Field(default_factory=list)
    source_specific_metadata: SourceSpecificMetadata

    @field_validator("research_question_ids")
    @classmethod
    def unique_question_ids(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("research_question_ids cannot contain empty values")
        return list(dict.fromkeys(value))

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("source timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def validate_category_metadata(self) -> "ResearchSource":
        source_type = ResearchSourceType(self.source_type)
        required = REQUIRED_METADATA[source_type]
        missing = sorted(required - self.source_specific_metadata.keys())
        if missing:
            raise ValueError(
                f"{self.source_type} source_specific_metadata is missing: {', '.join(missing)}"
            )
        invalid_identity = sorted(
            field_name
            for field_name in REQUIRED_IDENTITY_METADATA[source_type]
            if (
                (
                    self.source_specific_metadata.get(field_name) is None
                    and field_name
                    not in NULLABLE_UNVERIFIED_IDENTITY_METADATA.get(
                        source_type,
                        set(),
                    )
                )
                or (
                    isinstance(self.source_specific_metadata.get(field_name), str)
                    and (
                        not self.source_specific_metadata[field_name].strip()
                        or self.source_specific_metadata[field_name].strip().lower()
                        in PLACEHOLDER_IDENTITY_VALUES
                    )
                )
                or (
                    self.source_specific_metadata.get(field_name) is not None
                    and not isinstance(
                        self.source_specific_metadata.get(field_name),
                        str,
                    )
                )
            )
        )
        if invalid_identity:
            raise ValueError(
                f"{self.source_type} source identity metadata is blank or placeholder: "
                + ", ".join(invalid_identity)
            )
        return self
