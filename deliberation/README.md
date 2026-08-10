# Deliberation

## 1. Purpose

Research Reportを複数の分析視点から審議し、Conclusion用のDeliberation Resultへ統合します。

## 2. Responsibilities

論証、因果構造、Stakeholder対応、反論分析、統合、品質審査を担当します。

## 3. Agent Structure

Manager、Argument Analyst、Causal & Structural Analyst、Stakeholder & Response Analyst、Counterargument Analyst、Quality Reviewerで構成します。

## 4. Workflow

Research Result→一次並列分析→初回統合→反論分析→再統合→Final Gateです。

## 5. Inputs

Researcherの`research_result` messageとResearch Reportです。

## 6. Outputs

Deliberation Result、主要Viewpoint、Conclusion向けhandoffです。

## 7. Role Definitions

`role_definitions/deliberation/*.json`を共通RD Loader経由で参照します。

## 8. PMP Interfaces

Researcherから受信し、必要時はupstream revisionを要求し、Conclusionへ送信します。

## 9. Storage

`storage/data/workflows/deliberation/`、Artifacts、Conclusion向けOutboxを使用します。

## 10. Discord Operations

`#deliberation`の実行・status・result・resumeコマンドを使用します。

## 11. Main Files

`manager.py`、`registry.py`、`workflow.py`、`state.py`、`validator.py`、`agents/`、`schemas/`です。

## 12. Testing

Viewpoint追跡、revision、validator、handoffをunit/integration testで検証します。
