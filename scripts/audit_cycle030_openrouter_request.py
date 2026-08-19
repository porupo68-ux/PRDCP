from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterator


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from common.models.pmp import PMPMessage
from common.prompting import PRDCP_COMMON_RULES, PromptBuilder
from common.role_definitions import RoleDefinitionLoader
from common.role_definitions import RoleDefinitionExtractor
from common.role_definitions.agent_runtime import (
    prepare_agent_execution,
    specialize_agent_execution_prompt,
)
from common.structured_outputs import (
    normalize_strict_output_schema,
    strict_output_schema,
    validate_strict_output_schema,
)
from producer.schemas.general_opinion import GeneralOpinionInput, GeneralOpinionOutput
from providers.openrouter_provider import OpenRouterModelProvider


AGENT_ID = "producer.general_opinion_analyst"
SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "openrouter_api_key",
    "discord_bot_token",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct the Cycle 030 General Opinion OpenRouter request without "
            "performing Retrieval or Provider calls"
        )
    )
    parser.add_argument("workflow_id")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_DIR / "storage" / "data",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def digest(value: Any) -> dict[str, Any]:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "utf8_bytes": len(encoded)}


def walk(value: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{path}/{index}")


def enum_audit(schema: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for path, value in walk(schema):
        if not isinstance(value, dict) or not isinstance(value.get("enum"), list):
            continue
        literals = value["enum"]
        sizes = [
            len(item.encode("utf-8")) for item in literals if isinstance(item, str)
        ]
        result.append(
            {
                "path": path + "/enum",
                "count": len(literals),
                "largest_string_utf8_bytes": max(sizes, default=0),
                "total_string_utf8_bytes": sum(sizes),
            }
        )
    return result


def reconstruct(workflow_id: str, data_dir: Path) -> dict[str, Any]:
    workflow_path = data_dir / "workflows" / "producer" / f"{workflow_id}.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    messages = [PMPMessage.model_validate(item) for item in workflow["message_history"]]
    request = next(
        message
        for message in reversed(messages)
        if message.sender_agent_id == "producer.manager"
        and message.receiver_agent_id == AGENT_ID
        and message.message_type == "task"
    )
    context_paths = sorted((data_dir / "retrieval_contexts" / workflow_id).glob("*.json"))
    contexts = [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in context_paths
    ]
    context_path, context = next(
        (path, item) for path, item in contexts if item.get("agent_id") == AGENT_ID
    )
    reservation_path = (
        data_dir
        / "provider_call_reservations"
        / "openrouter"
        / workflow_id
        / f"{AGENT_ID}.json"
    )
    reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
    model = str(reservation["model_id"])

    provider_input = GeneralOpinionInput.model_validate(request.payload).model_dump(
        mode="json"
    )
    provider_input["retrieval_context"] = context

    pydantic_schema = GeneralOpinionOutput.model_json_schema()
    normalized = normalize_strict_output_schema(pydantic_schema)
    specialized = GeneralOpinionOutput.specialize_strict_output_schema(
        deepcopy(normalized),
        provider_input,
    )
    validate_strict_output_schema(specialized, schema_name="GeneralOpinionOutput")
    final_schema = strict_output_schema(
        GeneralOpinionOutput,
        input_data=provider_input,
    )

    with tempfile.TemporaryDirectory() as temporary:
        rd_loader = RoleDefinitionLoader.from_project(
            PROJECT_DIR,
            access_log_path=Path(temporary) / "rd_access.jsonl",
        )
        execution = prepare_agent_execution(
            loader=rd_loader,
            agent_id=AGENT_ID,
            message=request,
            agent_prompt=(
                PROJECT_DIR / "producer" / "prompts" / "general_opinion_analyst.md"
            ).read_text(encoding="utf-8"),
            output_schema=GeneralOpinionOutput,
            expected_output_message_type="result",
        )
        execution = specialize_agent_execution_prompt(
            execution,
            loader=rd_loader,
            agent_id=AGENT_ID,
            message=request,
            agent_prompt=(
                PROJECT_DIR / "producer" / "prompts" / "general_opinion_analyst.md"
            ).read_text(encoding="utf-8"),
            output_schema=GeneralOpinionOutput,
            input_data=provider_input,
        )
        snapshot = rd_loader.load(AGENT_ID)
        role_context = RoleDefinitionExtractor().extract_llm_context(snapshot)
    body = OpenRouterModelProvider.build_request_body(
        model=model,
        system_prompt=execution.system_prompt,
        input_data=provider_input,
        output_schema=GeneralOpinionOutput,
    )
    # Cycle 029 persisted the exact response_format schema separately while
    # diagnosing the 400.  Rebuild the accompanying pre-repair system prompt
    # from the same RD/prompt and the old unspecialized schema.  No credential
    # or HTTP transport header is involved in either request representation.
    project_root = data_dir.parents[1]
    failed_schema_path = project_root / "general_opinion_real_schema.json"
    failed_schema = json.loads(failed_schema_path.read_text(encoding="utf-8"))
    failed_base_schema = deepcopy(failed_schema)
    failed_supporting = failed_base_schema["$defs"]["SupportingSource"]["properties"]
    failed_supporting["source"] = {
        "minLength": 1,
        "title": "Source",
        "type": "string",
    }
    failed_supporting["url"] = {
        "format": "uri",
        "maxLength": 2083,
        "minLength": 1,
        "title": "Url",
        "type": "string",
    }
    failed_system_prompt = PromptBuilder().build(
        common_rules=PRDCP_COMMON_RULES,
        role_context=role_context,
        agent_prompt=(
            PROJECT_DIR / "producer" / "prompts" / "general_opinion_analyst.md"
        ).read_text(encoding="utf-8"),
        task_constraints=request.constraints,
        output_schema=failed_base_schema,
        reviewer_context=None,
    )
    system_suffix = body["messages"][0]["content"][len(execution.system_prompt) :]
    failed_body = deepcopy(body)
    failed_body["messages"][0]["content"] = failed_system_prompt + system_suffix
    failed_body["response_format"]["json_schema"]["schema"] = failed_schema
    sensitive_paths = [
        path
        for path, value in walk(
            {"failed": failed_body, "repaired": body}
        )
        if path.rsplit("/", 1)[-1].lower() in SENSITIVE_KEYS and value
    ]
    if sensitive_paths:
        raise ValueError(
            "Refusing to persist request audit with sensitive fields: "
            + ", ".join(sensitive_paths)
        )

    return {
        "audit": {
            "cycle": "030",
            "workflow_id": workflow_id,
            "agent_id": AGENT_ID,
            "model": model,
            "retrieval_reused": True,
            "retrieval_provider_calls": 0,
            "reasoning_provider_calls": 0,
            "source_count": len(context.get("sources", [])),
            "retrieval_context_path": str(context_path),
            "retrieval_context_sha256": hashlib.sha256(
                context_path.read_bytes()
            ).hexdigest(),
            "contains_credentials": False,
        },
        "transformation_trace": {
            "pydantic_model_json_schema": digest(pydantic_schema),
            "strict_normalized_schema": digest(normalized),
            "specialized_strict_schema": digest(specialized),
            "final_response_format_schema": digest(final_schema),
            "specialized_equals_final": specialized == final_schema,
            "failed_response_format_schema": digest(failed_schema),
            "failed_enum_audit": enum_audit(failed_schema),
            "repaired_enum_audit": enum_audit(final_schema),
        },
        "failed_request_reconstruction": failed_body,
        "repaired_request": body,
    }


def main() -> int:
    args = parse_args()
    artifact = reconstruct(args.workflow_id, args.data_dir)
    rendered = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
