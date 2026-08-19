# Storage

`storage/data/`が唯一のCanonical Storageです。Repository実装はこのディレクトリ直下、実行時データは`data/`だけに置きます。

- `data/workflows/<layer>/`: Layer別Workflow状態とPMP message
- `data/deliveries/<workflow_id>/`: 最終納品物
- `data/artifacts/`: Layer間で参照する成果物
- `data/outbox/`: Cross-layer Handoff
- `data/retrieval_contexts/`: Reasoningから分離して保存する検索結果とhash
- `data/provider_call_reservations/`: LLM taskの送信前Reservation
- `data/retrieval_call_reservations/`: Retrieval taskの送信前Reservation
- `data/provider_*_authorizations/`, `data/retrieval_reconstruction_authorizations/`: 一回限りRetry/Repair認可
- `data/provider_model_compatibility/`: 検証済みProvider/Agent/output schema/model binding
- `data/runtime_model_snapshots/`: 起動時model snapshotとdrift照合情報
- `data/logs/`: Runtime、RD access、Application log

5 Layerは同じ`workflow_id`を共有します。旧Prototype固有パスを新規データへ書き込みません。

Workflow、PMP、Result、Human Decision、Outbox、Reservation、AuthorizationはRecoveryの監査入力です。Recoveryの都合だけで既存JSONを削除・初期化せず、通常のState更新はatomic writeで行います。旧Schema互換は読込時adapterまたは新しい監査recordで扱います。
