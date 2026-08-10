from __future__ import annotations

from pathlib import Path

from common.models.pmp import PMPMessage
from deliberation.schemas.deliberation_result import DeliberationResult
from deliberation.state import DeliberationWorkflowState
from storage.json_repository import JsonRepository


class DeliberationWorkflowRepository(JsonRepository):
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.workflows_dir = data_dir / "workflows" / "deliberation"
        self.researcher_outbox_dir = data_dir / "outbox" / "deliberation"
        self.results_dir = data_dir / "artifacts" / "deliberation_results"
        self.conclusion_outbox_dir = data_dir / "outbox" / "conclusion"
        self.researcher_revision_outbox_dir = data_dir / "outbox" / "researcher_revision"
        for directory in (
            self.workflows_dir,
            self.researcher_outbox_dir,
            self.results_dir,
            self.conclusion_outbox_dir,
            self.researcher_revision_outbox_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def save(self, state: DeliberationWorkflowState) -> None:
        state.touch()
        self.write_json_atomic(
            self.workflows_dir / f"{state.workflow_id}.json",
            state.model_dump(mode="json"),
        )
        self.write_text_atomic(
            self.workflows_dir / f"{state.workflow_id}.messages.jsonl",
            "".join(message.to_json(indent=None) + "\n" for message in state.message_history),
        )

    def load(self, workflow_id: str) -> DeliberationWorkflowState:
        path = self.workflows_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Deliberation workflow not found: {workflow_id}")
        return DeliberationWorkflowState.model_validate(self.read_json(path))

    def load_researcher_handoff(self, workflow_id: str) -> PMPMessage:
        path = self.researcher_outbox_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Researcher handoff not found: {workflow_id}")
        return PMPMessage.model_validate(self.read_json(path))

    def save_result(self, result: DeliberationResult) -> Path:
        path = self.results_dir / f"{result.workflow_id}.json"
        self.write_json_atomic(path, result.model_dump(mode="json"))
        return path

    def load_result(self, workflow_id: str) -> DeliberationResult:
        path = self.results_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Deliberation Result not found: {workflow_id}")
        return DeliberationResult.model_validate(self.read_json(path))

    def save_conclusion_outbox(self, message: PMPMessage) -> Path:
        path = self.conclusion_outbox_dir / f"{message.workflow_id}.json"
        self.write_json_atomic(path, message.model_dump(mode="json"))
        return path

    def save_researcher_revision_outbox(self, message: PMPMessage) -> Path:
        path = self.researcher_revision_outbox_dir / f"{message.workflow_id}.json"
        self.write_json_atomic(path, message.model_dump(mode="json"))
        return path
