from __future__ import annotations

from typing import Any

from common.models.pmp import PMPMessage
from common.role_definitions.exceptions import RoleBoundaryViolationError
from common.role_definitions.models import RoleDefinitionSnapshot, RoleRuntimeConfig


class RoleBoundaryValidator:
    """Deterministic first stage; semantic boundaries remain explicit in the prompt."""

    ACTION_FIELDS = ("requested_action", "action", "target_output", "task_type")

    def validate(
        self,
        *,
        message: PMPMessage,
        runtime_config: RoleRuntimeConfig,
        snapshot: RoleDefinitionSnapshot,
        expected_output_message_type: str | None = None,
    ) -> None:
        if message.message_type not in runtime_config.accepted_message_types:
            self._violate(
                snapshot,
                requested_action=message.message_type,
                violated_rule="accepted_message_types",
                message=f"{snapshot.agent_id} does not accept {message.message_type}",
            )
        if expected_output_message_type and expected_output_message_type not in runtime_config.generated_message_types:
            self._violate(
                snapshot,
                requested_action=expected_output_message_type,
                violated_rule="generated_message_types",
                message=f"{snapshot.agent_id} may not generate {expected_output_message_type}",
            )
        requested = self._requested_actions(message.payload, message.constraints)
        prohibited = set(runtime_config.prohibited_requested_actions)
        for action in requested:
            if action in prohibited:
                self._violate(
                    snapshot,
                    requested_action=action,
                    violated_rule="runtime_boundary.prohibited_requested_actions",
                    message=f"Requested action {action!r} is outside {snapshot.agent_id}'s role",
                )

    def _requested_actions(self, *objects: dict[str, Any]) -> set[str]:
        actions: set[str] = set()
        for value in objects:
            for field in self.ACTION_FIELDS:
                candidate = value.get(field)
                if isinstance(candidate, str):
                    actions.add(candidate.strip().lower())
        return actions

    @staticmethod
    def _violate(
        snapshot: RoleDefinitionSnapshot,
        *,
        requested_action: str,
        violated_rule: str,
        message: str,
    ) -> None:
        raise RoleBoundaryViolationError(
            message,
            agent_id=snapshot.agent_id,
            requested_action=requested_action,
            violated_rule=violated_rule,
        )
