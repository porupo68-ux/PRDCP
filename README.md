# PRDCP 

ProducerがResearch Planを作成し、Researcherが証拠を収集し、Deliberationが多角的に分析し、Conclusionが人間の最終選択を確定します。Playwrightは、その確定済みConclusionを変更せず、台本・引用台帳・映像指示・制作ノートへ変換します。OpenRouter APIキーがなくてもMock Providerで5層の全工程を確認できます。

通信・Agent ID・status・クロスレイヤーpayloadは、`specifications/common/`のPMP v2.0を機械可読な正本として実装しています。共通基盤は1か所、Layer固有実装とRDはLayer別、実行時データは`storage/data/`に集約したCanonical Projectです。

## Canonical構成

- `main.py`: 唯一の実行Entry Point
- `runtime.py`: 5 Layer、Provider、RD Loader、Repositoryの組み立て
- `config/`, `common/`, `specifications/`: 共通設定・共通実装・共通契約
- `producer/`, `researcher/`, `deliberation/`, `conclusion/`, `playwright/`: Layer固有実装
- `role_definitions/<layer>/`: AgentごとのRole Definition
- `discord_app/`: 全Layer共通のDiscord Control Plane
- `storage/data/`: 唯一のCanonical Storage
- `docs/migration/`, `docs/audit/`: 移行方針と監査記録
- `archive/`: 統合元PrototypeとZIPの保全領域（実行対象外）

## 最初に実行する3コマンド

```powershell
py main.py --doctor
py main.py --demo-e2e --topic "生成AIは人間の仕事を奪うのか"
py main.py --status <表示されたworkflow_id>
```

- `--doctor`は依存関係、保存先、31 RD、32 Agent ID、29 Message Type、7 PMP Status、4 Handoff、Provider設定を検査します。
- `--demo-e2e`はAPI料金なしのMockで5層と6納品ファイルを確認します。
- `--status`は停止した層、Agent、error、revision回数、次に実行するコマンドを表示します。
- CLIは通常、読みやすい要約だけを表示します。内部状態が必要な場合だけ末尾へ`--json`を付けます。

Mock E2Eの完走は制御系・Schema・保存・層間接続が動くことを保証します。実OpenRouterの応答品質と外部サービス可用性は別条件なので、API keyと全model IDを設定後に`--doctor`を再実行してください。

## 実装済み

### 共通・Producer

- PMP v2.0モデル、親子message追跡、Agent ID・message type・status検証
- Producerの5専門工程と最大3回の修正ループ
- JSON/JSONL永続化とResearcher Inboxへの`research_plan`出力
- Mock Providerと任意利用のOpenRouter Provider

### Researcher

- Research Managerと7専門Researcher、Researcher Quality Reviewer
- Research Planから必要なAgentだけを選択するResearch Task分解
- `asyncio.gather()`による専門Researcherの並列実行
- 一部失敗のlimitation化、全失敗時の安全停止
- 全カテゴリ共通のSource Schemaとカテゴリ固有metadata
- DOI、正規化URL、書誌情報、タイトル類似度による重複整理
- Research Question・Evidence・SourceのID追跡
- Research Report、coverage、evidence gap、品質評価、source perspective生成
- 対象Agentだけを再実行する最大3回の修正ループ
- 独立Research Report artifactとDeliberation Outboxへの`research_result`出力
- 手動起動と、設定で有効化できるProducer完了後の自動起動

### Deliberation

- Argument、Causal & Structural、Stakeholder & Responseの3専門Agentを並列実行
- Deliberation Managerによる初回統合
- 初回統合後にCounterargument Analystを実行し、変更履歴付きで再統合
- 最大3つの主要ViewpointとEvidence→Claim→Viewpoint追跡
- Schema、ID、Evidence、統合系譜を検査する決定論的Validator
- Deliberation Quality ReviewerによるConclusion Readiness審査
- 対象Agentだけを再実行し、依存する統合工程だけを再実行する最大2回の修正ループ
- Evidence不足時のResearcher追加調査要求と`WAITING_UPSTREAM_REVISION`再開処理
- 一次分析の一部失敗を条件付きで継続し、2系統未満では安全停止
- Deliberation Result artifactとConclusion Outboxへの`deliberation_result`出力
- Discordからの開始、状態確認、結果表示、再開

### Conclusion

