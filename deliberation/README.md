# Deliberation

## 1. Purpose

Research Reportを複数の分析視点から審議し、Conclusion用のDeliberation Resultへ統合します。

## 2. Responsibilities

論証、因果構造、Stakeholder対応、反論分析、統合、品質審査を担当します。

## 3. Agent Structure

Manager、Argument Analyst、Causal & Structural Analyst、Stakeholder & Response Analyst、Counterargument Analyst、Quality Reviewerで構成します。

## 4. Workflow

Research Result→一次並列分析→初回統合→反論分析→最終統合→決定論的Validation→Quality Reviewです。Quality Reviewerには成果物だけでなく、Primary Analysts→Manager→Counterargument→Manager→Quality Reviewerを確認できる最小PMP routing traceを渡します。

## 5. Inputs

Researcherの`research_result` messageとResearch Reportです。

## 6. Outputs

Deliberation Result、主要Viewpoint、型分離されたtraceability、Conclusion向けhandoffです。task、analysis、counterargument、initial/final integrationは別namespaceで採番し、Evidence→SourceとClaim→Analysis→Evidence→Sourceを追跡します。

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

## 13. Revision and Recovery

`--deliberation-resume`はResearcherから追加Evidenceを受領した後の再分析、`--deliberation-recover`は技術障害後のcheckpoint recoveryです。RecoveryはPrimary分析、初回統合、Counterargument、最終統合、決定論的Validation、Quality Reviewを個別に照合し、完了済みの高コスト処理を再実行しません。課金済みの可能性がある一時障害は`--deliberation-provider-retry`で一度だけ明示再送します。

Validatorのclaim、viewpoint、Evidence、revision request、integration change、unresolved/uncertainty件数は同じcanonical対象集合から算出し、`passed=true`と実体の矛盾を拒否します。blocking Counterargumentは修正、明示棄却、未解決保持、Researcher返送のいずれかへ必ずroutingします。
