from __future__ import annotations

import json
import os
from pathlib import Path

from common.models.pmp import PMPMessage
from researcher.schemas.research_report import ResearchReport
from researcher.schemas.human_evidence import HumanEvidenceDecisionArtifact
from researcher.state import ResearcherWorkflowState
from storage.json_repository import JsonRepository


class ResearcherWorkflowRepository(JsonRepository):
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.workflows_dir = data_dir / "workflows" / "researcher"
        self.producer_outbox_dir = data_dir / "outbox" / "researcher"
        self.deliberation_outbox_dir = data_dir / "outbox" / "deliberation"
        self.researcher_revision_inbox_dir = data_dir / "outbox" / "researcher_revision"
        self.reports_dir = data_dir / "artifacts" / "research_reports"
        self.human_evidence_decisions_dir = (
            data_dir / "artifacts" / "human_evidence_decisions"
        )
        for directory in (
            self.workflows_dir,
            self.producer_outbox_dir,
            self.deliberation_outbox_dir,
            self.researcher_revision_inbox_dir,
            self.reports_dir,
            self.human_evidence_decisions_dir,
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

    def load_revision_request(self, workflow_id: str) -> PMPMessage:
        path = self.researcher_revision_inbox_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Researcher revision request not found: {workflow_id}")
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

    def load_deliberation_outbox(self, workflow_id: str) -> PMPMessage:
        path = self.deliberation_outbox_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Deliberation outbox not found: {workflow_id}")
        return PMPMessage.model_validate(self.read_json(path))

    def create_human_evidence_decision_once(
        self,
        artifact: HumanEvidenceDecisionArtifact,
    ) -> Path:
        """Atomically claim one human decision identity across CLI/Discord processes."""

        path = (
            self.human_evidence_decisions_dir
            / artifact.decision.workflow_id
            / f"{artifact.decision.quality_review_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ValueError(
                "Human evidence decision already exists for this Quality Review"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    artifact.model_dump(mode="json"),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                if os.name != "nt":
                    os.fsync(handle.fileno())
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return path

    def load_human_evidence_decision(
        self,
        workflow_id: str,
        quality_review_id: str,
    ) -> HumanEvidenceDecisionArtifact:
        path = (
            self.human_evidence_decisions_dir
            / workflow_id
            / f"{quality_review_id}.json"
        )
        if not path.exists():
            raise FileNotFoundError(
                f"Human evidence decision not found: {workflow_id}/{quality_review_id}"
            )
        return HumanEvidenceDecisionArtifact.model_validate(self.read_json(path))

    def list_human_evidence_decisions(
        self,
        workflow_id: str,
    ) -> list[HumanEvidenceDecisionArtifact]:
        directory = self.human_evidence_decisions_dir / workflow_id
        if not directory.exists():
            return []
        return [
            HumanEvidenceDecisionArtifact.model_validate(self.read_json(path))
            for path in sorted(directory.glob("*.json"))
        ]
