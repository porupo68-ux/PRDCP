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
