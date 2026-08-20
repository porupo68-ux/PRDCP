# Researcher

## 1. Purpose

Research Planを専門領域別に調査し、根拠付きResearch Reportへ統合します。

## 2. Responsibilities

調査分解、並列調査、Source正規化、重複整理、coverage・gap評価、品質審査を担当します。

## 3. Agent Structure

Manager、7専門Researcher、Quality Reviewerで構成します。

## 4. Workflow

Research Plan→Task分解→7専門AgentのRetrieval→Structured Reasoning→Source統合→Research Report→Quality Review→Human Evidence Gateです。検索結果を先に保存し、Reasoning失敗後も同じRetrieval Contextを再利用します。Quality ReviewerはEvidenceの不足と技術的整合性を分類しますが、利用可否を最終決定しません。

## 5. Inputs

Producerの`research_plan` messageとResearcher Inboxです。

## 6. Outputs

Research Report、Source群、Human Evidence Decision、Deliberation向け`research_result` messageです。DeliberationへのHandoffにはHuman Decision、受容された未解決gap、決定論的integrity repairを明示します。

## 7. Role Definitions

`role_definitions/researcher/*.json`を共通RD Loader経由で参照します。

## 8. PMP Interfaces

Producerから受信し、DeliberationへCanonical PMP messageを送信します。

## 9. Storage

`storage/data/workflows/researcher/`、`retrieval_contexts/`、Artifacts、各種Authorization/Reservation、Deliberation向けOutboxを使用します。Human Decisionは`artifacts/human_evidence_decisions/<workflow_id>/<quality_review_id>.json`へ、決定論的integrity repairは`artifacts/human_evidence_integrity_repairs/<workflow_id>/<repair_id>.json`へcreate-onceで保存します。State更新やartifact/Outbox保存が中断してもRecoveryで同じidentityを再利用します。

## 10. Discord Operations

`#researcher`と`#sources`へ進捗・結果を出力します。`researcher_evidence`で審査内容を確認し、`researcher_accept`、`researcher_accept_limitations`、`researcher_revise`のいずれかを人間が明示します。

## 11. Human Evidence Gate

Quality Review完了後は常に`WAITING_HUMAN_EVIDENCE_REVIEW`で停止します。`ACCEPT`は未解決Evidence findingがない場合だけ、`ACCEPT_WITH_LIMITATIONS`は全Evidence findingを未解決gapとして開示する場合だけ許可します。受容されたgapはEvidenceでも事実確認でもなく、下流は引用・根拠として使用できません。`REVISE`は追加調査計画を0-callで保存して停止し、Provider呼び出しには別の明示的authorizationが必要です。Schema、PMP、provenance等のhard integrity failureは人間判断で上書きできません。

`--researcher-recover`はHuman Decisionを推測せず、保存済みdecision/state/outboxを照合します。同時に、完全一致limitation重複除去と限定されたSource分類修復だけをProvider/Retrieval 0件で実行できます。同一公式文書の複数Sourceに文書系列relationだけが欠けるHard Findingは、明示的な`--researcher-integrity-repair WORKFLOW_ID`で修復します。発行者・正規化title・版・canonical本文候補・既存relationを保存済みReportだけで一意に証明できる場合に限り、全Sourceを保持してcanonical Sourceの`merged_evidence_ids`を再構成します。曖昧性やconflictはFail Closedであり、Evidence Gap、Human Decision、Quality Review、Revision budgetを変更しません。

修復artifactは`repair_kind`で識別するcanonical discriminated unionです。分類修復、`report_limitation_exact_deduplication`、`research_source_duplicate_tracking`をResearcher保存、Deliberation読込、下流Handoffで同じ契約として検証します。元Quality Review、Source identity、本文、URL、Research Question、Human Decisionは変更しません。

旧実行の局所Recoveryには`--researcher-runtime-model-repair`、`--researcher-runtime-output-repair`、`--researcher-runtime-adapter-repair`、`--researcher-runtime-identity-repair`、`--researcher-runtime-provenance-repair`があります。すべて既存Retrievalのhashと監査台帳を要求し、旧taskを再送しません。Retrieval Context自体が欠け、オペレーターが新規検索を承認する場合だけ`--researcher-retrieval-reconstruct`を使います。

## 12. Main Files

`manager.py`、`registry.py`、`workflow.py`、`state.py`、`agents/`、`schemas/`です。

## 13. Testing

Source、Registry、Workflowのunit testとLayer間integration testで検証します。
