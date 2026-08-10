# Producer

## 1. Purpose

入力テーマを評価し、Researcherへ渡すResearch Planを作成します。

## 2. Responsibilities

Topic探索・選択、世論観点、Research Question設計、品質審査を担当します。

## 3. Agent Structure

Manager、Topic Scout、Topic Selector、General Opinion Analyst、Research Planner、Quality Reviewerで構成します。

## 4. Workflow

Topic入力→候補生成→選定→世論分析→Research Plan→Final Gateの順に処理します。

## 5. Inputs

ユーザーのtopic、またはTopic Scoutが生成した候補です。

## 6. Outputs

Research Plan、Producer state、Researcher向け`research_plan` messageです。

## 7. Role Definitions

`role_definitions/producer/*.json`を共通RD Loader経由で実行時に参照します。

## 8. PMP Interfaces

ResearcherへCanonical PMPの`research_plan`を送信します。

## 9. Storage

`storage/data/workflows/producer/`とResearcher向けOutboxを使用します。

## 10. Discord Operations

`#producer`の`!producer`、`!producer_topic`、`!producer_status`を使用します。

## 11. Main Files

`manager.py`、`registry.py`、`workflow.py`、`state.py`、`agents/`、`schemas/`です。

## 12. Testing

Producerのunit testと5 Layer Mock E2Eで検証します。
