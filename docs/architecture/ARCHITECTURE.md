# PRDCP v2 Architecture

## One canonical build

The five layers are developed and tested as one codebase. A change to PMP,
RDLoader, storage, providers, or the shared agent runner is therefore applied to
every layer at once.

```text
Producer -> Researcher -> Deliberation -> Conclusion -> Playwright
```

Researcher always stops at the Human Evidence Gate after its Quality Review.
Only an explicit `ACCEPT`, `ACCEPT_WITH_LIMITATIONS`, or `REVISE` decision can
advance the control plane. Conclusion separately stops at the human-selection
gate. `--demo-e2e` may cross both gates only with explicit Mock fixtures.

Human Evidence Governance separates three concepts. Quality Review classifies
machine-observed findings; a Human Decision accepts, accepts with disclosed
limitations, or requests revision; Provider authorization is a separate,
audited permission. Human acceptance never converts an unresolved evidence gap
into evidence or factual support, and cannot override schema, PMP, provenance,
or other hard integrity failures. The decision and accepted gaps are preserved
across every downstream handoff through Playwright.

## Standard layer shape

Each layer follows the same navigational pattern:

```text
<layer>/
  agents/       Specialist adapters and agent declarations
  prompts/      Small layer-specific prompt additions
  schemas/      Pydantic input/output contracts
  manager.py    Workflow orchestration and revision routing
  registry.py   Agent construction and model assignment
  state.py      Persisted workflow state
  validator.py  Deterministic checks, when the layer needs them
  workflow.py   IDs, order, display names, and dependencies
```

Every `workflow.py` exposes the same discovery fields:

- `LAYER_ID`
- `MANAGER_ID`
- `AGENT_IDS`
- `AGENT_ORDER`
- `DISPLAY_NAMES`

## Shared control plane

| Location | Responsibility |
| --- | --- |
| `common/agents/base.py` | Identical RD-aware execution, retry, error, and PMP behavior |
| `common/models/pmp.py` | PMP v2 envelope and enums |
| `common/role_definitions/` | RD registry, validation, cache, extraction, and prompt injection |
| `common/specifications.py` | Drift audit against the canonical machine-readable design |
| `providers/` | Mock and OpenRouter model access |
| `storage/` | Atomic state, artifact, outbox, and delivery persistence |
| `cli_app/` | Operator commands, concise output, diagnostics, and status inspection |
| `specifications/common/` | Canonical PMP, Agent, Status, Message Type, and Handoff registries |

## Intentional design override

The design archive contains `playwright.quality_reviewer`, while the later
Playwright implementation plan fixes the executable layer at five agents. The
override is explicit in `config/implementation_overrides.json`; quality is
provided by the Evidence & Citation Editor, deterministic validator, and
Playwright Manager final gate. The doctor command fails if an undocumented
design/runtime mismatch appears.
