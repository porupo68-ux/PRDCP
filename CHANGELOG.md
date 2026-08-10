# Changelog

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
