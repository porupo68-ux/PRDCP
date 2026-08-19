# Changelog

## Cycle 029

- Search retrievalをStructured Reasoningから分離し、8 Agentへdurable retrieval context、決定論的identity、独立reservation、source-grounding contractを追加。
- Research Plannerは検索不要のままGemini reasoningへ移行し、General Opinionと7 Researcher specialistはOpenRouter Web Search adapterから保存済みcitationを受け取る構成へ変更。
- DoctorをReasoning / Retrievalの二軸監査へ拡張し、438回帰テストと5層Mock E2Eを完走。

## Unreleased

- Separate Playwright's LLM revision budget from an allowlisted, one-pass,
  zero-Provider deterministic citation repair. `--playwright-recover` can now
  reconstruct a missing paragraph-to-evidence mapping only from saved canonical
  traceability, re-run Final Gate locally, persist a hash-audited repair artifact,
  and deliver exactly once without changing Script, Conclusion, Visual, or Evidence.
- Refine Demo Safe Mode to block automatic LLM agent execution rather than
  revision state transitions: persist upstream requests and mixed pending plans,
  enter `WAITING_UPSTREAM_REVISION`, and stop before Researcher or Deliberation
  agents are automatically dispatched.
- Keep internal-only Safe Mode revisions fail-closed with an explicit execution
  stop reason, while preserving approved, true-blocked, normal-mode, revision
  count, checkpoint, and duplicate-request behavior.
- Make Deliberation Quality Gate decisions repairability-first: route fixable
  evidence gaps to Researcher and fixable internal findings to targeted
  revision, reserve `blocked` for unrecoverable conditions, and reject blocked
  outputs that simultaneously declare executable revision routes.
- Clarify that Quality Reviewer self-retry and workflow revision are independent,
  and add regression coverage for the observed legacy blocked-plus-routes
  recovery result without weakening deterministic validation.
- Persist mixed upstream-and-internal Deliberation revision plans, route
  Researcher evidence collection before any dependent internal rerun, and
  restore the exact target dependency plan through `resume()` without resetting
  completed high-cost checkpoints or double-counting the revision cycle.
- Keep `recover()` dedicated to technical checkpoint recovery, including
  failures after a pending revision has been consumed, while preserving Demo
  Safe Mode, revision limits, and provider-independent workflow routing.
- Add invocation-scoped `--provider`, `--safe-mode`, and `--no-safe-mode`
  overrides with CLI-over-environment precedence while preserving immutable
  Settings and all existing command defaults.
- Share one effective provider and Demo Safe Mode configuration across all five
  managers, bind provider-call reservations to the effective data directory and
  logical provider namespace, and report unsafe live-runtime combinations.
- Separate Deliberation task, primary-analysis, counterargument-analysis, and
  initial/final-integration identifier namespaces, while normalizing legacy ID
  collisions only when saved checkpoints are read.
- Replace mixed Deliberation traceability arrays with typed claim, evidence,
  source, analysis, counterargument, integration, and task references, and
  validate the claim-to-source chain before Conclusion handoff.
- Make deterministic validation metrics derive from one explicit target set and
  cross-check counts, routing dispositions, unresolved items, uncertainties,
  revision requests, and integration changes before allowing `passed=true`.
- Give the Deliberation Quality Reviewer a bounded PMP/checkpoint routing trace,
  require evidence-bound stakeholder specifics, and require every blocking
  counterargument to be revised, rejected, retained as unresolved, or routed to
  Researcher without dropping internal revision targets.
- Constrain Quality Review `revision_targets` to Deliberation agents and carry
  `researcher.manager` only as the explicit target of an upstream research
  request, with read-time normalization for legacy mixed routing responses.
- Rebuild the checkpoint-recovery review view with canonical nested analysis
  references, current deterministic metrics, and derived legacy PMP parent,
  retry, status, and supersession metadata without rewriting saved artifacts.
- Preserve legacy Deliberation workflow JSON and high-cost checkpoints during
  recovery by applying compatibility conversion in memory and rerunning only the
  incomplete Quality Review path when earlier checkpoints are complete.
- Normalize every OpenRouter Structured Output schema recursively so all declared
  object properties, including fields backed by Pydantic defaults and default
  factories, are listed in `required`, and every object node declares
  `additionalProperties: false`.
- Replace free-form output dictionaries with explicit Pydantic models, including
  nested models, array items, unions, `$defs`, and Deliberation Quality Review
  `required_scope`; reject newly introduced free-form output objects before an API call.
- Separate strict API-schema normalization from validation, recursively remove
  Pydantic `default` annotations only from the API-boundary copy, and reject
  unresolved references or unsupported `$ref` siblings with schema-path errors.
- Use the same strict schema in agent prompts and OpenRouter `response_format`
  payloads across Producer, Researcher, Deliberation, Conclusion, and Playwright.
- Add a recursive cross-layer audit covering all 22 OpenRouter output schema roots
  plus prompt/`response_format` generation, while preserving legacy Deliberation
  checkpoint payloads for Quality Reviewer recovery.

## 2.0.0

- Canonicalized five Prototype snapshots into the `PRDCP_v2` project root.
- Consolidated five equivalent Discord bot copies into `discord_app/bot.py`.
- Merged non-conflicting runtime records into the single `storage/data/` tree.
- Added responsibility-level README files and migration/audit documentation.
- Sanitized credential fields in `.env.example`.
- Five layers now ship from one canonical codebase.
- Added `--doctor` for dependency, storage, RD, PMP, registry, and provider checks.
- Added `--status` to show every saved layer, the blocking error, and the next command.
- Default CLI output is concise; `--json` preserves full developer output.
- Consolidated five duplicated BaseAgent implementations into `common/agents/base.py`.
- Standardized every `workflow.py` around `LAYER_ID`, `MANAGER_ID`, `AGENT_IDS`,
  `AGENT_ORDER`, and `DISPLAY_NAMES`.
- Bundled the canonical PMP v2.0 registries and added automatic drift detection.
- Added rotating application logs and a searchable runtime event JSONL.
- Added `pyproject.toml`, a repeatable verification script, and GitHub Actions CI.
- Added architecture, maintenance, and troubleshooting guides.
