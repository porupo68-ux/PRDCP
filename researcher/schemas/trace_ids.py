from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Any

from pydantic import Field


EVIDENCE_ID_PREFIX = "evidence_"
SOURCE_ID_PREFIX = "source_"

EvidenceId = Annotated[
    str,
    Field(min_length=len(EVIDENCE_ID_PREFIX) + 1, pattern=r"^evidence_.+"),
]
SourceId = Annotated[
    str,
    Field(min_length=len(SOURCE_ID_PREFIX) + 1, pattern=r"^source_.+"),
]


def canonicalize_legacy_evidence_id(value: str) -> str:
    return _canonicalize_legacy_id(value, EVIDENCE_ID_PREFIX)


def canonicalize_legacy_source_id(value: str) -> str:
    return _canonicalize_legacy_id(value, SOURCE_ID_PREFIX)


def _canonicalize_legacy_id(value: str, prefix: str) -> str:
    """Map a legacy identifier into a stable namespace without editing its source file."""

    if value.startswith(prefix) and len(value) > len(prefix):
        return value
    digest = sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}legacy_{digest}"


def canonicalize_legacy_trace_ids(value: Any) -> Any:
    """Return an in-memory compatibility view of legacy evidence/source references.

    The conversion is deliberately opt-in. Provider output models still reject legacy
    prefixes, while managers may apply this function when reading an already persisted
    artifact or provider error payload created under the former contract.
    """

    return _walk(value, parent_key=None)


def _walk(value: Any, *, parent_key: str | None) -> Any:
    if isinstance(value, dict):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"source_perspectives", "sources_by_category"} and isinstance(
                item, dict
            ):
                converted[key] = {
                    category: [
                        canonicalize_legacy_source_id(entry)
                        if isinstance(entry, str)
                        else _walk(entry, parent_key=key)
                        for entry in references
                    ]
                    if isinstance(references, list)
                    else _walk(references, parent_key=key)
                    for category, references in item.items()
                }
            elif _is_evidence_id_key(key) and isinstance(item, str):
                converted[key] = canonicalize_legacy_evidence_id(item)
            elif _is_source_id_key(key) and isinstance(item, str):
                converted[key] = canonicalize_legacy_source_id(item)
            elif _is_evidence_ids_key(key) and isinstance(item, list):
                converted[key] = [
                    canonicalize_legacy_evidence_id(entry)
                    if isinstance(entry, str)
                    else _walk(entry, parent_key=key)
                    for entry in item
                ]
            elif _is_source_ids_key(key) and isinstance(item, list):
                converted[key] = [
                    canonicalize_legacy_source_id(entry)
                    if isinstance(entry, str)
                    else _walk(entry, parent_key=key)
                    for entry in item
                ]
            else:
                converted[key] = _walk(item, parent_key=key)
        return converted
    if isinstance(value, list):
        if parent_key in {"source_perspectives", "sources_by_category"}:
            return [
                canonicalize_legacy_source_id(item) if isinstance(item, str) else item
                for item in value
            ]
        return [_walk(item, parent_key=parent_key) for item in value]
    return value


def _is_evidence_id_key(key: str) -> bool:
    return key == "evidence_id" or key.endswith("_evidence_id")


def _is_source_id_key(key: str) -> bool:
    return key == "source_id" or key.endswith("_source_id")


def _is_evidence_ids_key(key: str) -> bool:
    return key == "evidence_ids" or key.endswith("_evidence_ids")


def _is_source_ids_key(key: str) -> bool:
    return key == "source_ids" or key.endswith("_source_ids")
