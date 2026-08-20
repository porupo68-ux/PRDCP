# Conclusion

## 1. Purpose

Deliberation Resultから複数の結論候補を作成し、人間の最終選択を確定します。

## 2. Responsibilities

Position生成、Decision評価・統合、品質審査、Human Selectionの保存を担当します。

## 3. Agent Structure

Manager、Position Generator、Decision Evaluator、Decision Integrator、Quality Reviewerで構成します。

## 4. Workflow

Deliberation Result→候補生成→評価→統合→Final Gate→Human Selectionです。

## 5. Inputs

DeliberationのhandoffとDeliberation Resultです。

## 6. Outputs

Conclusion Package、候補一覧、選択済みFinal Conclusionです。

## 7. Role Definitions

`role_definitions/conclusion/*.json`を共通RD Loader経由で参照します。

## 8. PMP Interfaces

Deliberationから受信し、Human Selection後にPlaywrightへ送信します。

## 9. Storage

`storage/data/workflows/conclusion/`、Artifacts、Playwright向けOutboxを使用します。

## 10. Discord Operations

`#conclusion`のoptions・select・integrate・result・resumeコマンドを使用します。

## 11. Main Files

`manager.py`、`registry.py`、`workflow.py`、`state.py`、`validator.py`、`agents/`、`schemas/`です。

## 12. Testing

候補統合、Candidate Coverage、選択、revision、checkpoint recovery、handoffをunit/integration testで検証します。

## 13. Recovery and Human Selection

`--conclusion-recover`はPosition、Evaluation、Integration、Quality Reviewの保存済みResult PMPを照合し、Result保存後にState更新だけが失敗したcheckpointをProvider 0件で復元します。旧schemaでDecision Evaluatorが候補を部分評価したことを保存済みinvalid payloadから証明できる場合に限り、Positionを再実行せず、専用の一回限りtask identityでEvaluationから再開します。入力候補数×14基準と候補数分の比較行はstrict schemaで固定し、受信後もPosition候補・評価候補・比較候補の完全一致を監査してStateへ保存します。欠落評価の推測補完は行いません。通信・応答契約の一時障害は`--conclusion-provider-retry`、保存済み`revision_required`の一サイクルは`--conclusion-revise`、元taskと一回retryがともに契約違反になった場合だけ異なるmodelの`--conclusion-contract-repair`を使用します。

Quality Review通過後も`WAITING_HUMAN_SELECTION`で停止し、`--conclusion-select`または`--conclusion-integrate`による明示的人間選択を必須とします。互換性修復に成功したmodel bindingはProvider・Agent・output schema・元modelへ限定してappend-only保存し、将来の一致taskだけに適用します。
