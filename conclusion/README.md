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

候補統合、選択、revision、handoffをunit/integration testで検証します。
