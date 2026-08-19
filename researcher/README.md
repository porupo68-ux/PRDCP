# Researcher

## 1. Purpose

Research Planを専門領域別に調査し、根拠付きResearch Reportへ統合します。

## 2. Responsibilities

調査分解、並列調査、Source正規化、重複整理、coverage・gap評価、品質審査を担当します。

## 3. Agent Structure

Manager、7専門Researcher、Quality Reviewerで構成します。

## 4. Workflow

Research Plan→Task分解→専門調査→Source統合→Research Report→Quality Review→Human Evidence Gateです。Quality ReviewerはEvidenceの不足と技術的整合性を分類しますが、利用可否を最終決定しません。

## 5. Inputs

Producerの`research_plan` messageとResearcher Inboxです。

## 6. Outputs

Research Report、Source群、Human Evidence Decision、Deliberation向け`research_result` messageです。DeliberationへのHandoffにはHuman Decision、受容された未解決gap、決定論的integrity repairを明示します。

## 7. Role Definitions

`role_definitions/researcher/*.json`を共通RD Loader経由で参照します。

## 8. PMP Interfaces

Producerから受信し、DeliberationへCanonical PMP messageを送信します。

## 9. Storage

`storage/data/workflows/researcher/`、Artifacts、Deliberation向けOutboxを使用します。Human Decisionは`artifacts/human_evidence_decisions/<workflow_id>/<quality_review_id>.json`へcreate-onceで保存し、State更新やOutbox保存が中断してもRecoveryで同じ決定を再利用します。

## 10. Discord Operations

`#researcher`と`#sources`へ進捗・結果を出力します。`researcher_evidence`で審査内容を確認し、`researcher_accept`、`researcher_accept_limitations`、`researcher_revise`のいずれかを人間が明示します。

## 11. Human Evidence Gate

Quality Review完了後は常に`WAITING_HUMAN_EVIDENCE_REVIEW`で停止します。`ACCEPT`は未解決Evidence findingがない場合だけ、`ACCEPT_WITH_LIMITATIONS`は全Evidence findingを未解決gapとして開示する場合だけ許可します。受容されたgapはEvidenceでも事実確認でもなく、下流は引用・根拠として使用できません。`REVISE`は追加調査計画を0-callで保存して停止し、Provider呼び出しには別の明示的authorizationが必要です。Schema、PMP、provenance等のhard integrity failureは人間判断で上書きできません。

## 12. Main Files

`manager.py`、`registry.py`、`workflow.py`、`state.py`、`agents/`、`schemas/`です。

## 13. Testing

Source、Registry、Workflowのunit testとLayer間integration testで検証します。
