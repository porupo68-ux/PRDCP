# Playwright

## 1. Purpose

選択済みFinal Conclusionを変更せず、台本と制作向け最終成果物へ変換します。

## 2. Responsibilities

Narrative設計、Script執筆、Evidence/Citation編集、Visual設計、Manager Final Gateを担当します。

## 3. Agent Structure

Manager、Narrative Architect、Scriptwriter、Evidence Citation Editor、Visual Directorで構成します。

## 4. Workflow

Final Conclusion→Narrative Blueprint→Script Draft→Citation/Visual編集→Final Script Packageです。

## 5. Inputs

ConclusionのHuman Selection済みhandoffです。

## 6. Outputs

Script、Source List、Citation Manifest、Visual Plan、Production Notes、Final Script Packageです。

## 7. Role Definitions

`role_definitions/playwright/*.json`を共通RD Loader経由で参照します。

## 8. PMP Interfaces

Conclusionから受信し、`system.final_output`へ最終納品messageを送信します。

## 9. Storage

`storage/data/workflows/playwright/`と`storage/data/deliveries/<workflow_id>/`を使用します。

## 10. Discord Operations

`#playwright`、`#deliveries`、`#workflow-status`へ進捗と成果物を出力します。

## 11. Main Files

`manager.py`、`registry.py`、`workflow.py`、`state.py`、`validator.py`、`agents/`、`schemas/`です。

## 12. Testing

Citation、Visual、Delivery、Full Mock E2Eで検証します。
