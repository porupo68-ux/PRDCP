# Researcher

## 1. Purpose

Research Planを専門領域別に調査し、根拠付きResearch Reportへ統合します。

## 2. Responsibilities

調査分解、並列調査、Source正規化、重複整理、coverage・gap評価、品質審査を担当します。

## 3. Agent Structure

Manager、7専門Researcher、Quality Reviewerで構成します。

## 4. Workflow

Research Plan→Task分解→専門調査→Source統合→Research Report→Final Gateです。

## 5. Inputs

Producerの`research_plan` messageとResearcher Inboxです。

## 6. Outputs

Research Report、Source群、Deliberation向け`research_result` messageです。

## 7. Role Definitions

`role_definitions/researcher/*.json`を共通RD Loader経由で参照します。

## 8. PMP Interfaces

Producerから受信し、DeliberationへCanonical PMP messageを送信します。

## 9. Storage

`storage/data/workflows/researcher/`、Artifacts、Deliberation向けOutboxを使用します。

## 10. Discord Operations

`#researcher`と`#sources`へ進捗・結果を出力します。

## 11. Main Files

`manager.py`、`registry.py`、`workflow.py`、`state.py`、`agents/`、`schemas/`です。

## 12. Testing

Source、Registry、Workflowのunit testとLayer間integration testで検証します。
