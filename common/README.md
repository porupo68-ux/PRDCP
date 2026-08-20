# Common

PMP、RD、共通Model、Validation、Loggingを扱う共有実装です。

`specifications/`が「何が正しいか」を定義し、`common/`はその契約をロード・検証・実行します。Layer固有のManager、Workflow、Agent処理はここへ移しません。

- `role_definitions/`: 共通RD Loader
- `models/`: 共通Pydantic Model
- `validation/`: 共通Validation
- `prompting/`: Prompt構築支援
- `logger.py`: 共通Logging

`structured_outputs.py`はOpenRouterへ渡す22 root schemaを再帰的にstrict化・検査します。root、nested object、array items、`$defs`、union以下の全objectで`additionalProperties: false`を要求し、`properties`がある場合は`required == properties.keys()`を保証します。自由形式dictは意味を壊して閉じず、Structured Output境界では明示的Pydantic modelを要求します。

ResearcherのHuman Evidence integrity repairは`researcher.schemas.human_evidence`がcanonical contract ownerです。分類修復、Report完全一致deduplication、同一文書系列tracking repairを`repair_kind` discriminatorで検証し、Researcherによる生成・保存からDeliberation、Conclusion、Playwright、Deliveryまで旧単一型を再定義しません。duplicate trackingのrepairability判定とrelation-only mutationは`researcher.integrity_repair`が所有し、CommonへLayer固有ロジックを移しません。
