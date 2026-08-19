from __future__ import annotations

from pydantic import BaseModel

from common.agents import StructuredAgent
from common.models.errors import PayloadValidationError
from conclusion.schemas.decision_context import DecisionContext
from conclusion.validator import ConclusionValidator


class ConclusionAgent(StructuredAgent):
    """Conclusion adapter for the shared structured-agent pipeline."""

    prompt_layer = "conclusion"
    manager_agent_id = "conclusion.manager"

    def validate_output_contract(
        self,
        input_payload: BaseModel,
        output_payload: BaseModel,
        *,
        provider_input: dict | None = None,
    ) -> BaseModel:
        del provider_input
        context = getattr(input_payload, "decision_context", None)
        if not isinstance(context, DecisionContext):
            return output_payload

        input_candidates = getattr(input_payload, "position_candidates", None)
        if input_candidates is None:
            output_candidates = getattr(output_payload, "position_candidates", [])
            candidate_ids = {
                item.position_candidate_id for item in output_candidates
            }
        else:
            candidate_ids = {
                item.position_candidate_id for item in input_candidates
            }
        violations = ConclusionValidator.unknown_reference_ids(
            decision_context=context,
            value=output_payload,
            candidate_ids=candidate_ids,
        )
        if not violations:
            return output_payload

        validation_errors = [
            {
                "type": "value_error.reference_integrity",
                "loc": item["path"],
                "msg": (
                    f"Unknown {item['kind']} reference {item['id']!r}; "
                    "copy an exact ID from the request canonical allowlist"
                ),
                "input": item["id"],
            }
            for item in violations
        ]
        summary = "; ".join(
            f"{item['path']}={item['id']}" for item in violations
        )
        raise PayloadValidationError(
            "Conclusion output reference-integrity contract failed: " + summary,
            invalid_payload=output_payload.model_dump(mode="json"),
            validation_errors=validation_errors,
        )
