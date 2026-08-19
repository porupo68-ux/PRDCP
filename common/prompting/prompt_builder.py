from __future__ import annotations

import json

from common.role_definitions.models import RoleContext


def _bullet_lines(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) if values else "- なし"


class PromptBuilder:
    """Builds the system prompt in the precedence order defined by RD Loader v1."""

    def build(
        self,
        *,
        common_rules: str,
        role_context: RoleContext,
        agent_prompt: str,
        task_constraints: dict,
        output_schema: dict,
        reviewer_context: dict | None = None,
    ) -> str:
        sections = [
            common_rules.strip(),
            f"# Agent Identity\n- agent_id: {role_context.agent_id}\n- display_name: {role_context.display_name}\n- description: {role_context.description}",
            f"# Mission\n{role_context.mission}",
            f"# Responsibilities\n{_bullet_lines(role_context.responsibilities)}",
            f"# Responsibility Boundaries\n{_bullet_lines(role_context.responsibility_boundaries)}",
            f"# Decision Rules\n{_bullet_lines(role_context.decision_rules)}",
            f"# Constraints\n{_bullet_lines(role_context.constraints)}",
            f"# Prohibited Actions\n{_bullet_lines(role_context.prohibited_actions)}",
            "# Task Constraints\n"
            + json.dumps(task_constraints, ensure_ascii=False, indent=2, sort_keys=True),
            f"# Agent-specific Prompt\n{agent_prompt.strip()}",
            f"# Success Definition\n{role_context.success_definition}",
            f"# Failure Conditions\n{_bullet_lines(role_context.failure_conditions)}",
            f"# Output Requirements\n{_bullet_lines(role_context.output_requirements)}",
            f"# Revision Rules\n{_bullet_lines(role_context.revision_rules)}",
            f"# Uncertainty Rules\n{_bullet_lines(role_context.uncertainty_rules)}",
        ]
        if reviewer_context:
            sections.append(
                "# Review Target Role Boundaries\n"
                + json.dumps(reviewer_context, ensure_ascii=False, indent=2, sort_keys=True)
            )
        # OpenRouter receives this already-validated schema in response_format.
        # Repeating the entire schema in the system message consumes context twice
        # and does not add a second enforcement boundary. Keep the precedence
        # section, but make the API-level contract authoritative.
        sections.append(
            "# Output Schema\n"
            "Return exactly one JSON object that conforms to the strict JSON Schema "
            "supplied by the runtime in response_format. Include every required key, "
            "use empty arrays for required collections with no items, and add no "
            "undeclared properties."
        )
        return "\n\n".join(sections).strip() + "\n"
