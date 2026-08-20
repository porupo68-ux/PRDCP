# Tests

PRDCP全体のテストを一元管理します。

- `unit/`: Model、Registry、RD Loader、Formatter等
- `integration/`: Layer間接続とE2E寄りの検証
- `discord/`: Channel RoutingとChannel Guard
- `fixtures/`: 共通Fixture

実行:

```powershell
py -m unittest discover -s tests -p "test_*.py" -v
```

Release相当の検証は次を使用します。

```powershell
py scripts\verify.py
```

compile、全unit/integration、31 Role DefinitionのSTRICT読込、Common contract drift、OpenRouterへ送る22 root Structured Output Schemaの再帰監査、5層Mock E2E、6 Deliveryファイルを確認します。実API固有の試験が必要な場合も、Static→Targeted Regression→Integration→Mock/Fault→保存済みcheckpointを使う局所Real callの順に進め、正常なResultとRetrievalを再実行しません。

`unit/test_readme_command_catalog.py`は、CLIの全operation flagとDiscord Botへ実登録された全commandが、ルート`README.md`の一括コマンド表に掲載されていることを検証します。新しい運用コマンドを追加した場合、説明と外部API呼び出し条件を同表へ追記しない限り回帰テストを通しません。
