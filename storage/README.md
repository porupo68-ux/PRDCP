# Storage

`storage/data/`が唯一のCanonical Storageです。Repository実装はこのディレクトリ直下、実行時データは`data/`だけに置きます。

- `data/workflows/<layer>/`: Layer別Workflow状態とPMP message
- `data/deliveries/<workflow_id>/`: 最終納品物
- `data/artifacts/`: Layer間で参照する成果物
- `data/outbox/`: Cross-layer Handoff
- `data/logs/`: Runtime、RD access、Application log

5 Layerは同じ`workflow_id`を共有します。旧Prototype固有パスを新規データへ書き込みません。
