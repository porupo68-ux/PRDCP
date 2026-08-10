# Troubleshooting

## Start here

```powershell
py main.py --doctor
py main.py --status <workflow_id>
```

`--doctor` checks the environment before a workflow starts. `--status` reads all
saved layers and shows the latest stage, failures, revision counts, candidates,
deliveries, and the next command.

## Result meanings

| State | Meaning | Action |
| --- | --- | --- |
| `COMPLETED` | The layer finished and wrote its handoff/artifact | Run the next command shown by `--status` |
| `WAITING_HUMAN_SELECTION` | Conclusion passed quality review | Choose a listed candidate with `--conclusion-select` |
| `WAITING_UPSTREAM_REVISION` | The current layer needs corrected upstream evidence/output | Process the revision outbox, then use the layer's resume command |
| `FAILED` | A technical, schema, RD, or routing error stopped safely | Read `error`, runtime log, and the layer's messages JSONL |
| `BLOCKED` | Quality requirements were not met within the revision limit | Inspect findings and decide whether to change evidence, RD, or workflow rules |

## Files to inspect

```text
storage/data/logs/application.log
storage/data/logs/runtime_events.jsonl
storage/data/logs/rd_access.jsonl
storage/data/workflows/<layer>/<workflow_id>.json
storage/data/workflows/<layer>/<workflow_id>.messages.jsonl
storage/data/outbox/<target>/<workflow_id>.json
```

The state JSON answers "where did it stop?". The messages JSONL answers "which
Agent request/result caused it?". The RD access log answers "which RD version
and hash were used?".

## Common failures

### `OpenRouter model ID is not configured`

Set every required `MODEL_*` value to an actual OpenRouter model ID. Display
names in `config/models.json` are documentation; they are not API identifiers.

### `Unknown sender_agent_id` or `Unknown message_type`

Run `--doctor`. A contract drift failure means a registry and runtime enum were
changed separately. Update the canonical file in `specifications/common/` and
the runtime implementation in the same change.

### RD validation failure

Do not disable STRICT mode to continue production. Fix the named RD or registry
entry, update its version, and rerun the doctor command.

### Discord does not respond

Confirm `discord.py` is installed, `DISCORD_BOT_TOKEN` is set, Message Content
Intent is enabled, and the doctor command reports Discord as configured.
