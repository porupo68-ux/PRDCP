from deliberation.agents.base import DeliberationAgent
from deliberation.schemas.analysis_task import DeliberationAnalysisTask
from deliberation.schemas.stakeholder_response_analysis import StakeholderResponseAnalysisResult


class StakeholderResponseAnalyst(DeliberationAgent):
    agent_id = "deliberation.stakeholder_response_analyst"
    input_schema = DeliberationAnalysisTask
    output_schema = StakeholderResponseAnalysisResult

    def normalize_provider_output(
        self,
        raw: dict,
        *,
        provider_input: dict | None = None,
    ) -> dict:
        """Hydrate immutable source lineage from the selected Evidence IDs."""

        if not isinstance(provider_input, dict):
            return raw
        context = provider_input.get("evidence_context")
        if not isinstance(context, list):
            return raw
        source_by_evidence = {
            item["evidence_id"]: item["source_id"]
            for item in context
            if isinstance(item, dict)
            and isinstance(item.get("evidence_id"), str)
            and isinstance(item.get("source_id"), str)
        }
        normalized = dict(raw)
        normalized_facts = []
        for raw_fact in raw.get("specific_facts", []):
            if not isinstance(raw_fact, dict):
                normalized_facts.append(raw_fact)
                continue
            fact = dict(raw_fact)
            evidence_ids = [
                item
                for item in fact.get("evidence_ids", [])
                if isinstance(item, str) and item in source_by_evidence
            ]
            fact["source_ids"] = list(
                dict.fromkeys(source_by_evidence[item] for item in evidence_ids)
            )
            normalized_facts.append(fact)
        normalized["specific_facts"] = normalized_facts
        return normalized
