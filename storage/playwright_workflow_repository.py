from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.models.pmp import PMPMessage
from playwright.schemas import (
    CitationManifest,
    FinalScriptPackage,
    NarrativeBlueprint,
    ScriptDraft,
    VisualPlan,
)
from playwright.state import PlaywrightWorkflowState
from storage.json_repository import JsonRepository


class PlaywrightWorkflowRepository(JsonRepository):
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.workflows_dir = data_dir / "workflows" / "playwright"
        self.conclusion_outbox_dir = data_dir / "outbox" / "playwright"
        self.conclusion_revision_outbox_dir = data_dir / "outbox" / "conclusion_revision"
        self.narrative_dir = data_dir / "artifacts" / "narrative_blueprints"
        self.script_dir = data_dir / "artifacts" / "script_drafts"
        self.citation_dir = data_dir / "artifacts" / "citation_manifests"
        self.visual_dir = data_dir / "artifacts" / "visual_plans"
        self.package_dir = data_dir / "artifacts" / "final_script_packages"
        self.deliveries_dir = data_dir / "deliveries"
        for directory in (
            self.workflows_dir,
            self.conclusion_outbox_dir,
            self.conclusion_revision_outbox_dir,
            self.narrative_dir,
            self.script_dir,
            self.citation_dir,
            self.visual_dir,
            self.package_dir,
            self.deliveries_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def save(self, state: PlaywrightWorkflowState) -> None:
        state.touch()
        self.write_json_atomic(self.workflows_dir / f"{state.workflow_id}.json", state.model_dump(mode="json"))
        self.write_text_atomic(
            self.workflows_dir / f"{state.workflow_id}.messages.jsonl",
            "".join(message.to_json(indent=None) + "\n" for message in state.message_history),
        )

    def load(self, workflow_id: str) -> PlaywrightWorkflowState:
        path = self.workflows_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Playwright workflow not found: {workflow_id}")
        return PlaywrightWorkflowState.model_validate(self.read_json(path))

    def load_conclusion_handoff(self, workflow_id: str) -> PMPMessage:
        path = self.conclusion_outbox_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Conclusion handoff not found: {workflow_id}")
        return PMPMessage.model_validate(self.read_json(path))

    def save_narrative(self, artifact: NarrativeBlueprint, workflow_id: str) -> Path:
        return self._save_model(self.narrative_dir / f"{workflow_id}.json", artifact)

    def save_script(self, artifact: ScriptDraft, workflow_id: str) -> Path:
        return self._save_model(self.script_dir / f"{workflow_id}.json", artifact)

    def save_citation_manifest(self, artifact: CitationManifest, workflow_id: str) -> Path:
        return self._save_model(self.citation_dir / f"{workflow_id}.json", artifact)

    def save_visual_plan(self, artifact: VisualPlan, workflow_id: str) -> Path:
        return self._save_model(self.visual_dir / f"{workflow_id}.json", artifact)

    def save_final_package(self, artifact: FinalScriptPackage) -> Path:
        return self._save_model(self.package_dir / f"{artifact.workflow_id}.json", artifact)

    def load_final_package(self, workflow_id: str) -> FinalScriptPackage:
        path = self.package_dir / f"{workflow_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Final Script Package not found: {workflow_id}")
        return FinalScriptPackage.model_validate(self.read_json(path))

    def save_conclusion_revision_outbox(self, message: PMPMessage) -> Path:
        path = self.conclusion_revision_outbox_dir / f"{message.workflow_id}.json"
        self.write_json_atomic(path, message.model_dump(mode="json"))
        return path

    def save_deliveries(self, package: FinalScriptPackage) -> dict[str, str]:
        target = self.deliveries_dir / package.workflow_id
        target.mkdir(parents=True, exist_ok=True)
        paths = {
            "final_script_package": target / "final_script_package.json",
            "script": target / "script.md",
            "citation_manifest": target / "citation_manifest.json",
            "source_list": target / "source_list.md",
            "visual_plan": target / "visual_plan.md",
            "production_notes": target / "production_notes.md",
        }
        self.write_json_atomic(paths["final_script_package"], package.model_dump(mode="json"))
        self.write_json_atomic(paths["citation_manifest"], package.citation_manifest.model_dump(mode="json"))
        self.write_text_atomic(paths["script"], self._script_markdown(package))
        self.write_text_atomic(paths["source_list"], self._source_markdown(package))
        self.write_text_atomic(paths["visual_plan"], self._visual_markdown(package))
        self.write_text_atomic(paths["production_notes"], self._notes_markdown(package))
        return {key: str(path) for key, path in paths.items()}

    def _save_model(self, path: Path, artifact) -> Path:
        self.write_json_atomic(path, artifact.model_dump(mode="json"))
        return path

    @staticmethod
    def _script_markdown(package: FinalScriptPackage) -> str:
        lines = [f"# {package.title_candidates[0]}", ""]
        for section in package.script.sections:
            lines.extend([f"## {section.heading}", ""])
            for paragraph in section.paragraphs:
                lines.extend([paragraph.speaker_text, ""])
            if section.transition_text:
                lines.extend([f"_Transition: {section.transition_text}_", ""])
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _source_markdown(package: FinalScriptPackage) -> str:
        lines = ["# Source List", ""]
        for item in package.citation_manifest.source_list:
            source_id = item.get("source_id", "unknown")
            title = item.get("title") or item.get("publisher") or item.get("url") or "Untitled source"
            url = item.get("url")
            lines.append(f"- `{source_id}`: [{title}]({url})" if url else f"- `{source_id}`: {title}")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _visual_markdown(package: FinalScriptPackage) -> str:
        lines = ["# Visual Plan", ""]
        for cue in package.visual_plan.visual_cues:
            lines.extend(
                [
                    f"## {cue.section_id} / {cue.paragraph_id}",
                    "",
                    f"- Type: {cue.visual_type}",
                    f"- Direction: {cue.description}",
                    f"- On-screen text: {cue.on_screen_text or '-'}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _notes_markdown(package: FinalScriptPackage) -> str:
        return (
            "# Production Notes\n\n"
            f"```json\n{json.dumps(package.production_summary, ensure_ascii=False, indent=2)}\n```\n\n"
            "## Limitations to disclose\n\n"
            + "\n".join(f"- {item}" for item in package.limitations_to_disclose)
            + "\n"
        )

