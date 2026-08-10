# Discord App

5 Layer共通のDiscord Control Planeです。

- `bot.py`: 唯一のBot Entry Pointと全Layerコマンド
- `channel_router.py`: Channel RoutingとChannel Guard
- `commands.py`: 状態・成果物読込などの共通処理
- `message_formatter.py`: Discord向け表示整形
- `views.py`: UI View

`cli_app/commands.py`は常に`discord_app.bot.create_bot`を呼びます。Layer別botコピーは置きません。
