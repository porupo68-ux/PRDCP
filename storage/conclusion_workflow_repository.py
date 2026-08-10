from __future__ import annotations

from pathlib import Path

from common.models.pmp import PMPMessage
from conclusion.schemas.conclusion_package import ConclusionPackage
from conclusion.schemas.final_conclusion import FinalConclusion
from conclusion.state import ConclusionWorkflowState
from storage.json_repository import JsonRepository


class ConclusionWorkflowRepository(JsonRepository):
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.workflows_dir = data_dir / "workflows" / "conclusion"
        self.deliberation_outbox_dir = data_dir / "outbox" / "conclusion"
        self.packages_dir = data_dir / "artifacts" / "conclusion_packages"
        self.final_conclusions_dir = data_dir / "artifacts" / "final_conclusions"
        self.playwright_outbox_dir = data_dir / "outbox" / "playwright"
        self.deliberation_revision_outbox_dir = data_dir / "outbox" / "deliberation_revision"
        for directory in (
            self.workflows_dir,
            self.deliberation_outbox_dir,
            self.packages_dir,
            self.final_conclusions_dir,
            self.playwright_outbox_dir,
            self.deliberation_revision_outbox_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def save(self, state: ConclusionWorkflowState) -> None:
        state.touch()
        self.write_json_atomic(self.workflows_dir / f"{state.workflow_id}.json", state.model_dump(mode="json"))
        self.write_text_atomic(
            self.workflows_dir / f"{state.workflow_id}.messages.jsonl",
            "".join(message.to_json(indent=None) + "\n" for message in state.message_history),
        )

    def load(self, workflow_id: str) -> ConclusionWorkflowState:
        path = self.workflows_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Conclusion workflow not found: {workflow_id}")
        return ConclusionWorkflowState.model_validate(self.read_json(path))

    def load_deliberation_handoff(self, workflow_id: str) -> PMPMessage:
        path = self.deliberation_outbox_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Deliberation handoff not found: {workflow_id}")
        return PMPMessage.model_validate(self.read_json(path))

    def save_package(self, package: ConclusionPackage) -> Path:
        path = self.packages_dir / f"{package.workflow_id}.json"
        self.write_json_atomic(path, package.model_dump(mode="json"))
        return path

    def load_package(self, workflow_id: str) -> ConclusionPackage:
        path = self.packages_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Conclusion Package not found: {workflow_id}")
        return ConclusionPackage.model_validate(self.read_json(path))

    def save_final_conclusion(self, conclusion: FinalConclusion) -> Path:
        path = self.final_conclusions_dir / f"{conclusion.workflow_id}.json"
        self.write_json_atomic(path, conclusion.model_dump(mode="json"))
        return path

    def load_final_conclusion(self, workflow_id: str) -> FinalConclusion:
        path = self.final_conclusions_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Final Conclusion not found: {workflow_id}")
        return FinalConclusion.model_validate(self.read_json(path))

    def save_playwright_outbox(self, message: PMPMessage) -> Path:
        path = self.playwright_outbox_dir / f"{message.workflow_id}.json"
        self.write_json_atomic(path, message.model_dump(mode="json"))
        return path

    def save_deliberation_revision_outbox(self, message: PMPMessage) -> Path:
        path = self.deliberation_revision_outbox_dir / f"{message.workflow_id}.json"
        self.write_json_atomic(path, message.model_dump(mode="json"))
        return path
