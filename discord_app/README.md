# Discord App

5 Layer共通のDiscord Control Planeです。

- `bot.py`: 唯一のBot Entry Pointと全Layerコマンド
- `channel_router.py`: Channel RoutingとChannel Guard
- `commands.py`: 状態・成果物読込などの共通処理
- `message_formatter.py`: Discord向け表示整形
- `views.py`: UI View

`cli_app/commands.py`は常に`discord_app.bot.create_bot`を呼びます。Layer別botコピーは置きません。

Producer statusの`Completed`欄は保存済み`ProducerWorkflowState.completed_agents`だけを表示します。未完了Agentは`Pending`、実行中Agentは`Current`へ分離し、FAILED時にWorkflow定義上の全Agentを完了済みのように表示しません。Discord表示のsource of truthは永続化済みWorkflow Stateです。

ResearcherのHuman Evidence Gateは`!researcher_evidence`で確認し、`!researcher_accept`、`!researcher_accept_limitations`、`!researcher_revise`、`!researcher_recover`で操作します。Human DecisionはCLIと同じcreate-once artifactへ保存され、Discord操作だけでProvider呼び出しを許可しません。新コマンドを有効化するにはデプロイ後にBotプロセスを再起動してください。

`!runtime_models [layer]`は起動時Runtime Model、現在のConfigured Model、検証済み互換bindingを分けて表示します。Driftがある場合はProvider Reservation前に停止します。

Botへ実登録された全Discordコマンド、用途、外部API呼び出しの目安はルート`README.md`の「コマンド早見表（CLI / Discord）」へ一括掲載します。Discordには技術障害用のProvider retry、contract/capability repair、Playwright deterministic repairを公開していないため、これらは`--status`で状態を確認してCLIから明示実行します。

例外の完全TracebackはApplication Logへ記録し、Discordには700文字以下のoperational summaryだけを送信します。Layer実行またはhandoff validationが失敗した場合は対応するstatus通知を`ERROR`へ閉じ、Discord表示に`RUNNING`を残しません。Discordの2000文字制限を回避するためTracebackや巨大payloadを直接投稿しません。