- Conclusion Manager、Position Generator、Decision Evaluator、Decision Integrator、Quality Reviewer
- Deliberation Resultから2～5件の独立したPosition Candidateを生成
- 全候補を同一の14基準で評価し、`NOT_EVALUABLE`をゼロ点扱いしない順序尺度を採用
- 実行可能性・法的制約などのblocking reasonと不確実性を明示
- 重みの異なる複数ProfileによるSensitivity Analysis
- Candidate間の両立性を検査し、必要な場合だけIntegrated Optionを生成
- 対象Agentと依存工程だけを再実行する最大2回の修正ループ
- 根拠不足時のDeliberation差し戻しと`WAITING_UPSTREAM_REVISION`再開処理
- Quality Gate通過後も`WAITING_HUMAN_SELECTION`で停止し、人間の選択を必須化
- 選択済みFinal Conclusionを不変Artifactとして保存し、Playwright Outboxへ`conclusion_handoff`を出力
- Discordからの開始、候補表示、統合依頼、選択、状態確認、結果表示、再開

### Playwright

- Playwright Manager、Narrative Architect、Scriptwriter、Evidence & Citation Editor、Visual Director
- ConclusionのHuman SelectionとTraceabilityを開始前に検証
- Narrative Blueprintから台本、Citation Manifest、Visual Planを順次生成
- Final Conclusionの内容・ID・SHA-256 Hashを固定し、意味変更を禁止
- 段落、Claim、Evidence、Source、Citation、Visual CueのID追跡
- 引用必須段落、未裏付け主張、出典Locator、制限事項、Chart出典、Visual整合性の決定論的検証
- 対象Agentと依存工程だけを再実行する最大2回の修正ループ
- 上流不足時のConclusion差し戻しと`WAITING_UPSTREAM_REVISION`再開処理
- 独立Playwright Quality Reviewerを置かず、ValidatorとManager Final Gateで最終判定
- JSON・MarkdownによるFinal Script Package 6ファイルの納品
- CLI・Discordからの開始、状態確認、台本・引用・映像指示・結果表示、再開

## 1. セットアップ（Windows）

自動セットアップ:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```

手動セットアップ:

```powershell
cd prdcp
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
copy .env.example .env
```

最初は`.env`の`PRDCP_PROVIDER=mock`のままで構いません。

## 2. APIなしで全工程を確認

Producerだけを実行します。

```powershell
py main.py --demo --topic "生成AIは人間の仕事を奪うのか"
```

ProducerからResearcher、Deliberation、Conclusionの品質審査まで連続実行し、人間の選択待ちで停止します。

```powershell
py main.py --demo-full --topic "生成AIは人間の仕事を奪うのか"
```

Mockで最初の候補を自動選択し、ProducerからFinal Script PackageまでE2E実行します。

```powershell
py main.py --demo-e2e --topic "生成AIは人間の仕事を奪うのか"
```

Producerで作成済みの`workflow_id`からResearcherだけを手動起動できます。

```powershell
py main.py --researcher <workflow_id>
```

Researcherで作成済みの`workflow_id`からDeliberationだけを手動起動できます。

```powershell
py main.py --deliberation <workflow_id>
```

Researcherの追加調査結果を受領した後は、待機中Workflowを再開できます。

```powershell
py main.py --deliberation-resume <workflow_id>
```

Deliberationで作成済みの`workflow_id`からConclusionを起動できます。

```powershell
py main.py --conclusion <workflow_id>
```

Deliberationの修正版を受領した後は、待機中のConclusion Workflowを再開できます。

```powershell
py main.py --conclusion-resume <workflow_id>
```

品質審査済みCandidateを人間が選択すると、Final ConclusionとPlaywright Handoffを確定します。

```powershell
py main.py --conclusion-select <workflow_id> <candidate_id>
```

複数Candidateの統合案を再生成・再審査する場合は、2件以上のIDを指定します。

```powershell
py main.py --conclusion-integrate <workflow_id> <candidate_id_1> <candidate_id_2>
```

Human Selection確定後にPlaywrightを手動起動します。

```powershell
py main.py --playwright <workflow_id>
```

Conclusionの修正版を受領した後は、待機中のPlaywright Workflowを再開できます。

```powershell
py main.py --playwright-resume <workflow_id>
```

## 3. 保存先

```text
storage/data/workflows/producer/<workflow_id>.json
storage/data/workflows/producer/<workflow_id>.messages.jsonl
storage/data/outbox/researcher/<workflow_id>.json

