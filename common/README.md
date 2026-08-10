# Common

PMP、RD、共通Model、Validation、Loggingを扱う共有実装です。

`specifications/`が「何が正しいか」を定義し、`common/`はその契約をロード・検証・実行します。Layer固有のManager、Workflow、Agent処理はここへ移しません。

- `role_definitions/`: 共通RD Loader
- `models/`: 共通Pydantic Model
- `validation/`: 共通Validation
- `prompting/`: Prompt構築支援
- `logger.py`: 共通Logging
