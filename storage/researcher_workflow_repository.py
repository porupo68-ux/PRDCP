from __future__ import annotations

from pathlib import Path

from common.models.pmp import PMPMessage
from researcher.schemas.research_report import ResearchReport
from researcher.state import ResearcherWorkflowState
from storage.json_repository import JsonRepository


class ResearcherWorkflowRepository(JsonRepository):
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.workflows_dir = data_dir / "workflows" / "researcher"
        self.producer_outbox_dir = data_dir / "outbox" / "researcher"
        self.deliberation_outbox_dir = data_dir / "outbox" / "deliberation"
        self.reports_dir = data_dir / "artifacts" / "research_reports"
        for directory in (
            self.workflows_dir,
            self.producer_outbox_dir,
            self.deliberation_outbox_dir,
            self.reports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def save(self, state: ResearcherWorkflowState) -> None:
        state.touch()
        state_path = self.workflows_dir / f"{state.workflow_id}.json"
        messages_path = self.workflows_dir / f"{state.workflow_id}.messages.jsonl"
        self.write_json_atomic(state_path, state.model_dump(mode="json"))
        self.write_text_atomic(
            messages_path,
            "".join(message.to_json(indent=None) + "\n" for message in state.message_history),
        )

    def load(self, workflow_id: str) -> ResearcherWorkflowState:
        path = self.workflows_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Researcher workflow not found: {workflow_id}")
        return ResearcherWorkflowState.model_validate(self.read_json(path))

    def load_producer_handoff(self, workflow_id: str) -> PMPMessage:
        path = self.producer_outbox_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Producer handoff not found: {workflow_id}")
        return PMPMessage.model_validate(self.read_json(path))

    def save_report(self, report: ResearchReport) -> Path:
        path = self.reports_dir / f"{report.workflow_id}.json"
        self.write_json_atomic(path, report.model_dump(mode="json"))
        return path

    def load_report(self, workflow_id: str) -> ResearchReport:
        path = self.reports_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Research Report not found: {workflow_id}")
        return ResearchReport.model_validate(self.read_json(path))

    def save_deliberation_outbox(self, message: PMPMessage) -> Path:
        path = self.deliberation_outbox_dir / f"{message.workflow_id}.json"
        self.write_json_atomic(path, message.model_dump(mode="json"))
        return path
