from __future__ import annotations

import re


ARGUMENT_ANALYSIS_PREFIX = "argument_analysis_"
CAUSAL_ANALYSIS_PREFIX = "causal_analysis_"
STAKEHOLDER_ANALYSIS_PREFIX = "stakeholder_analysis_"
COUNTERARGUMENT_ANALYSIS_PREFIX = "counterargument_analysis_"
INITIAL_INTEGRATION_PREFIX = "integration_initial_"
FINAL_INTEGRATION_PREFIX = "integration_final_"

ANALYSIS_PREFIXES = (
    ARGUMENT_ANALYSIS_PREFIX,
    CAUSAL_ANALYSIS_PREFIX,
    STAKEHOLDER_ANALYSIS_PREFIX,
    COUNTERARGUMENT_ANALYSIS_PREFIX,
)
INTEGRATION_PREFIXES = (INITIAL_INTEGRATION_PREFIX, FINAL_INTEGRATION_PREFIX)
TASK_PREFIXES = ("delib_task_", "counter_task_")
EVIDENCE_PREFIXES = ("evidence_",)
SOURCE_PREFIXES = ("source_",)
CLAIM_PREFIXES = ("claim_", "causal_claim_")
COUNTERARGUMENT_PREFIXES = ("counterargument_", "counter_")
CHALLENGE_PREFIXES = ("challenge_", "steelman_")


def require_identifier_prefix(
    value: str,
    prefixes: tuple[str, ...],
    *,
    field_name: str,
) -> str:
    if not any(value.startswith(prefix) for prefix in prefixes):
        expected = ", ".join(prefixes)
        raise ValueError(f"{field_name} must use one of these prefixes: {expected}")
    return value


def require_identifier_list(
    values: list[str],
    prefixes: tuple[str, ...],
    *,
    field_name: str,
) -> list[str]:
    invalid = sorted(
        value for value in values if not any(value.startswith(prefix) for prefix in prefixes)
    )
    if invalid:
        expected = ", ".join(prefixes)
        raise ValueError(
            f"{field_name} contains IDs outside [{expected}]: {invalid}"
        )
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicate IDs")
    return values


def canonicalize_analysis_id(
    value: str,
    *,
    canonical_prefix: str,
    legacy_prefixes: tuple[str, ...] = (),
) -> str:
    """Normalize known legacy IDs in memory while emitting one new namespace."""

    if value.startswith(canonical_prefix):
        return value
    for legacy_prefix in legacy_prefixes:
        if value.startswith(legacy_prefix):
            suffix = value[len(legacy_prefix) :]
            suffix = re.sub(r"[^a-zA-Z0-9_-]+", "_", suffix).strip("_")
            return f"{canonical_prefix}{suffix or 'legacy'}"
    expected = ", ".join((canonical_prefix, *legacy_prefixes))
    raise ValueError(f"analysis_id has an unsupported namespace; expected {expected}")


def canonicalize_analysis_reference(value: str) -> str:
    mappings = (
        (ARGUMENT_ANALYSIS_PREFIX, ("arg_analysis_", "analysis_argument_")),
        (CAUSAL_ANALYSIS_PREFIX, ("analysis_causal_",)),
        (
            STAKEHOLDER_ANALYSIS_PREFIX,
            ("analysis_stakeholder_", "analysis_task_"),
        ),
        (COUNTERARGUMENT_ANALYSIS_PREFIX, ("analysis_counterargument_",)),
    )
    for canonical, legacy in mappings:
        if value.startswith(canonical) or any(value.startswith(item) for item in legacy):
            return canonicalize_analysis_id(
                value,
                canonical_prefix=canonical,
                legacy_prefixes=legacy,
            )
    return require_identifier_prefix(
        value,
        ANALYSIS_PREFIXES,
        field_name="analysis_ids",
    )
