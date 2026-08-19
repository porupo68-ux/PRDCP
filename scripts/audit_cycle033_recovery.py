from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.runtime_models import audit_runtime_models
from config.settings import Settings
from runtime import build_researcher_manager


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow_id")
    args = parser.parse_args()
    settings = Settings.from_env(refresh_dotenv=True)
    manager = build_researcher_manager(settings)
    audit = manager.inspect_retrieval_reconstruction(args.workflow_id)
    runtime = audit_runtime_models(settings, (manager,)).for_layer("researcher")
    action_counts = {
        action: sum(item["action"] == action for item in audit["tasks"])
        for action in sorted({item["action"] for item in audit["tasks"]})
    }
    print(
        json.dumps(
            {
                "workflow_id": args.workflow_id,
                "state_status": audit["state_status"],
                "provider": settings.provider,
                "retrieval_provider": settings.retrieval_provider,
                "demo_safe_mode": settings.demo_safe_mode,
                "planned_retrieval_calls": audit["planned_retrieval_calls"],
                "planned_reasoning_calls": audit["planned_reasoning_calls"],
                "planned_quality_review_calls": 1,
                "actions": action_counts,
                "runtime_model_drift": len(runtime.drifted),
                "specialist_models": sorted(
                    {item["runtime_model_id"] for item in audit["tasks"]}
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
