from __future__ import annotations

from common.models.errors import PRDCPError


class RoleDefinitionError(PRDCPError):
    """Base error for deterministic Role Definition failures."""

    error_code = "ROLE_DEFINITION_LOAD_FAILED"

    def __init__(self, message: str, *, agent_id: str | None = None) -> None:
        super().__init__(message)
        self.agent_id = agent_id


class RoleDefinitionNotFoundError(RoleDefinitionError):
    error_code = "ROLE_DEFINITION_NOT_FOUND"


class RoleDefinitionInvalidJSONError(RoleDefinitionError):
    error_code = "ROLE_DEFINITION_INVALID_JSON"


class RoleDefinitionValidationError(RoleDefinitionError):
    error_code = "ROLE_DEFINITION_SCHEMA_INVALID"


class RoleDefinitionAgentIDMismatchError(RoleDefinitionValidationError):
    error_code = "ROLE_DEFINITION_AGENT_ID_MISMATCH"


class RoleDefinitionVersionUnsupportedError(RoleDefinitionValidationError):
    error_code = "ROLE_DEFINITION_VERSION_UNSUPPORTED"


class RoleDefinitionDuplicateAgentIDError(RoleDefinitionValidationError):
    error_code = "ROLE_DEFINITION_DUPLICATE_AGENT_ID"


class RoleDefinitionSectionNotFoundError(RoleDefinitionError):
    error_code = "ROLE_DEFINITION_SECTION_NOT_FOUND"


class RoleDefinitionHashError(RoleDefinitionError):
    error_code = "ROLE_DEFINITION_HASH_FAILED"


class RoleDefinitionCacheError(RoleDefinitionError):
    error_code = "ROLE_DEFINITION_CACHE_FAILED"


class RoleBoundaryViolationError(RoleDefinitionError):
    error_code = "ROLE_BOUNDARY_VIOLATION"

    def __init__(
        self,
        message: str,
        *,
        agent_id: str | None = None,
        requested_action: str | None = None,
        violated_rule: str | None = None,
    ) -> None:
        super().__init__(message, agent_id=agent_id)
        self.requested_action = requested_action
        self.violated_rule = violated_rule