storage/data/workflows/researcher/<workflow_id>.json
storage/data/workflows/researcher/<workflow_id>.messages.jsonl
storage/data/artifacts/research_reports/<workflow_id>.json
storage/data/outbox/deliberation/<workflow_id>.json

storage/data/workflows/deliberation/<workflow_id>.json
storage/data/workflows/deliberation/<workflow_id>.messages.jsonl
storage/data/artifacts/deliberation_results/<workflow_id>.json
storage/data/outbox/conclusion/<workflow_id>.json
storage/data/outbox/researcher_revision/<workflow_id>.json

storage/data/workflows/conclusion/<workflow_id>.json
storage/data/workflows/conclusion/<workflow_id>.messages.jsonl
storage/data/artifacts/conclusion_packages/<workflow_id>.json
storage/data/artifacts/final_conclusions/<workflow_id>.json
storage/data/outbox/playwright/<workflow_id>.json
storage/data/outbox/deliberation_revision/<workflow_id>.json

storage/data/workflows/playwright/<workflow_id>.json
storage/data/workflows/playwright/<workflow_id>.messages.jsonl
storage/data/artifacts/narrative_blueprints/<workflow_id>.json
storage/data/artifacts/script_drafts/<workflow_id>.json
storage/data/artifacts/citation_manifests/<workflow_id>.json
storage/data/artifacts/visual_plans/<workflow_id>.json
storage/data/artifacts/final_script_packages/<workflow_id>.json
storage/data/outbox/conclusion_revision/<workflow_id>.json
storage/data/deliveries/<workflow_id>/final_script_package.json
storage/data/deliveries/<workflow_id>/script.md
storage/data/deliveries/<workflow_id>/citation_manifest.json
storage/data/deliveries/<workflow_id>/source_list.md
storage/data/deliveries/<workflow_id>/visual_plan.md
storage/data/deliveries/<workflow_id>/production_notes.md
```

旧Producer Prototypeの`storage/data/workflows/<workflow_id>.json`も読込互換を維持しています。

## 4. テスト

```powershell
py scripts\verify.py
```

この検証はcompile、全単体・統合テスト、STRICT RD読込、共通仕様のdrift検査、5層Mock E2E、6納品ファイル確認を順番に実行します。GitHubへ公開した後は`.github/workflows/ci.yml`がPython 3.11と3.14で同じ検証を自動実行します。

## 5. Discord Bot

`.env`の`DISCORD_BOT_TOKEN`を設定し、Developer PortalでMessage Content Intentを有効にしてから実行します。

```powershell
py main.py
```

Producerコマンド:

```text
!producer
!producer_topic 生成AIは人間の仕事を奪うのか
!producer_status <workflow_id>
```

Researcherコマンド:

```text
!researcher <workflow_id>
!researcher_status <workflow_id>
!researcher_result <workflow_id>
```

Deliberationコマンド:

```text
!deliberation <workflow_id>
!deliberation_status <workflow_id>
!deliberation_result <workflow_id>
!deliberation_resume <workflow_id>
```

Conclusionコマンド:

```text
!conclusion <workflow_id>
!conclusion_status <workflow_id>
!conclusion_options <workflow_id>
!conclusion_select <workflow_id> <candidate_id>
!conclusion_integrate <workflow_id> <candidate_id_1> <candidate_id_2>
!conclusion_result <workflow_id>
!conclusion_resume <workflow_id>
```

Playwrightコマンド:

```text
!playwright <workflow_id>
!playwright_status <workflow_id>
!playwright_script <workflow_id>
!playwright_citations <workflow_id>
!playwright_visuals <workflow_id>
!playwright_result <workflow_id>
!playwright_resume <workflow_id>
```

初期値ではResearcherは手動起動です。Producer完了後の自動起動を使う場合は`.env`で次を設定します。

```text
PRDCP_AUTO_START_RESEARCHER=true
PRDCP_AUTO_START_DELIBERATION=true
PRDCP_AUTO_START_CONCLUSION=true
PRDCP_AUTO_START_PLAYWRIGHT=true
```

## 6. OpenRouterへ切り替える場合

`.env`で次を設定します。

```text
PRDCP_PROVIDER=openrouter
OPENROUTER_API_KEY=<your key>
```

各`MODEL_...`には、利用時点でOpenRouterに登録されている実際のmodel IDを指定してください。設計上の表示名と環境変数の対応は`config/models.json`に記録しています。

OpenRouter応答は指定JSON Schemaとして検証されます。Schema不一致や技術エラーのretryは、Quality Reviewerによる成果物修正ループとは別に記録されます。

## 仕様上の正規化

- PMPはCommon仕様のv2.0を使用
- message typeと品質判定はstatus registryのlower snake caseへ統一
- Researcherの7カテゴリは共通`ResearchSource` Schemaで表現
- `sources`をAgent共通の出力名とし、カテゴリは`source_type`で識別
- Researcher→Deliberation payloadはCommonのhandoff contractが要求する項目をトップレベルに保持
- Researcherは証拠収集・整理に限定し、因果分析・解決策・結論を生成しない
- Deliberationの工程差は既存`deliberation_task_assignment/result`のpayloadで識別し、未登録message typeを追加しない
- 上流Evidence不足は`revision_required`と`revision_scope=researcher_return`で表現
- Deliberation→Conclusion payloadはCommon handoff contractの17項目をトップレベルに保持
- Deliberationは多角的分析に限定し、最終結論や政策選択を生成しない
- Conclusionの工程差は既存`conclusion_task_assignment/result`のpayloadで識別し、未登録message typeを追加しない
- Deliberation差し戻しは登録済み`revision_request`と`revision_scope=deliberation_return`で表現
- Quality Reviewerは候補を承認するだけで最終選択せず、人間の明示選択を必須とする
- Conclusion→Playwright payloadは実行用の`final_conclusion`、`conclusion_package`、`human_selection`、`traceability_manifest`、`limitations_to_disclose`を正本とし、Common handoff contractの24項目も互換用に保持
- Playwrightは新しいMessage Typeを追加せず、層内処理を登録済み`task`・`result`・`revision_request`で表現
- PlaywrightのFinal GateはCommonの列挙値を増やさず、Playwright State内の5状態へ限定
- Playwrightは独立Quality Reviewerを実行せず、決定論的Validator、Evidence & Citation Editor、Manager Final Gateで品質を保証

## RD Loader v2

Producer、Researcher、Deliberation、Conclusion、Playwrightの実装済み全31 Agentは、実行ごとに`role_definitions/registry.json`から対応RDを特定します。RuntimeはJSON検証後にSnapshotを固定し、LLM向けRole ContextとRuntime設定を分離してからSystem Promptを組み立てます。

```text
common/role_definitions/  Loader・Registry・Validator・Cache・Extractor・Access Log
common/prompting/         共通規則とPrompt Builder
role_definitions/producer/
role_definitions/researcher/
role_definitions/deliberation/
role_definitions/conclusion/
role_definitions/playwright/
```

本番では起動時に全RDを検証し、1件でも不正なら起動を停止します。

```text
PRDCP_RD_STRICT=true
PRDCP_RD_RELOAD=false
```

RD編集中は、Agent実行のたびにファイル更新を確認できます。不正な更新が見つかった場合、旧Cacheへ自動Fallbackせず、そのAgent実行を停止します。

```text
PRDCP_RD_RELOAD=true
```

Agentの応答PMPには`metadata.extensions.role_definition`としてRD ID、Version、SHA-256 Hashを記録します。Manager自身のRD使用情報はWorkflow Stateの`role_definition_usage`へ保存され、参照履歴は`storage/data/logs/rd_access.jsonl`へ記録されます。Role Definition本文全体や認証情報はAccess Logへ保存しません。

元のWord RDからJSON正本へ変換する際に行った構文修復は`role_definitions/SOURCE_REPAIRS.json`に記録しています。

## 開発・障害調査ガイド

- [Architecture](docs/architecture/ARCHITECTURE.md): 5層の統一構成と共通基盤
- [Maintenance](docs/operations/MAINTENANCE.md): 変更内容ごとの編集場所とRelease checklist
- [Troubleshooting](docs/operations/TROUBLESHOOTING.md): 状態値、ログ、よくある停止理由
- [Migration](docs/migration/README.md): ファイル配置計画とMigration Map
- [Audit](docs/audit/README.md): Canonical化の差分監査と検証結果
- [Changelog](CHANGELOG.md): v2での変更点
