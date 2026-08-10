# Maintenance Guide

## Before changing code

```powershell
py main.py --doctor
py scripts\verify.py
```

Use a temporary `PRDCP_DATA_DIR` when experimenting so test workflows do not
mix with real runs.

## Where to edit

| Change | Primary location | Required follow-up |
| --- | --- | --- |
| Agent responsibility or prohibition | `role_definitions/<layer>/*.json` | Update RD version; run `--doctor` and RD tests |
| Small wording instruction | `<layer>/prompts/*.md` | Confirm RD still has priority |
| Input/output field | `<layer>/schemas/*.py` | Update fixtures, validator, handoff tests |
| Agent order or dependency | `<layer>/workflow.py` and `manager.py` | Test normal, revision, failure, and resume paths |
| Model assignment | `config/models.json` and `.env` | Run `--doctor` with the real provider selected |
| PMP value or handoff | `specifications/common/*.json` first | Update runtime enum/validator and drift tests together |
| New provider | `providers/` and `runtime.py` | Implement `ModelProvider`; add retry/error tests |
| Storage path or artifact | `storage/` | Preserve atomic writes and restart tests |
| CLI or operator message | `cli_app/` | Keep default output concise; expose full detail through `--json` |

## Adding an agent

1. Add the canonical Agent ID to the design registry.
2. Add model configuration and a Role Definition.
3. Add its schema, prompt, and thin agent class.
4. Register it in the layer registry and `workflow.py`.
5. Route it only through the layer Manager.
6. Add success, invalid payload, technical failure, revision, and RD trace tests.
7. Run `py scripts\verify.py`.

Do not copy the execution loop into the new layer. Subclass
`common.agents.StructuredAgent` through the layer's `agents/base.py` adapter.

## Release checklist

- `py main.py --doctor` has no failures.
- The complete unittest suite passes.
- Mock E2E creates exactly six delivery files.
- `specifications/common/` and runtime registries have no drift.
- The ZIP contains no `.env`, API key, workflow state, logs, cache, or virtual environment.
- A freshly extracted ZIP passes `py scripts\verify.py`.
