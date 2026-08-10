from __future__ import annotations

from pathlib import Path

from common.models.pmp import PMPMessage
from producer.state import ProducerWorkflowState
from storage.json_repository import JsonRepository


class ProducerWorkflowRepository(JsonRepository):
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.workflows_dir = data_dir / "workflows" / "producer"
        self.legacy_workflows_dir = data_dir / "workflows"
        self.researcher_outbox_dir = data_dir / "outbox" / "researcher"
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.researcher_outbox_dir.mkdir(parents=True, exist_ok=True)

    def save(self, state: ProducerWorkflowState) -> None:
        state.touch()
        state_path = self.workflows_dir / f"{state.workflow_id}.json"
        messages_path = self.workflows_dir / f"{state.workflow_id}.messages.jsonl"
        self.write_json_atomic(state_path, state.model_dump(mode="json"))
        self.write_text_atomic(
            messages_path,
            "".join(message.to_json(indent=None) + "\n" for message in state.message_history),
        )

    def load(self, workflow_id: str) -> ProducerWorkflowState:
        path = self.workflows_dir / f"{workflow_id}.json"
        if not path.exists():
            path = self.legacy_workflows_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Workflow not found: {workflow_id}")
        return ProducerWorkflowState.model_validate(self.read_json(path))

    def save_researcher_outbox(self, message: PMPMessage) -> Path:
        path = self.researcher_outbox_dir / f"{message.workflow_id}.json"
        self.write_json_atomic(path, message.model_dump(mode="json"))
        return path


# Backward-compatible name used by the Producer v1 public API.
WorkflowRepository = ProducerWorkflowRepository
