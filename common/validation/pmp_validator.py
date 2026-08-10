from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from common.models.errors import PMPValidationError
from common.models.pmp import PMPMessage
from config.settings import BASE_DIR


class PMPValidator:
    def __init__(self, agent_registry_path: Path | None = None) -> None:
        path = agent_registry_path or BASE_DIR / "config" / "agents.json"
        self.agent_ids = set(json.loads(path.read_text(encoding="utf-8")))
        # Canonical Common registry defines this non-agent delivery endpoint.
        self.agent_ids.add("system.final_output")

    def validate(self, message: PMPMessage | dict) -> PMPMessage:
        try:
            validated = message if isinstance(message, PMPMessage) else PMPMessage.model_validate(message)
        except ValidationError as exc:
            raise PMPValidationError(str(exc)) from exc
        for field_name, agent_id in (
            ("sender_agent_id", validated.sender_agent_id),
            ("receiver_agent_id", validated.receiver_agent_id),
        ):
            if agent_id not in self.agent_ids:
                raise PMPValidationError(f"Unknown {field_name}: {agent_id}")
        if validated.routing.revision_target is not None and validated.routing.revision_target not in self.agent_ids:
            raise PMPValidationError(
                f"Unknown routing.revision_target: {validated.routing.revision_target}"
            )
        return validated
